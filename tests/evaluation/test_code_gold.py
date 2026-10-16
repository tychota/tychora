"""Code gold set: real queries over Tychora's own pinned source, lexical and structural."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.code.conftest import git
from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.assets import asset_path
from tests.helpers.code_gold import (
    DATASET_ID,
    load_templates,
    materialize,
    mean_metrics,
    write_dataset,
)
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.ports.execution import ExecutionContext

pytestmark = [pytest.mark.slow, pytest.mark.assets("TYCHORA_TEST_CODE_CORPUS")]

METRICS = ("file_recall", "definition_recall", "file_mrr", "file_ndcg")
VARIANTS = (
    {
        "name": "lexical",
        "profile": "test",
        "overrides": {"channels": ["lexical"], "rerank": False},
    },
    {
        "name": "lexical_structural",
        "profile": "test",
        "overrides": {"channels": ["lexical", "structural_graph"], "rerank": False},
    },
)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    shutil.copytree(asset_path("TYCHORA_TEST_CODE_CORPUS") / "src", repo / "src")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Gold Corpus")
    git(repo, "config", "user.email", "gold@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pinned corpus")
    return repo


def _wait(kb, job_id, budget):
    """Poll a job to a terminal state within budget seconds; one wait call allows at most 300."""
    end = time.monotonic() + budget
    while True:
        status = kb.jobs.wait(job_id, timeout=min(30, max(0.0, end - time.monotonic())))
        if status.state not in {"queued", "running"} or time.monotonic() >= end:
            return status


def test_code_gold_set_runs_offline_with_valid_locators(tmp_path):
    repo = _repository(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n'
        "[configuration.limits]\nmax_tokens=30000\n"
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "workspace", config=config, token_counter=SyntheticCounter()
    ) as kb:
        handle = kb.sources.add(
            {
                "type": "repository",
                "path": str(repo),
                "source_key": "tychora",
                "collection_id": "default",
                "kind": "code_file@1",
                "metadata": {"title": "Tychora source"},
            }
        )
        status = _wait(kb, handle.id, scaled(900))
        assert status.state == "succeeded", status.error_code
        owner = kb._workspace.owner

        def labels():
            return owner.db.execute(
                "SELECT s.source_id,r.revision_id,sr.data_json FROM structured_records sr "
                "JOIN evidence_units u USING(unit_id) JOIN extractions e USING(extraction_id) "
                "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
                "WHERE sr.record_type='code_symbol'"
            ).fetchall()

        rows = kb._workspace.bridge.call(owner.run_blocking(labels, ExecutionContext()))
        source_id, revision_id = rows[0][:2]
        definitions: dict[tuple[str, str], list[str]] = {}
        for _, _, data in rows:
            record = json.loads(data)
            key = (record["relative_path"], record["name"])
            definitions.setdefault(key, []).append(record["definition_key"])
        cases = [
            materialize(
                t, source_id=source_id, revision_id=revision_id, definitions=definitions
            )
            for t in load_templates()
        ]
        write_dataset(tmp_path / "gold", cases)
        kb.evaluations.register_dataset(tmp_path / "gold")
        snapshot = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.store.resolve_snapshot(None), ExecutionContext()
            )
        )
        run = kb.evaluations.run(
            {
                "dataset_id": DATASET_ID,
                "split": "test",
                "snapshot_id": snapshot.snapshot_id,
                "variants": list(VARIANTS),
                "seed": 0,
                "repetitions": 1,
                "offline": True,
            }
        )
        assert _wait(kb, run.id, scaled(900)).state == "succeeded"
        report = kb.evaluations.get(run.id)

    test_cases = {case["case_id"] for case in cases if case["split"] == "test"}
    assert set(report["cases"]) == test_cases
    for entry in report["cases"].values():
        for result in entry["runs"]:
            values = {m["name"]: m["value"] for m in result["metrics"]["metrics"]}
            assert values["forbidden_leakage"] in (None, 0.0)
            assert values["locator_validity"] in (None, 1.0)
    means = mean_metrics(report, METRICS)
    assert set(means) == {"lexical", "lexical_structural"}
    destination = os.environ.get("TYCHORA_CODE_GOLD_REPORT")
    if destination:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(
            json.dumps(
                {"dataset": DATASET_ID, "variants": means}, indent=2, sort_keys=True
            )
            + "\n"
        )
