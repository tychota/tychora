"""One ingest verifies job bindings no more often than before the sub-stage markers."""

from pathlib import Path

import pytest

from tests.application.test_inspection import _published
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.ports.execution import ExecutionContext

# Counted on main for one add: start, acquire, parse, project and publish.
MAIN_VERIFICATIONS_PER_INGEST = 7


@pytest.mark.asyncio
async def test_sub_stage_markers_do_not_reverify_bindings(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    original = JobRepository._verify_bindings

    def counting(self, status):
        calls.append(status.id)
        return original(self, status)

    def patch():
        monkeypatch.setattr(JobRepository, "_verify_bindings", counting)

    async with _published(tmp_path, before_add=patch) as (owner, _, _):
        stages = await owner.run_blocking(
            lambda: [
                row[0]
                for row in owner.db.execute(
                    "SELECT json_extract(payload_json,'$.stage') FROM job_events"
                    " ORDER BY rowid"
                ).fetchall()
            ],
            ExecutionContext(),
        )
    assert {"windowing", "normalize_commit"} <= set(stages)
    assert 0 < len(calls) <= MAIN_VERIFICATIONS_PER_INGEST
