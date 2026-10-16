"""Wait budgets for tests, scaled for slow runners."""

from __future__ import annotations

import os


def scaled(seconds: float) -> float:
    """seconds multiplied by TYCHORA_TEST_TIMEOUT_FACTOR (default 1; CI coverage runs use 3)."""
    return seconds * float(os.environ.get("TYCHORA_TEST_TIMEOUT_FACTOR", "1"))
