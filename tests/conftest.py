import socket

import pytest


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
