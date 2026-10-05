"""Gold upload is bounded data, never a server-selected filesystem path."""

import pytest
from httpx import ASGITransport, AsyncClient

from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


@pytest.mark.asyncio
async def test_http_gold_registration_requires_manage_and_payload_bytes(
    executable_integrity_corpus,
):
    kb, gold, _ = executable_integrity_corpus
    reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
    app = create_app(
        kb._workspace.owner,
        kb._workspace.services,
        LocalAuth({"owner": kb.scope, "reader": reader}, authority="127.0.0.1:8765"),
    )
    payload = {
        "manifest_json": (gold / "manifest.json").read_text(),
        "cases_jsonl": (gold / "cases.jsonl").read_text(),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        denied = await client.post(
            "/v1/evaluations/datasets",
            json=payload,
            headers={"Authorization": "Bearer reader"},
        )
        assert denied.status_code == 403
        registered = await client.post(
            "/v1/evaluations/datasets",
            json=payload,
            headers={"Authorization": "Bearer owner"},
        )
        assert registered.status_code == 201, registered.text
        assert registered.json()["version_sha"]
