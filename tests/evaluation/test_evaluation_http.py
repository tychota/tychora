"""Gold upload is bounded data, never a server-selected filesystem path."""

import pytest
from httpx import ASGITransport, AsyncClient, Response

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


@pytest.mark.asyncio
@pytest.mark.slow
async def test_http_evaluation_list_and_detail_serialize_real_runs_and_deny_reader(
    executable_integrity_corpus,
):
    import asyncio

    kb, gold, cases = executable_integrity_corpus
    reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
    app = create_app(
        kb._workspace.owner,
        kb._workspace.services,
        LocalAuth({"owner": kb.scope, "reader": reader}, authority="127.0.0.1:8765"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        registered = await client.post(
            "/v1/evaluations/datasets",
            json={
                "manifest_json": (gold / "manifest.json").read_text(),
                "cases_jsonl": (gold / "cases.jsonl").read_text(),
            },
        )
        assert registered.status_code == 201, registered.text
        query = await client.post(
            "/v1/queries", json={"question": "synthetic", "modalities": ["lexical"]}
        )
        assert query.status_code == 200, query.text
        profile = kb._workspace.owner.config.profile
        accepted = await client.post(
            "/v1/evaluations",
            json={
                "dataset_id": registered.json()["dataset_id"],
                "split": "test",
                "snapshot_id": query.json()["snapshot_id"],
                "variants": [{"name": "baseline", "profile": profile}],
                "offline": False,
            },
        )
        assert accepted.status_code == 202, accepted.text
        evaluation_id = accepted.json()["id"]
        listing, detail = await asyncio.gather(
            client.get("/v1/evaluations"),
            client.get(f"/v1/evaluations/{evaluation_id}"),
            return_exceptions=True,
        )
        assert isinstance(listing, Response), repr(listing)
        assert isinstance(detail, Response), repr(detail)
        assert listing.status_code == 200, listing.text
        assert evaluation_id in {entry["evaluation_id"] for entry in listing.json()}
        assert detail.status_code == 200, detail.text
        assert detail.json()["evaluation_id"] == evaluation_id
        assert detail.json()["report"]["dataset_id"] == registered.json()["dataset_id"]
        assert len(detail.json()["cases"]) == len(cases)
        for path in ("/v1/evaluations", f"/v1/evaluations/{evaluation_id}"):
            denied = await client.get(path, headers={"Authorization": "Bearer reader"})
            assert denied.status_code == 403
