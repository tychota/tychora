"""The client bridge admits every caller and refuses only once it is closed.

The bridge has no admission cap of its own: the owner's lane bounds are what
refuse work when a queue is full, and `tests/runtime/test_lanes.py` pins those
refusals.
"""

import asyncio
import threading

import pytest

from tychora.application.bridge import ClientBridge
from tychora.application.runtime import BACKGROUND_BOUND, INTERACTIVE_BOUND
from tychora.contracts.errors import TychoraError


def test_bridge_admits_more_callers_than_any_owner_lane_bound():
    bridge = ClientBridge()
    release = threading.Event()
    callers = INTERACTIVE_BOUND + BACKGROUND_BOUND + 1

    async def held(index: int) -> int:
        while not release.is_set():
            await asyncio.sleep(0.001)
        return index

    try:
        futures = [bridge.submit(held(index)) for index in range(callers)]
        release.set()
        assert [future.result(timeout=10) for future in futures] == list(range(callers))
    finally:
        release.set()
        bridge.close()


def test_closed_bridge_refuses_with_the_workspace_closing_error():
    bridge = ClientBridge()
    bridge.close()

    async def never() -> None:
        raise AssertionError("a closed bridge must not run work")

    with pytest.raises(TychoraError) as error:
        bridge.submit(never())
    assert error.value.code == "WORKSPACE_BUSY"
    assert error.value.retryable
    assert error.value.reason == "owner.closing"
    assert "closing" in error.value.message
