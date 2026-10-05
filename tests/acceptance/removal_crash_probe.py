"""Synthetic original-removal victim for parent-owned SIGKILL tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.acceptance.faults import FaultInjector
from tests.operations.test_removal import _Counter
from tychora import Tychora


def run(root: Path, point: str) -> None:
    source, revision, _sha = json.loads((root / "victim.json").read_text())
    injector = FaultInjector(root, point)
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        artifacts = kb._workspace.owner.artifacts
        unlink = artifacts.unlink_registered

        def paused(ref):
            if point == "tombstone_before_unlink":
                injector.hit(point)
            result = unlink(ref)
            if point == "unlink_before_final_sql":
                injector.hit(point)
            return result

        artifacts.unlink_registered = paused
        kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source,
                "revision_ids": [revision],
                "confirm_original_removal": True,
            }
        )
    raise AssertionError("The expected removal boundary was not reached")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
