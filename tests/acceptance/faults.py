"""Test-only process-death barriers; never imported by production code."""

from __future__ import annotations

import os
import time
from pathlib import Path


class FaultInjector:
    def __init__(self, marker_dir: Path, selected: str | None = None) -> None:
        self.marker_dir = marker_dir
        self.selected = selected

    def hit(self, point: str) -> None:
        if point != self.selected:
            return
        marker = self.marker_dir / point
        with marker.open("wb") as stream:
            stream.write(str(os.getpid()).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        while True:
            time.sleep(1)
