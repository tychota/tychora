"""The benchmark entry point runs a small offline suite end to end."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tychora.contracts.operations import JobEvent
from tychora.evaluation.bench.results import Assertion, load_result
from tychora.interfaces import bench
from tychora.interfaces.bench import TychoraBenchClient, main


class FakeJobs:
    """Job operations of a workspace whose jobs have many events."""

    def __init__(self, total: int, states: tuple[str, ...] = ("succeeded",)) -> None:
        self.total = total
        self.states = iter(states)
        self.timeouts: list[float] = []
        self.pages: list[int] = []

    def events_after(self, job_id: str, *, after: int = 0):
        self.pages.append(after)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        return tuple(
            JobEvent(
                job_id=job_id,
                sequence=sequence,
                timestamp=start + timedelta(seconds=sequence),
                stage="parse",
                state="running",
            )
            for sequence in range(after + 1, min(after + 100, self.total) + 1)
        )

    def wait(self, job_id: str, *, timeout: float):
        self.timeouts.append(timeout)
        return SimpleNamespace(state=next(self.states))


def _client(jobs: FakeJobs, tmp_path) -> TychoraBenchClient:
    return TychoraBenchClient(SimpleNamespace(jobs=jobs), tmp_path, ("lexical",))


def test_events_pages_through_every_event(tmp_path):
    jobs = FakeJobs(250)
    events = _client(jobs, tmp_path).events("job")
    assert [event.sequence for event in events] == list(range(1, 251))
    assert jobs.pages == [0, 100, 200]


def test_events_stop_on_an_exact_page_boundary(tmp_path):
    jobs = FakeJobs(200)
    assert len(_client(jobs, tmp_path).events("job")) == 200


def test_waits_are_bounded_to_the_job_wait_cap(tmp_path):
    jobs = FakeJobs(0, states=("running", "running", "succeeded"))
    assert _client(jobs, tmp_path).wait("job") == "succeeded"
    assert jobs.timeouts
    assert max(jobs.timeouts) <= 300.0


def test_footprint_counts_workspace_files(tmp_path):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "big").write_bytes(b"x" * 50)
    (tmp_path / "small").write_bytes(b"x" * 10)
    footprint = _client(FakeJobs(0), tmp_path).footprint()
    assert (footprint.total_bytes, footprint.largest_artifact_bytes) == (60, 50)


def test_offline_query_suite_writes_a_result(tmp_path):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "query",
            "--profile",
            "offline",
            "--limit",
            "5",
            "--out",
            str(tmp_path / "results"),
            "--cache",
            str(tmp_path / "cache"),
        ]
    )
    assert code == 0
    [path] = list((tmp_path / "results").rglob("query-offline.json"))
    document = json.loads(path.read_text())
    assert document["schema"] == "tychora.bench-result"
    assert document["stages"]["total"]["n"] >= 1
    assert document["corpus"]["name"] == "bench-small"
    assert {"cpu", "cores", "ram_bytes", "os", "python", "sqlite", "chroma"} <= set(
        document["machine"]
    )


def test_families_suite_exits_with_usage_code(tmp_path, capsys):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "families",
            "--profile",
            "offline",
            "--cache",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert "families suite" in capsys.readouterr().err


def test_incomparable_results_are_reported_cleanly(tmp_path, capsys):
    arguments = [
        "run",
        "--corpus",
        "bench-small",
        "--suite",
        "ingest",
        "--profile",
        "offline",
        "--limit",
        "2",
        "--out",
        str(tmp_path / "results"),
        "--cache",
        str(tmp_path / "cache"),
    ]
    assert main(arguments) == 0
    [first] = (tmp_path / "results").rglob("ingest-offline.json")
    document = json.loads(first.read_text())
    document["reference_machine"] = not document["reference_machine"]
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps(document))
    capsys.readouterr()
    assert (
        main(
            [
                *arguments[:-4],
                "--out",
                str(tmp_path / "again"),
                "--cache",
                str(tmp_path / "cache"),
                "--compare",
                str(previous),
            ]
        )
        == 2
    )
    assert "bench.incomparable" in capsys.readouterr().err


def _args(tmp_path, *extra, suite="ingest", corpus="bench-small", profile="offline"):
    return [
        "run",
        "--corpus",
        corpus,
        "--suite",
        suite,
        "--profile",
        profile,
        "--limit",
        "2",
        "--out",
        str(tmp_path / "results"),
        "--cache",
        str(tmp_path / "cache"),
        *extra,
    ]


def _real_result(tmp_path):
    assert main(_args(tmp_path)) == 0
    [path] = (tmp_path / "results").rglob("ingest-offline.json")
    return load_result(path)


def test_online_profile_needs_a_credential_before_any_workspace(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    assert main(_args(tmp_path, profile="online")) == 2
    assert "VOYAGE_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "cache" / "workspaces").exists()
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    assert main(_args(tmp_path, profile="online")) == 2


def test_missing_corpus_exits_with_usage_code(tmp_path, capsys):
    assert main(_args(tmp_path, corpus="does-not-exist")) == 2
    assert capsys.readouterr().err


def test_over_budget_stage_exits_one(tmp_path, monkeypatch, capsys):
    result = _real_result(tmp_path)
    failing = result.model_copy(
        update={
            "stages": {
                name: stats.model_copy(update={"verdict": "fail"})
                for name, stats in result.stages.items()
            }
        }
    )
    monkeypatch.setattr(bench, "run_suite", lambda *_: failing)
    assert main(_args(tmp_path)) == 1
    assert "over budget" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"failures": ("doc-1: job failed",)}, "doc-1: job failed"),
        ({"assertions": (Assertion(name="probe-1", passed=False),)}, "probe-1"),
        ({"aborted": True}, "aborted"),
    ],
)
def test_invalid_measurements_exit_one(tmp_path, update, expected):
    result = _real_result(tmp_path).model_copy(update=update)
    assert any(expected in problem for problem in bench._problems(result, None))


def test_invalid_measurement_fails_the_command(tmp_path, monkeypatch, capsys):
    result = _real_result(tmp_path).model_copy(update={"aborted": True})
    monkeypatch.setattr(bench, "run_suite", lambda *_: result)
    assert main(_args(tmp_path)) == 1
    assert "aborted" in capsys.readouterr().err


def test_auto_workspace_is_removed_and_explicit_one_is_kept(tmp_path):
    assert main(_args(tmp_path)) == 0
    assert not list((tmp_path / "cache" / "workspaces").iterdir())
    kept = tmp_path / "kept"
    assert main(_args(tmp_path, "--workspace", str(kept))) == 0
    assert (kept / "workspace.toml").exists()
