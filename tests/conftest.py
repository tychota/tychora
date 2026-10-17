import socket

import pytest

from tests.helpers import assets

pytest_plugins = ["tests.fixtures.docling"]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip (or, for qualification and release, fail) when declared assets are missing."""
    assets.check_item(item)


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Forget the finished test's declared assets."""
    assets.end_test()


@pytest.fixture(autouse=True)
def deny_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = request.node.get_closest_marker("live")
    qualification = request.node.get_closest_marker("qualification")
    local_loopback = qualification and qualification.kwargs.get("loopback") is True
    if live or local_loopback:
        return

    def blocked_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is disabled in ordinary tests")

    monkeypatch.setattr(socket.socket, "connect", blocked_connection)
    monkeypatch.setattr(socket, "create_connection", blocked_connection)
