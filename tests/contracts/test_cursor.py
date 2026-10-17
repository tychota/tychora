"""Keyset cursors round-trip and reject anything they did not produce."""

import base64

import pytest

from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import Cursor


def test_cursor_round_trips():
    cursor = Cursor(sort_key=("guide", 3), last_id="source-1")
    token = cursor.encode()
    assert "=" not in token
    assert Cursor.decode(token) == cursor


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        "x" * 1025,
        base64.urlsafe_b64encode(b'{"k":[],"id":""}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"k":[1.5],"id":"a"}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"k":[],"id":"a","x":1}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"id":"a","k":[]}').decode(),
        base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("="),
    ],
)
def test_cursor_rejects_tampered_tokens(token):
    with pytest.raises(TychoraError) as error:
        Cursor.decode(token)
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "cursor.invalid",
    )
