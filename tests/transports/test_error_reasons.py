"""Error reasons reach HTTP, MCP, CLI, the HTTP client and the PDF child."""

import json

import httpx
import pytest
import typer
from mcp.types import TextContent

import tychora.ingestion.pdf_analysis as analysis
from tychora.application import pdf_intake
from tychora.contracts.errors import TychoraError
from tychora.interfaces.commands._common import fail
from tychora.interfaces.http.app import error_response
from tychora.interfaces.http.client import _checked
from tychora.interfaces.mcp.server import _tool_error


def _ranged() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST", "Range is outside the content.", reason="range.unsatisfiable"
    )


def test_http_error_response_carries_reason_and_status_override():
    response = error_response(_ranged())
    assert response.status_code == 416
    assert json.loads(bytes(response.body))["reason"] == "range.unsatisfiable"


def test_http_client_rebuilds_reason():
    response = httpx.Response(
        409,
        json={
            "schema_version": "2",
            "code": "WORKSPACE_BUSY",
            "message": "Busy.",
            "retryable": True,
            "reason": "owner.live",
        },
    )
    with pytest.raises(TychoraError) as error:
        _checked(response)
    assert (error.value.code, error.value.reason, error.value.retryable) == (
        "WORKSPACE_BUSY",
        "owner.live",
        True,
    )


def test_http_client_treats_unknown_reason_as_invalid_response():
    response = httpx.Response(
        400, json={"code": "INVALID_REQUEST", "message": "x", "reason": "nope.never"}
    )
    with pytest.raises(TychoraError) as error:
        _checked(response)
    assert error.value.code == "PROVIDER_FAILURE"


def test_mcp_tool_error_carries_reason():
    result = _tool_error(_ranged())
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert json.loads(content.text)["reason"] == "range.unsatisfiable"


def test_cli_failure_prints_reason(capsys):
    with pytest.raises(typer.Exit) as exited:
        fail(_ranged())
    assert exited.value.exit_code == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "range.unsatisfiable"


def test_pdf_analysis_child_keeps_reason(monkeypatch):
    def broken(path, limits):
        raise TychoraError(
            "INTEGRITY_ERROR", "Unexpected.", reason="invariant.unexpected"
        )

    monkeypatch.setattr(analysis, "analyse_pdf", broken)

    class Sender:
        sent: tuple = ()

        def send(self, value):
            self.sent = value

        def close(self):
            pass

    sender = Sender()
    pdf_intake._analysis_child("unused.pdf", {}, sender)
    assert sender.sent == (
        False,
        ("INTEGRITY_ERROR", "Unexpected.", "invariant.unexpected"),
    )
    rebuilt = pdf_intake._child_error(sender.sent[1])
    assert (rebuilt.code, rebuilt.reason) == ("INTEGRITY_ERROR", "invariant.unexpected")
