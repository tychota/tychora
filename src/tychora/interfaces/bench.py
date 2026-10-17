"""Run the engine benchmark: ``python -m tychora.interfaces.bench fetch|run``."""

import argparse
import os
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

from tychora import Tychora
from tychora.application.job_wait import wait_until_settled
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobEvent
from tychora.contracts.requests import SourceInput
from tychora.evaluation.bench.corpus import BenchCorpusError, fetch_corpus, load_corpus
from tychora.evaluation.bench.harness import (
    BenchSource,
    BenchUnavailable,
    Footprint,
    RunContext,
    SearchOutcome,
    SuiteInputs,
    require_available,
    run_suite,
)
from tychora.evaluation.bench.results import (
    is_reference,
    load_result,
    machine_fingerprint,
    regressions,
    write_result,
)
from tychora.evaluation.bench.stages import WallClockStageSource

EVENT_PAGE = 100
WAIT_SECONDS = 3600.0
DEFAULT_BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"
OFFLINE_CONFIG = (
    'schema_version = 1\n[profiles.bench]\nbuiltin = "general-documents@1"\n'
    '[defaults]\nprofile = "bench"\n'
)


class BenchCounter:
    """Deterministic output counter for offline runs (about four characters a token)."""

    model = "bench-quarter-codepoints"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        """Count output tokens as a quarter of the characters."""
        return max(1, len(text) // 4)


class TychoraBenchClient:
    """A benchmark client over one open local workspace."""

    def __init__(self, kb: Tychora, root: Path, modalities: tuple[str, ...]) -> None:
        self._kb = kb
        self._root = root
        self._modalities = modalities

    def add(self, source: BenchSource) -> str:
        """Add one source to the workspace and return its job id."""
        fields: dict[str, object] = {
            "source_key": source.source_key,
            "collection_id": self._kb.default_collection_id,
            "kind": source.kind,
            "metadata": {"title": source.title},
        }
        if source.format:
            fields["format"] = source.format
        if source.media_type:
            fields["media_type"] = source.media_type
        if source.text is not None:
            fields.update(type="text", text=source.text)
        else:
            fields.update(type="bytes", data=source.data)
        return self._kb.sources.add(SourceInput.model_validate(fields)).id

    def wait(self, job_id: str) -> str:
        """Wait for the job in bounded calls and return its final state."""
        return wait_until_settled(
            lambda timeout: self._kb.jobs.wait(job_id, timeout=timeout), WAIT_SECONDS
        ).state

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        """Return every event of the job, paging through the service limit."""
        found: list[JobEvent] = []
        while True:
            page = self._kb.jobs.events_after(
                job_id, after=found[-1].sequence if found else 0
            )
            found.extend(page)
            if len(page) < EVENT_PAGE:
                return tuple(found)

    def search(self, question: str) -> SearchOutcome:
        """Search the workspace and time the call; engine errors become an error code."""
        began = time.perf_counter()
        try:
            result = self._kb.search(
                {"question": question, "modalities": list(self._modalities)}
            )
        except TychoraError as error:
            return SearchOutcome(
                run_id=None,
                unit_ids=(),
                error_code=error.code,
                duration_ms=(time.perf_counter() - began) * 1000,
            )
        return SearchOutcome(
            run_id=result.run_id,
            unit_ids=tuple(
                unit for segment in result.segments for unit in segment.matched_unit_ids
            ),
            error_code=None,
            duration_ms=(time.perf_counter() - began) * 1000,
        )

    def footprint(self) -> Footprint:
        """Sum the workspace files and find the largest artifact."""
        total = largest = 0
        for path in self._root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                total += size
                if path.parent.name == "artifacts":
                    largest = max(largest, size)
        return Footprint(total_bytes=total, largest_artifact_bytes=largest)


def _commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() or "unknown"


def _checkpoints(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(",") if part)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tychora.interfaces.bench")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser(
        "fetch", help="Download and verify a corpus's public documents."
    )
    run = commands.add_parser("run", help="Run one benchmark suite.")
    for sub in (fetch, run):
        sub.add_argument("--corpus", required=True)
        sub.add_argument("--cache", type=Path, default=Path(".cache/bench"))
        sub.add_argument("--benchmarks", type=Path, default=DEFAULT_BENCHMARKS)
    run.add_argument(
        "--suite", required=True, choices=("ingest", "query", "scale", "families")
    )
    run.add_argument("--profile", required=True, choices=("offline", "online"))
    run.add_argument("--out", type=Path, default=Path("bench-results"))
    run.add_argument("--workspace", type=Path)
    run.add_argument("--limit", type=int)
    run.add_argument("--checkpoints", type=_checkpoints, default=(100, 1000, 10000))
    run.add_argument("--max-wall-seconds", type=float)
    run.add_argument(
        "--compare", type=Path, help="Earlier result of the same runner class."
    )
    return parser


def _open(workspace: Path, profile: str, benchmarks: Path) -> tuple[Tychora, str]:
    workspace.mkdir(parents=True, exist_ok=True)
    settings = workspace / "workspace.toml"
    if not settings.exists():
        settings.write_text(
            OFFLINE_CONFIG
            if profile == "offline"
            else (benchmarks / "online-workspace.toml").read_text()
        )
    config = resolve_config(settings, "bench", {})
    kb = Tychora.open(
        workspace,
        config=config,
        token_counter=BenchCounter() if profile == "offline" else None,
    )
    if profile == "online":
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": str(kb.default_collection_id),
                "provider": "voyage",
                "action": "grant",
            }
        )
    return kb, config.config_sha


def _problems(result, compare: Path | None) -> list[str]:
    found = list(regressions(result, load_result(compare))) if compare else []
    found += [
        f"{name}: over budget"
        for name, stats in result.stages.items()
        if stats.verdict == "fail"
    ]
    return found


def _run(args: argparse.Namespace) -> int:
    require_available(args.suite)
    corpus = load_corpus(args.corpus, args.benchmarks)
    workspace = (
        args.workspace
        or args.cache / "workspaces" / f"{args.suite}-{args.profile}-{time.time_ns()}"
    )
    if workspace.exists() and any(workspace.iterdir()):
        raise BenchUnavailable(f"{workspace} must be a new, empty directory")
    machine = machine_fingerprint()
    reference = tomllib.loads((args.benchmarks / "reference-machine.toml").read_text())
    kb, config_sha = _open(workspace, args.profile, args.benchmarks)
    try:
        client = TychoraBenchClient(
            kb,
            workspace,
            ("lexical",) if args.profile == "offline" else ("lexical", "text"),
        )
        context = RunContext(
            commit=_commit(),
            profile=args.profile,
            machine=machine,
            reference=is_reference(
                machine, reference, os.environ.get("TYCHORA_BENCH_RUNNER_CLASS")
            ),
            retained_sha=config_sha,
            checkpoints=args.checkpoints,
            max_wall_seconds=args.max_wall_seconds,
            limit=args.limit,
        )
        result = run_suite(
            client,
            corpus,
            args.suite,
            context,
            SuiteInputs(args.cache, WallClockStageSource(client.events)),
        )
    finally:
        kb.close()
    print(write_result(result, args.out))
    problems = _problems(result, args.compare)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run ``fetch`` or ``run``; return the process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            print(fetch_corpus(load_corpus(args.corpus, args.benchmarks), args.cache))
            return 0
        return _run(args)
    except (
        BenchCorpusError,
        BenchUnavailable,
        TychoraError,
        ValueError,
        OSError,
    ) as error:
        reason = getattr(error, "reason", None)
        print(f"{error} [{reason}]" if reason else error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
