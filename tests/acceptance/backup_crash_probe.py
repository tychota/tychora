"""Synthetic backup victim, killed after a durable staged payload copy."""

from __future__ import annotations

import sys
from pathlib import Path

from tests.acceptance.faults import FaultInjector
from tests.operations.test_removal import _Counter
from tychora import Tychora
from tychora.adapters.files.archive import ArchiveWriter
from tychora.application.backup import BackupService
from tychora.ports.execution import ExecutionContext


def run(root: Path, point: str) -> None:
    injector = FaultInjector(root, point)
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        copy = ArchiveWriter.copy_artifact

        def paused(self, source, ref, ctx):
            result = copy(self, source, ref, ctx)
            injector.hit(point)
            return result

        ArchiveWriter.copy_artifact = paused
        BackupService(kb._workspace.owner).export(
            root / "backup", kb._workspace.owner.scope, ctx=ExecutionContext()
        )
    raise AssertionError("The expected backup boundary was not reached")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
