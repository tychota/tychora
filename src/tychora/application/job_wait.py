"""Wait for a job past the cap of a single wait call."""

import time
from collections.abc import Callable
from typing import Protocol

MAX_WAIT_SECONDS = 300.0
SETTLED_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class _Settling(Protocol):
    @property
    def state(self) -> str: ...


def wait_until_settled[T: _Settling](
    wait_once: Callable[[float], T],
    overall_seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call ``wait_once`` with bounded timeouts until the job settles or the deadline passes.

    Each call waits at most ``MAX_WAIT_SECONDS``, the cap of a single job wait, so
    the overall deadline can be far longer. The last status is returned even when
    it is not settled.
    """
    end = clock() + overall_seconds
    while True:
        status = wait_once(min(MAX_WAIT_SECONDS, max(0.0, end - clock())))
        if status.state in SETTLED_STATES or clock() >= end:
            return status
