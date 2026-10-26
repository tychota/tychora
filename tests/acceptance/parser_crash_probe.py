"""Synthetic owner with a live parser child, launched for owner SIGKILL tests."""

from __future__ import annotations

import os
import sys
import time
from io import BytesIO
from pathlib import Path

from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


def blocked_parser(paths: tuple[Path, ...]) -> None:
    root = Path(paths[0].read_text())
    marker = root / "parser-child-live"
    with marker.open("w") as stream:
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
    while True:
        time.sleep(1)


def run(root: Path) -> None:
    config = resolve_config(root / "workspace.toml", "test", {})
    scope = RequestScope(principal_id="synthetic-parser-owner")
    with WorkspaceOwner.open(root / "data", config, scope) as owner:
        ctx = ExecutionContext()
        ref = owner.run_write_sync(
            lambda: owner.artifacts.put_stream(
                BytesIO(str(root).encode()), "text/plain"
            ),
            ctx,
        )
        owner.run_parser_sync(blocked_parser, (ref,), ctx)


if __name__ == "__main__":
    run(Path(sys.argv[1]))
