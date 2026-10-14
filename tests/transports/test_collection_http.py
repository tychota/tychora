"""Managed identity is explicit and independent of identical static grants."""

import pytest
from httpx import ASGITransport, AsyncClient

from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


@pytest.mark.asyncio
async def test_managed_create_refresh_archive_and_reopen(tmp_path):
    path = tmp_path / "workspace"
    with Tychora.open(path, token_counter=Counter()) as kb:
        original = kb.scope
        reader = original.model_copy(update={"capabilities": frozenset({"read"})})
        auth = LocalAuth(
            {"managed": original, "static": original, "reader": reader},
            authority="127.0.0.1:8765",
            managed_owner_token="managed",
        )
        app = create_app(kb._workspace.owner, kb._workspace.services, auth)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            body = {"title": "  Library  ", "slug": "library"}
            for token in ("static", "reader"):
                denied = await client.post(
                    "/v1/collections",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert denied.status_code == 403
            denied = await client.post(
                "/v1/collections",
                json=body,
                headers={
                    "Authorization": "Bearer managed",
                    "X-Tychora-Scope": reader.model_dump_json(),
                },
            )
            assert denied.status_code == 403
            headers = {"Authorization": "Bearer managed"}
            response = await client.post("/v1/collections", json=body, headers=headers)
            assert response.status_code == 201, response.text
            collection = response.json()
            cid = collection["collection_id"]
            assert collection["title"] == "Library"
            fresh = (await client.get("/v1/handshake", headers=headers)).json()
            static = (
                await client.get(
                    "/v1/handshake", headers={"Authorization": "Bearer static"}
                )
            ).json()
            assert fresh["managed_workspace_owner"]
            assert not static["managed_workspace_owner"]
            assert cid in fresh["scope"]["collection_ids"]
            assert cid not in static["scope"]["collection_ids"]
            assert auth._grants == (("static", original), ("reader", reader))
            assert cid in kb.scope.collection_ids
            source = {
                "type": "text",
                "source_key": "managed-only",
                "collection_id": cid,
                "kind": "document@1",
                "format": "markdown",
                "text": "Synthetic managed collection.",
                "metadata": {"title": "Managed"},
            }
            assert (
                await client.post(
                    "/v1/sources",
                    json=source,
                    headers={"Authorization": "Bearer static"},
                )
            ).status_code == 403
            accepted = await client.post("/v1/sources", json=source, headers=headers)
            assert accepted.status_code == 202, accepted.text
            from tychora.ports.execution import ExecutionContext

            assert (
                await kb._workspace.services.jobs.wait(
                    accepted.json()["id"], ExecutionContext(), timeout=5
                )
            ).state == "succeeded"
            duplicate = await client.post("/v1/collections", json=body, headers=headers)
            assert duplicate.status_code == 409
            for slug in ("default",):
                assert (
                    await client.post(
                        "/v1/collections", json={**body, "slug": slug}, headers=headers
                    )
                ).status_code == 400
            archived = (
                await client.post(f"/v1/collections/{cid}/archive", headers=headers)
            ).json()
            again = (
                await client.post(f"/v1/collections/{cid}/archive", headers=headers)
            ).json()
            assert archived == again
            assert archived["archived_at"]
            assert cid in kb.scope.collection_ids
            listed = (await client.get("/v1/collections", headers=headers)).json()
            assert cid not in [c["collection_id"] for c in listed["collections"]]
            retained = await client.get(f"/v1/collections/{cid}", headers=headers)
            assert retained.status_code == 200
            reactivated = (
                await client.post(f"/v1/collections/{cid}/reactivate", headers=headers)
            ).json()
            assert (
                reactivated["collection_id"] == cid
                and reactivated["archived_at"] is None
            )
    with Tychora.open(path, token_counter=Counter()) as reopened:
        assert cid in reopened.scope.collection_ids


@pytest.mark.asyncio
async def test_pdf_archive_between_analysis_and_submit_retains_receipt(tmp_path):
    from tests.transports.test_pdf_intake import descriptor, make_pdf

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth(
                {"managed": kb.scope},
                authority="127.0.0.1:8765",
                managed_owner_token="managed",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer managed"},
        ) as client:
            data = make_pdf(tmp_path)
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            path = "/v1/pdf-intakes/" + prepared.json()["intake_id"]
            assert (
                await client.put(path + "/content", content=data)
            ).status_code == 200
            assert (await client.post(path + "/analyse")).status_code == 200
            assert (
                await client.post(f"/v1/collections/{kb.default_collection_id}/archive")
            ).status_code == 200
            assert (
                await client.post("/v1/pdf-intakes", json=descriptor(kb, len(data)))
            ).status_code == 403
            assert (await client.post(path + "/analyse")).status_code == 403
            # Auth/lifecycle runs before unavailable explicit PDF policy execution.
            submit = await client.post(
                path + "/submit",
                json={"analysis_sha256": "a" * 64, "policy_sha256": "b" * 64},
            )
            assert submit.status_code == 403, submit.text
            assert (
                await client.put(path + "/content", content=data)
            ).status_code == 403
            assert (await client.get(path)).status_code == 200
            assert (await client.delete(path)).status_code == 200
