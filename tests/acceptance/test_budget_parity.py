"""The public HTTP response bytes obey the admitted query output contract."""

import pytest
from httpx2 import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


class _Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


@pytest.mark.asyncio
async def test_canonical_http_wire_fits_actual_bytes_and_tokens(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=_Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "budget",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "A synthetic café π marker with UTF-8 bytes.",
                "metadata": {
                    "title": "Budget fixture",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id).state == "succeeded"
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"synthetic-token": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            response = await client.post(
                "/v1/queries",
                headers={"Authorization": "Bearer synthetic-token"},
                json={
                    "question": "café",
                    "modalities": ["lexical"],
                    "budgets": {"max_tokens": 5000, "max_response_bytes": 5000},
                },
            )
        assert response.status_code == 200, response.text
        assert b"caf\xc3\xa9" in response.content
        contract = response.json()["budget_contract"]
        assert len(response.content) <= contract["max_response_bytes"] == 5000
        assert len(response.content.decode("utf-8")) <= contract["max_tokens"] == 5000
