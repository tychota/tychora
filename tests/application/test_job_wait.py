"""Waits longer than one bounded call loop until a deadline."""

import pytest

from tychora.application.job_wait import MAX_WAIT_SECONDS, wait_until_settled


class _Status:
    def __init__(self, state: str) -> None:
        self.state = state


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_each_call_is_bounded_and_the_loop_ends_at_a_terminal_state():
    clock = _Clock()
    timeouts: list[float] = []

    def wait_once(timeout: float):
        timeouts.append(timeout)
        clock.now += timeout
        return _Status("succeeded" if len(timeouts) == 3 else "running")

    status = wait_until_settled(wait_once, 3600.0, clock=clock)
    assert status.state == "succeeded"
    assert timeouts == [MAX_WAIT_SECONDS] * 3
    assert max(timeouts) <= 300.0


def test_the_last_call_only_covers_what_remains_of_the_deadline():
    clock = _Clock()
    timeouts: list[float] = []

    def wait_once(timeout: float):
        timeouts.append(timeout)
        clock.now += timeout
        return _Status("running")

    status = wait_until_settled(wait_once, 650.0, clock=clock)
    assert status.state == "running"
    assert timeouts == [300.0, 300.0, 50.0]


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted"])
def test_every_terminal_state_stops_the_loop(state):
    calls = []

    def wait_once(timeout: float):
        calls.append(timeout)
        return _Status(state)

    assert wait_until_settled(wait_once, 3600.0).state == state
    assert len(calls) == 1
