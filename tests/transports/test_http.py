"""HTTP boundary behavior against real application services."""

import pytest
from httpx import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


@pytest.mark.asyncio
async def test_reader_cannot_add_sources(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        reader = RequestScope(
            principal_id=owner.scope.principal_id,
            collection_ids=frozenset({str(kb.default_collection_id)}),
            access_ceiling=0,
            uses=frozenset({"lookup"}),
            capabilities=frozenset({"read"}),
        )
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"reader-token": reader},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer reader-token"},
        ) as client:
            response = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "x",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "text": "source",
                    "metadata": {"title": "X", "publisher": "Example", "edition": "1"},
                },
            )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_revision_cursor_skips_hidden_first_and_is_scope_bound(tmp_path):
    import base64

    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        reader = owner.scope.model_copy(update={"access_ceiling": 0})
        narrower = reader.model_copy(update={"uses": frozenset({"lookup"})})
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"full": reader, "narrow": narrower},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer full"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "cursor-test",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "Visible source.",
                    "metadata": {
                        "title": "Visible",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            status = await app.state.runtime.services.jobs.wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"

            def seed():
                row = owner.db.execute(
                    "SELECT r.revision_id,r.source_id FROM revisions r "
                    "JOIN sources s USING(source_id) WHERE s.source_key='cursor-test'"
                ).fetchone()
                revision, source_id = row
                hidden = "0" * 64
                owner.db.execute(
                    "INSERT INTO revisions SELECT ?,source_id,kind_id,original_sha,"
                    "'hidden',metadata_sha,title,published_on,effective_from,"
                    "effective_until,3,declared_review_state,allowed_uses_json,"
                    "metadata_json,captured_at FROM revisions WHERE revision_id=?",
                    (hidden, revision),
                )
                owner.db.execute(
                    "INSERT INTO revisions SELECT ?,source_id,kind_id,original_sha,"
                    "'later',metadata_sha,title,published_on,effective_from,"
                    "effective_until,0,declared_review_state,allowed_uses_json,"
                    "metadata_json,captured_at FROM revisions WHERE revision_id=?",
                    ("f" * 64, revision),
                )
                owner.db.execute(
                    "INSERT INTO sources SELECT ?,collection_id,'other-visible',"
                    "source_type,registration_json,access_floor,withdrawn_at,created_at "
                    "FROM sources WHERE source_id=?",
                    ("00000000-0000-4000-8000-000000000099", source_id),
                )
                owner.db.commit()
                return source_id, hidden

            source_id, hidden = await owner.run_blocking(seed, ExecutionContext())
            first = await client.get(f"/v1/sources/{source_id}/revisions?limit=1")
            assert first.status_code == 200, first.text
            assert len(first.json()["revisions"]) == 1
            assert first.json()["revisions"][0]["revision_id"] != hidden
            cursor = first.json()["next_cursor"]
            assert cursor
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            assert hidden.encode() not in decoded
            second = await client.get(
                f"/v1/sources/{source_id}/revisions",
                params={"limit": 1, "cursor": cursor},
            )
            assert second.status_code == 200
            assert second.json()["revisions"][0]["revision_id"] == "f" * 64
            denied = await client.get(
                f"/v1/sources/{source_id}/revisions",
                params={"limit": 1, "cursor": cursor},
                headers={"Authorization": "Bearer narrow"},
            )
            assert denied.status_code == 400
            sources = await client.get("/v1/sources?limit=1")
            assert sources.json()["next_cursor"]
            denied_sources = await client.get(
                "/v1/sources",
                params={"limit": 1, "cursor": sources.json()["next_cursor"]},
                headers={"Authorization": "Bearer narrow"},
            )
            assert denied_sources.status_code == 400
        await app.state.runtime.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_extraction_and_channel_coverage_continue_in_bounded_pages(tmp_path):
    import asyncio

    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"reader": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer reader"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "coverage-test",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "Coverage source.",
                    "metadata": {
                        "title": "Coverage",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            status = await app.state.runtime.services.jobs.wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"

            def seed():
                row = owner.db.execute(
                    "SELECT s.source_id,r.revision_id,e.extraction_id "
                    "FROM sources s JOIN revisions r USING(source_id) "
                    "JOIN extractions e USING(revision_id) "
                    "WHERE s.source_key='coverage-test'"
                ).fetchone()
                source_id, revision_id, original_extraction = row
                for number in range(10):
                    owner.db.execute(
                        "INSERT INTO extractions SELECT ?,revision_id,?,"
                        "normalizer_fingerprint,document_artifact_sha,"
                        "assertions_artifact_sha,diagnostics_artifact_sha,created_at "
                        "FROM extractions WHERE extraction_id=?",
                        (f"{number:064x}", f"{number + 100:064x}", original_extraction),
                    )
                family, batch = owner.db.execute(
                    "SELECT f.family_id,b.batch_id FROM index_families f "
                    "JOIN index_batches b USING(family_id) "
                    "WHERE b.extraction_id=? AND f.channel='lexical'",
                    (original_extraction,),
                ).fetchone()
                for number in range(17):
                    synthetic_family = f"{number + 1000:064x}"
                    owner.db.execute(
                        "INSERT INTO index_families SELECT ?,channel,backend,provider,"
                        "model,dimension,config_sha,config_json,backend_namespace,created_at "
                        "FROM index_families WHERE family_id=?",
                        (synthetic_family, family),
                    )
                    owner.db.execute(
                        "INSERT INTO index_batches SELECT ?,?, ?,input_manifest_sha,"
                        "expected_count,actual_count,entry_ids_sha,state,receipt_json "
                        "FROM index_batches WHERE batch_id=?",
                        (f"{number + 2000:064x}", synthetic_family, f"{0:064x}", batch),
                    )
                owner.db.commit()
                return source_id, revision_id, f"{0:064x}"

            source_id, revision_id, extraction_id = await owner.run_blocking(
                seed, ExecutionContext()
            )
            revision = await client.get(f"/v1/sources/{source_id}/revisions")
            assert revision.status_code == 200, revision.text
            assert revision.json()["revisions"][0]["extractions_truncated"] is True
            path = f"/v1/sources/{source_id}/revisions/{revision_id}/extractions"
            seen = []
            cursor = None
            while True:
                page = await client.get(
                    path, params={"limit": 4, **({"cursor": cursor} if cursor else {})}
                )
                assert page.status_code == 200, page.text
                assert len(page.json()["extractions"]) <= 4
                seen.extend(
                    item["extraction_id"] for item in page.json()["extractions"]
                )
                cursor = page.json()["next_cursor"]
                if cursor is None:
                    break
            assert len(seen) == 11 and len(set(seen)) == 11
            channels_path = path + f"/{extraction_id}/channels"
            channels = []
            cursor = None
            while True:
                page = await client.get(
                    channels_path,
                    params={"limit": 6, **({"cursor": cursor} if cursor else {})},
                )
                assert page.status_code == 200, page.text
                assert len(page.json()["channels"]) <= 6
                channels.extend(page.json()["channels"])
                cursor = page.json()["next_cursor"]
                if cursor is None:
                    break
            assert len(channels) == 17
            assert len({item["family_id"] for item in channels}) == 17
            local = await asyncio.to_thread(
                kb.sources.extractions, source_id, revision_id, limit=4
            )
            assert len(local.extractions) == 4
        await app.state.runtime.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_add_is_durable_and_idempotent_with_scoped_cancel(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        reader = owner.scope.model_copy(update={"capabilities": frozenset({"read"})})
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"writer-token": owner.scope, "reader-token": reader},
                authority="127.0.0.1:8765",
            ),
        )
        source = {
            "type": "text",
            "source_key": "manual",
            "collection_id": "default",
            "kind": "manual@1",
            "format": "text",
            "text": "Record verification outcomes.",
            "metadata": {
                "title": "Verification",
                "publisher": "Example",
                "edition": "1",
            },
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            writer = {"Authorization": "Bearer writer-token", "Idempotency-Key": "one"}
            first = await client.post("/v1/sources", headers=writer, json=source)
            assert first.status_code == 202, first.text
            second = await client.post("/v1/sources", headers=writer, json=source)
            assert second.status_code == 202
            assert second.json()["id"] == first.json()["id"]
            job_id = first.json()["id"]
            listing = await client.get(
                "/v1/jobs", headers={"Authorization": "Bearer writer-token"}
            )
            assert listing.status_code == 200
            assert job_id in {item["id"] for item in listing.json()["jobs"]}
            denied = await client.post(
                f"/v1/jobs/{job_id}/cancel",
                headers={"Authorization": "Bearer reader-token"},
            )
            assert denied.status_code == 403
            status = await client.get(
                f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer writer-token"}
            )
            assert status.status_code == 200
            event = await client.get(
                f"/v1/jobs/{job_id}/events",
                headers={"Authorization": "Bearer writer-token", "Last-Event-ID": "0"},
            )
            assert event.status_code == 200
            assert "id: 1\n" in event.text
            assert "event: job\n" in event.text
            conflict = await client.post(
                "/v1/sources", headers=writer, json={**source, "text": "Different"}
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_raw_gate_rejects_ambiguous_headers_and_streamed_oversize(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
                max_request_bytes=64,
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            for name, values in (
                ("Host", ("127.0.0.1:8765", "evil.invalid")),
                ("Origin", ("http://127.0.0.1:8765", "http://127.0.0.1:8765")),
                ("Authorization", ("Bearer owner-token", "Bearer owner-token")),
            ):
                headers = [
                    ("Authorization", "Bearer owner-token"),
                    ("Host", "127.0.0.1:8765"),
                ]
                headers.extend((name, value) for value in values)
                response = await client.get("/v1/discovery/kinds", headers=headers)
                assert response.status_code == 400, (name, response.text)
                assert response.json()["code"] == "INVALID_REQUEST"
            bad_port = await client.get(
                "/v1/discovery/kinds",
                headers={
                    "Authorization": "Bearer owner-token",
                    "Host": "127.0.0.1:8766",
                },
            )
            assert bad_port.status_code == 403
            assert bad_port.json()["code"] == "FORBIDDEN_SCOPE"

            async def chunks():
                yield b"x" * 40
                yield b"y" * 40

            big = await client.post(
                "/v1/sources",
                headers={"Authorization": "Bearer owner-token"},
                content=chunks(),
            )
            assert big.status_code == 400
            assert big.json()["code"] == "BUDGET_TOO_SMALL"


@pytest.mark.asyncio
async def test_discovery_source_listing_query_and_inspection(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        headers = {"Authorization": "Bearer owner-token"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers=headers,
        ) as client:
            kinds = await client.get("/v1/discovery/kinds")
            assert kinds.status_code == 200
            assert "manual@1" in {item["id"] for item in kinds.json()["definitions"]}
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "Record verification outcomes.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            assert added.status_code == 202
            job_id = added.json()["id"]
            import asyncio

            for _ in range(100):
                status = await client.get(f"/v1/jobs/{job_id}")
                if status.json()["state"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            assert status.json()["state"] == "succeeded", status.text
            sources = await client.get("/v1/sources")
            assert sources.status_code == 200
            item = sources.json()["definitions"][0]
            specific = await client.get(f"/v1/sources/{item['id']}")
            assert specific.status_code == 200
            assert specific.json()["id"] == item["id"]
            query = await client.post(
                "/v1/queries",
                json={"question": "verification", "modalities": ["lexical"]},
            )
            assert query.status_code == 200, query.text
            assert query.json()["segments"]
            unit_id = query.json()["segments"][0]["matched_unit_ids"][0]
            evidence = await client.get(f"/v1/evidence/{unit_id}")
            assert evidence.status_code == 200
            assert (
                evidence.json()["evidence"]["text"] == "Record verification outcomes."
            )


@pytest.mark.asyncio
async def test_writer_sync_and_query_replay_boundaries(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        writer = owner.scope.model_copy(
            update={"capabilities": frozenset({"read", "write"})}
        )
        reader = owner.scope.model_copy(update={"capabilities": frozenset({"read"})})
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"writer-token": writer, "reader-token": reader},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            wh = {"Authorization": "Bearer writer-token"}
            rh = {"Authorization": "Bearer reader-token"}
            added = await client.post(
                "/v1/sources",
                headers=wh,
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic record.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            assert added.status_code == 202
            await kb._workspace.services.jobs_for_scope(writer).wait(
                added.json()["id"],
                __import__(
                    "tychora.ports.execution", fromlist=["ExecutionContext"]
                ).ExecutionContext(),
                timeout=5,
            )
            sources = await client.get("/v1/sources", headers=wh)
            source_id = sources.json()["definitions"][0]["id"]
            denied = await client.post(f"/v1/sources/{source_id}/sync", headers=rh)
            assert denied.status_code == 403
            synced = await client.post(f"/v1/sources/{source_id}/sync", headers=wh)
            assert synced.status_code == 202, synced.text
            query = await client.post(
                "/v1/queries",
                headers=wh,
                json={"question": "synthetic", "modalities": ["lexical"]},
            )
            assert query.status_code == 200
            replay = await client.post(
                f"/v1/queries/{query.json()['run_id']}/replay", headers=wh
            )
            assert replay.status_code == 409
            assert replay.json()["code"] == "REPLAY_UNAVAILABLE"
            unacceptable = await client.post(
                "/v1/queries",
                headers={**wh, "Accept": "text/html"},
                json={"question": "synthetic", "modalities": ["lexical"]},
            )
            assert unacceptable.status_code == 406
            hierarchical = await client.post(
                "/v1/queries",
                headers={**wh, "Accept": "application/vnd.tychora.hierarchical+json"},
                json={"question": "synthetic", "modalities": ["lexical"]},
            )
            assert hierarchical.status_code == 200
            assert hierarchical.json()["schema_version"] == "hierarchical-evidence@1"


@pytest.mark.asyncio
async def test_requested_scope_can_only_narrow_token_and_length_is_strict(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
                max_request_bytes=512,
            ),
        )
        source = {
            "type": "text",
            "source_key": "manual",
            "collection_id": "default",
            "kind": "manual@1",
            "format": "text",
            "text": "A synthetic record.",
            "metadata": {
                "title": "Verification",
                "publisher": "Example",
                "edition": "1",
            },
        }
        narrowed = owner.scope.model_copy(update={"capabilities": frozenset({"read"})})
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            denied = await client.post(
                "/v1/sources",
                json=source,
                headers={
                    "Authorization": "Bearer owner-token",
                    "X-Tychora-Scope": narrowed.model_dump_json(),
                },
            )
            assert denied.status_code == 403
            assert denied.json()["code"] == "FORBIDDEN_SCOPE"
            wrong = narrowed.model_copy(update={"principal_id": "other"})
            rejected = await client.get(
                "/v1/discovery/kinds",
                headers={
                    "Authorization": "Bearer owner-token",
                    "X-Tychora-Scope": wrong.model_dump_json(),
                },
            )
            assert rejected.status_code == 403
            malformed = await client.get(
                "/v1/discovery/kinds",
                headers={
                    "Authorization": "Bearer owner-token",
                    "Content-Length": "bad",
                },
            )
            assert malformed.status_code == 400
            duplicate = await client.get(
                "/v1/discovery/kinds",
                headers=[
                    ("Authorization", "Bearer owner-token"),
                    ("Content-Length", "0"),
                    ("Content-Length", "0"),
                ],
            )
            assert duplicate.status_code == 400

            huge_length = await client.get(
                "/v1/discovery/kinds",
                headers={
                    "Authorization": "Bearer owner-token",
                    "Content-Length": "9" * 5000,
                },
            )
            assert huge_length.status_code == 400
            assert huge_length.json()["code"] == "INVALID_REQUEST"
            huge_event = await client.get(
                "/v1/jobs/unknown/events",
                headers={
                    "Authorization": "Bearer owner-token",
                    "Last-Event-ID": "9" * 5000,
                },
            )
            assert huge_event.status_code == 400
            assert huge_event.json()["code"] == "INVALID_REQUEST"
            overflow_event = await client.get(
                "/v1/jobs/unknown/events",
                headers={
                    "Authorization": "Bearer owner-token",
                    "Last-Event-ID": "9223372036854775808",
                },
            )
            assert overflow_event.status_code == 400
            assert overflow_event.json()["code"] == "INVALID_REQUEST"
            for body in ([], 7, "text"):
                invalid_source = await client.post(
                    "/v1/sources",
                    headers={"Authorization": "Bearer owner-token"},
                    json=body,
                )
                assert invalid_source.status_code == 400
                assert invalid_source.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_settings_activation_persists_and_invalid_candidate_keeps_previous(
    tmp_path,
):
    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        original = (root / "workspace.toml").read_text()
        candidate = original + '\n[configuration.output]\ntitle = "Updated evidence"\n'
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            before = await client.get("/v1/settings")
            assert before.status_code == 200
            changed = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": candidate}
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["config_sha"] != before.json()["config_sha"]
            assert changed.json()["values"]["output"]["title"] == "Updated evidence"
            bad = await client.put(
                "/v1/settings", json={"profile": "missing", "workspace_toml": original}
            )
            assert bad.status_code == 400
            after = await client.get("/v1/settings")
            assert after.json()["config_sha"] == changed.json()["config_sha"]
            assert (root / "workspace.toml").read_text() == candidate
        await app.state.runtime.services.source_jobs.aclose()
    with Tychora.open(root, token_counter=Counter()) as reopened:
        assert reopened._workspace.config.config_sha == changed.json()["config_sha"]


@pytest.mark.asyncio
async def test_nonlocal_profile_selection_survives_restart_and_mismatch_fails(tmp_path):
    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        original = (root / "workspace.toml").read_text()
        candidate = original.replace('profile = "local"', 'profile = "alternate"')
        candidate += '\n[profiles.alternate]\nbuiltin = "documents@1"\n'
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            mismatch = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": candidate}
            )
            assert mismatch.status_code == 400
            assert (root / "workspace.toml").read_text() == original
            activated = await client.put(
                "/v1/settings",
                json={"profile": "alternate", "workspace_toml": candidate},
            )
            assert activated.status_code == 200, activated.text
            assert activated.json()["profile"] == "alternate"
        await app.state.runtime.services.source_jobs.aclose()
    with Tychora.open(root, token_counter=Counter()) as reopened:
        assert reopened._workspace.config.profile == "alternate"
        assert reopened._workspace.config.config_sha == activated.json()["config_sha"]


@pytest.mark.asyncio
async def test_settings_failed_construction_restores_owner_and_current_services(
    tmp_path, monkeypatch
):
    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        previous_config, previous_store = owner.config, owner.store
        previous_services = kb._workspace.services
        app = create_app(
            owner,
            previous_services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        original = (root / "workspace.toml").read_text()
        candidate = original + '\n[configuration.output]\ntitle = "Candidate"\n'

        def fail_construction(*args, **kwargs):
            assert owner.config is not previous_config
            assert owner.store is not previous_store
            raise TychoraError("INVALID_REQUEST", "Candidate construction failed.")

        monkeypatch.setattr(
            "tychora.application.settings.build_services", fail_construction
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            failed = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": candidate}
            )
            assert failed.status_code == 400
            assert owner.config is previous_config
            assert owner.store is previous_store
            assert app.state.runtime.services is previous_services
            assert (root / "workspace.toml").read_text() == original
            assert (await client.get("/v1/discovery/kinds")).status_code == 200


@pytest.mark.asyncio
async def test_settings_committed_activation_survives_old_bridge_close_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        old_services = kb._workspace.services
        app = create_app(
            owner,
            old_services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        original = (root / "workspace.toml").read_text()
        candidate = original + '\n[configuration.output]\ntitle = "Committed"\n'

        async def failed_close():
            raise RuntimeError("private cleanup detail")

        original_close = old_services.source_jobs.close

        def failed_sync_close():
            raise RuntimeError("another private cleanup detail")

        monkeypatch.setattr(old_services.source_jobs, "aclose", failed_close)
        monkeypatch.setattr(old_services.source_jobs, "close", failed_sync_close)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            result = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": candidate}
            )
            assert result.status_code == 200
            assert result.json()["config_sha"] == owner.config.config_sha
            assert app.state.runtime.services is not old_services
            assert (root / "workspace.toml").read_text() == candidate
            assert (await client.get("/v1/settings")).json()[
                "config_sha"
            ] == result.json()["config_sha"]
            assert app.state.runtime.cleanup_diagnostics == (
                "previous source scheduler cleanup failed",
            )
            assert app.state.runtime.retired_count == 1
        monkeypatch.setattr(old_services.source_jobs, "close", original_close)
        await app.state.runtime.services.source_jobs.aclose()
    assert app.state.runtime.retired_count == 0


@pytest.mark.asyncio
async def test_settings_post_commit_cancellation_finishes_cleanup_and_returns_success(
    tmp_path, monkeypatch
):
    import asyncio

    from tychora.contracts.operations import SettingsActivation

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        old_services = kb._workspace.services
        app = create_app(
            owner,
            old_services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        candidate = (root / "workspace.toml").read_text()
        candidate += '\n[configuration.output]\ntitle = "Cancellation"\n'
        reached, release = asyncio.Event(), asyncio.Event()
        original_close = old_services.source_jobs.aclose

        async def held_close():
            reached.set()
            await release.wait()
            await original_close()

        monkeypatch.setattr(old_services.source_jobs, "aclose", held_close)
        app.state.runtime.admit(activation=True)
        try:
            activation = asyncio.create_task(
                app.state.settings.activate(
                    SettingsActivation(profile="local", workspace_toml=candidate)
                )
            )
            await asyncio.wait_for(reached.wait(), 3)
            activation.cancel()
            release.set()
            result = await activation
            assert result.config_sha == owner.config.config_sha
            assert (root / "workspace.toml").read_text() == candidate
            assert app.state.runtime.services is not old_services
        finally:
            app.state.runtime.release(activation=True)
        await app.state.runtime.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_settings_cancellation_during_cleanup_fallback_is_committed_success(
    tmp_path, monkeypatch
):
    import asyncio
    from threading import Event

    from tychora.contracts.operations import SettingsActivation

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        old_services = kb._workspace.services
        app = create_app(
            owner,
            old_services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        candidate = (root / "workspace.toml").read_text()
        candidate += '\n[configuration.output]\ntitle = "Fallback"\n'
        reached, release = Event(), Event()
        original_close = old_services.source_jobs.close

        async def failed_async_close():
            raise RuntimeError("private cleanup failure")

        def held_sync_close():
            reached.set()
            release.wait()
            original_close()

        monkeypatch.setattr(old_services.source_jobs, "aclose", failed_async_close)
        monkeypatch.setattr(old_services.source_jobs, "close", held_sync_close)
        app.state.runtime.admit(activation=True)
        try:
            activation = asyncio.create_task(
                app.state.settings.activate(
                    SettingsActivation(profile="local", workspace_toml=candidate)
                )
            )
            assert await asyncio.to_thread(reached.wait, 3)
            activation.cancel()
            release.set()
            result = await activation
            assert result.config_sha == owner.config.config_sha
            assert app.state.runtime.retired_count == 0
            assert (root / "workspace.toml").read_text() == candidate
        finally:
            release.set()
            app.state.runtime.release(activation=True)
        await app.state.runtime.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_indexed_settings_allow_credential_and_rerank_but_reject_representation(
    tmp_path,
):
    from tychora.ports.execution import ExecutionContext

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "indexed",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "An indexed synthetic record.",
                    "metadata": {
                        "title": "Indexed",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            status = await app.state.runtime.services.jobs.wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"
            original = (root / "workspace.toml").read_text()
            safe = original + (
                '\n[configuration.providers.text]\ncredential_ref = "env:ROTATED"\n'
                '[configuration.providers.rerank]\nprovider = "voyage"\n'
                'model = "rerank-2.5"\ncredential_ref = "env:RERANK"\n'
            )
            changed = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": safe}
            )
            assert changed.status_code == 200, changed.text
            for suffix in (
                "\n[configuration.providers.image]\ndimensions = 777\n",
                '\n[configuration.index]\ngraph = "ladybug"\n',
            ):
                incompatible = await client.put(
                    "/v1/settings",
                    json={"profile": "local", "workspace_toml": safe + suffix},
                )
                assert incompatible.status_code == 409, incompatible.text
                assert incompatible.json()["code"] == "PROFILE_INCOMPATIBLE"
            assert (root / "workspace.toml").read_text() == safe
        await app.state.runtime.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_settings_activation_rejects_active_accepted_job(tmp_path):
    import asyncio
    from threading import Event

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        started, release = Event(), Event()
        original_extract = services.sources._extract

        async def held(*args):
            started.set()
            await asyncio.to_thread(release.wait)
            return await original_extract(*args)

        services.sources._extract = held
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer owner-token"},
            ) as client:
                added = await client.post(
                    "/v1/sources",
                    json={
                        "type": "text",
                        "source_key": "manual",
                        "collection_id": "default",
                        "kind": "manual@1",
                        "format": "text",
                        "text": "A synthetic record.",
                        "metadata": {
                            "title": "Verification",
                            "publisher": "Example",
                            "edition": "1",
                        },
                    },
                )
                assert added.status_code == 202
                assert await asyncio.to_thread(started.wait, 3)
                previous = (root / "workspace.toml").read_text()
                busy = await client.put(
                    "/v1/settings",
                    json={"profile": "local", "workspace_toml": previous},
                )
                assert busy.status_code == 409
                assert busy.json()["code"] == "WORKSPACE_BUSY"
                assert (root / "workspace.toml").read_text() == previous
        finally:
            release.set()
        status = await services.jobs_for_scope(owner.scope).wait(
            added.json()["id"],
            __import__(
                "tychora.ports.execution", fromlist=["ExecutionContext"]
            ).ExecutionContext(),
            timeout=5,
        )
        assert status.state == "succeeded"


@pytest.mark.asyncio
async def test_retry_resumes_retained_transient_source_and_denies_manage_job(tmp_path):
    from tychora.contracts.errors import TychoraError
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        writer = owner.scope.model_copy(
            update={"capabilities": frozenset({"read", "write"})}
        )
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope, "writer-token": writer},
                authority="127.0.0.1:8765",
            ),
        )
        original_extract = services.sources._extract
        attempts = 0

        async def flaky(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TychoraError(
                    "PROVIDER_FAILURE", "Synthetic transient failure.", retryable=True
                )
            return await original_extract(*args)

        services.sources._extract = flaky
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            writer_headers = {"Authorization": "Bearer writer-token"}
            added = await client.post(
                "/v1/sources",
                headers=writer_headers,
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic record.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            assert added.status_code == 202
            job_id = added.json()["id"]
            failed = await services.jobs_for_scope(writer).wait(
                job_id, ExecutionContext(), timeout=5
            )
            assert failed.state == "failed"
            retried = await client.post(
                f"/v1/jobs/{job_id}/retry", headers=writer_headers
            )
            assert retried.status_code == 202, retried.text
            done = await services.jobs_for_scope(writer).wait(
                job_id, ExecutionContext(), timeout=5
            )
            assert done.state == "succeeded", (
                done.error_code,
                tuple(
                    await services.jobs_for_scope(writer).events_after(
                        job_id, 0, 100, ExecutionContext()
                    )
                ),
            )

            config_ref = await services.sources._config_ref(ExecutionContext())
            managed = await services.jobs.accept(
                "setup", None, {}, config_ref, ExecutionContext()
            )
            denied = await client.post(
                f"/v1/jobs/{managed.id}/retry", headers=writer_headers
            )
            assert denied.status_code == 403
            assert denied.json()["code"] == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_denied_cancel_does_not_signal_active_source(tmp_path):
    import asyncio
    from threading import Event

    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        reader = owner.scope.model_copy(update={"capabilities": frozenset({"read"})})
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"writer-token": owner.scope, "reader-token": reader},
                authority="127.0.0.1:8765",
            ),
        )
        started, release = Event(), Event()
        original = services.sources._extract

        async def held(*args):
            started.set()
            await asyncio.to_thread(release.wait)
            return await original(*args)

        services.sources._extract = held
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                added = await client.post(
                    "/v1/sources",
                    headers={"Authorization": "Bearer writer-token"},
                    json={
                        "type": "text",
                        "source_key": "manual",
                        "collection_id": "default",
                        "kind": "manual@1",
                        "format": "text",
                        "text": "A synthetic record.",
                        "metadata": {
                            "title": "Verification",
                            "publisher": "Example",
                            "edition": "1",
                        },
                    },
                )
                assert added.status_code == 202
                assert await asyncio.to_thread(started.wait, 3)
                job_id = added.json()["id"]
                denied = await client.post(
                    f"/v1/jobs/{job_id}/cancel",
                    headers={"Authorization": "Bearer reader-token"},
                )
                assert denied.status_code == 403
                running = await services.jobs_for_scope(owner.scope).get(
                    job_id, ExecutionContext()
                )
                assert running.state == "running"
                assert not running.cancellation_requested
        finally:
            release.set()
        done = await services.jobs_for_scope(owner.scope).wait(
            job_id, ExecutionContext(), timeout=5
        )
        assert done.state == "succeeded"


@pytest.mark.asyncio
async def test_expired_sse_cursor_requests_authoritative_refresh(tmp_path):
    from datetime import UTC, datetime, timedelta

    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic record.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            job_id = added.json()["id"]
            await services.jobs_for_scope(owner.scope).wait(
                job_id, ExecutionContext(), timeout=5
            )
            await owner.run_blocking(
                lambda: services.jobs.repository.expire_diagnostics(
                    datetime.now(UTC) + timedelta(seconds=1)
                ),
                ExecutionContext(),
            )
            events = await client.get(
                f"/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "0"}
            )
            assert events.status_code == 200
            assert "event: refresh\n" in events.text
            status = await client.get(f"/v1/jobs/{job_id}")
            assert status.json()["retention_limited"] is True


@pytest.mark.asyncio
async def test_local_file_source_requires_registered_root(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        source_path = tmp_path / "outside.txt"
        source_path.write_text("Synthetic")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            denied = await client.post(
                "/v1/sources",
                json={
                    "type": "file",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "path": str(source_path),
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            assert denied.status_code == 403
            assert denied.json()["code"] == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_settings_rejects_missing_retained_kind_and_preserves_active_profile(
    tmp_path,
):
    import shutil
    from pathlib import Path

    import tychora
    from tychora.ports.execution import ExecutionContext

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic record.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            assert added.status_code == 202
            status = await services.jobs_for_scope(owner.scope).wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"
            before = (root / "workspace.toml").read_text()
            active_sha = owner.config.config_sha
            source_pack = Path(tychora.__file__).parent / "profiles" / "documents"
            custom = root / "custom"
            shutil.copytree(source_pack, custom)
            profile_file = custom / "profile.toml"
            modified = (
                profile_file.read_text()
                .replace('id = "documents"', 'id = "incompatible"', 1)
                .replace(', "kinds/manual.toml"', "")
                .replace(', "manual@1"', "")
            )
            profile_file.write_text(modified)
            candidate = (
                before.replace('profile = "local"', 'profile = "incompatible"')
                + '\n[profiles.incompatible]\npath = "custom"\n'
            )
            rejected = await client.put(
                "/v1/settings",
                json={"profile": "incompatible", "workspace_toml": candidate},
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "PROFILE_INCOMPATIBLE"
            assert owner.config.config_sha == active_sha
            assert (root / "workspace.toml").read_text() == before
            kinds = await client.get("/v1/discovery/kinds")
            assert "manual@1" in {item["id"] for item in kinds.json()["definitions"]}


@pytest.mark.asyncio
async def test_invalid_route_query_uses_sanitized_400(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            invalid = await client.get("/v1/discovery/kinds?limit=not-a-number")
            assert invalid.status_code == 400
            assert invalid.json()["code"] == "INVALID_REQUEST"
            assert len(invalid.content) < 8192
            bad_timeout = await client.post(
                "/v1/queries?timeout=-1",
                json={"question": "synthetic", "modalities": ["lexical"]},
            )
            assert bad_timeout.status_code == 400
            assert bad_timeout.json()["code"] == "INVALID_REQUEST"


def test_app_lifespan_drains_jobs_before_borrowed_owner_closes(tmp_path):
    from fastapi.testclient import TestClient

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        with TestClient(
            app,
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            assert client.get("/v1/handshake").status_code == 200
        # The app drained its service loop before the borrowed owner released storage.
        from tychora.ports.execution import ExecutionContext

        assert (
            owner.run_blocking_sync(
                lambda: owner.db.execute(
                    "SELECT workspace_id FROM workspaces"
                ).fetchone()[0],
                ExecutionContext(),
            )
            == owner.workspace_id
        )


@pytest.mark.asyncio
async def test_settings_activation_rejects_inflight_query(tmp_path):
    import asyncio

    from tychora.ports.execution import ExecutionContext

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic record.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            await services.jobs_for_scope(owner.scope).wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            started, release = asyncio.Event(), asyncio.Event()
            original = services.query._prepare

            async def held(*args, **kwargs):
                started.set()
                await release.wait()
                return await original(*args, **kwargs)

            services.query._prepare = held
            request = asyncio.create_task(
                client.post(
                    "/v1/queries",
                    json={"question": "synthetic", "modalities": ["lexical"]},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 3)
                previous = (root / "workspace.toml").read_text()
                busy = await client.put(
                    "/v1/settings",
                    json={"profile": "local", "workspace_toml": previous},
                )
                assert busy.status_code == 409
                assert busy.json()["code"] == "WORKSPACE_BUSY"
            finally:
                release.set()
            result = await request
            assert result.status_code == 200, result.text


@pytest.mark.asyncio
async def test_settings_gate_counts_held_public_python_query(tmp_path):
    import asyncio
    from threading import Event

    from tychora.ports.execution import ExecutionContext

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        source = {
            "type": "text",
            "source_key": "python-query",
            "collection_id": "default",
            "kind": "manual@1",
            "format": "text",
            "text": "A synthetic Python query source.",
            "metadata": {"title": "Python", "publisher": "Example", "edition": "1"},
        }
        added = await asyncio.to_thread(kb.sources.add, source)
        await services.jobs.wait(added.id, ExecutionContext(), timeout=5)
        started, release = Event(), Event()
        original_prepare = services.query._prepare

        async def held(*args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait)
            return await original_prepare(*args, **kwargs)

        services.query._prepare = held
        searching = asyncio.create_task(
            asyncio.to_thread(
                kb.search, {"question": "synthetic", "modalities": ["lexical"]}
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer owner-token"},
            ) as client:
                activation = asyncio.create_task(
                    client.put(
                        "/v1/settings",
                        json={
                            "profile": "local",
                            "workspace_toml": (root / "workspace.toml").read_text(),
                        },
                    )
                )
                await asyncio.sleep(0.1)
                release.set()
                busy = await activation
                assert busy.status_code == 409
                assert busy.json()["code"] == "WORKSPACE_BUSY"
        finally:
            release.set()
        assert (await searching).segments


@pytest.mark.asyncio
async def test_settings_final_idle_check_excludes_public_python_submission(
    tmp_path, monkeypatch
):
    import asyncio

    from tychora import AsyncTychora

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        async_kb = AsyncTychora(kb._workspace)
        owner = kb._workspace.owner
        old_services = kb._workspace.services
        app = create_app(
            owner,
            old_services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        reached, release = asyncio.Event(), asyncio.Event()
        original_idle = old_services.source_jobs.is_idle_async
        calls = 0

        async def held_idle():
            nonlocal calls
            calls += 1
            if calls == 2:
                reached.set()
                await release.wait()
            return await original_idle()

        monkeypatch.setattr(old_services.source_jobs, "is_idle_async", held_idle)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            activation = asyncio.create_task(
                client.put(
                    "/v1/settings",
                    json={
                        "profile": "local",
                        "workspace_toml": (root / "workspace.toml").read_text(),
                    },
                )
            )
            try:
                await asyncio.wait_for(reached.wait(), 3)
                source = {
                    "type": "text",
                    "source_key": "racing-submission",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic racing source.",
                    "metadata": {
                        "title": "Race",
                        "publisher": "Example",
                        "edition": "1",
                    },
                }
                with pytest.raises(TychoraError) as error:
                    await asyncio.to_thread(kb.sources.add, source)
                assert error.value.code == "WORKSPACE_BUSY"
                with pytest.raises(TychoraError) as async_error:
                    await async_kb.sources.add(source)
                assert async_error.value.code == "WORKSPACE_BUSY"
            finally:
                release.set()
            result = await activation
            assert result.status_code == 200, result.text
            accepted = await asyncio.to_thread(kb.sources.add, source)
            assert accepted.state == "queued"
            from tychora.ports.execution import ExecutionContext

            status = await kb._workspace.services.jobs.wait(
                accepted.id, ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"
            assert (await async_kb.discover({"subject": "kinds"})).definitions


@pytest.mark.asyncio
async def test_source_and_revision_history_use_bounded_database_pages(tmp_path):
    from tychora.ports.execution import ExecutionContext

    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "history",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic source with bounded history.",
                    "metadata": {
                        "title": "History",
                        "publisher": "Example",
                        "edition": "1",
                    },
                },
            )
            status = await app.state.runtime.services.jobs.wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert status.state == "succeeded"

            def seed_history():
                source_id, revision_id = owner.db.execute(
                    "SELECT s.source_id,r.revision_id FROM sources s "
                    "JOIN revisions r USING(source_id) WHERE s.source_key='history'"
                ).fetchone()
                for number in range(30):
                    owner.db.execute(
                        "INSERT INTO revisions "
                        "SELECT ?,source_id,kind_id,original_sha,?,metadata_sha,title,"
                        "published_on,effective_from,effective_until,access_rank,"
                        "declared_review_state,allowed_uses_json,metadata_json,captured_at "
                        "FROM revisions WHERE revision_id=?",
                        (f"{number:064x}", f"edition-{number}", revision_id),
                    )
                    owner.db.execute(
                        "INSERT INTO sources "
                        "SELECT ?,collection_id,?,source_type,registration_json,"
                        "access_floor,withdrawn_at,created_at FROM sources WHERE source_id=?",
                        (
                            f"00000000-0000-4000-8000-{number + 10:012d}",
                            f"synthetic-{number}",
                            source_id,
                        ),
                    )
                owner.db.commit()
                return source_id

            source_id = await owner.run_blocking(seed_history, ExecutionContext())
            statements = []
            await owner.run_blocking(
                lambda: owner.db.set_trace_callback(statements.append),
                ExecutionContext(),
            )
            try:
                first = await client.get("/v1/sources?limit=2")
                assert first.status_code == 200, first.text
                assert len(first.json()["definitions"]) == 2
                assert first.json()["next_cursor"]
                second = await client.get(
                    "/v1/sources",
                    params={"limit": 2, "cursor": first.json()["next_cursor"]},
                )
                assert second.status_code == 200
                assert {item["id"] for item in first.json()["definitions"]}.isdisjoint(
                    {item["id"] for item in second.json()["definitions"]}
                )
                revisions = await client.get(
                    f"/v1/sources/{source_id}/revisions?limit=2"
                )
                assert revisions.status_code == 200, revisions.text
                assert len(revisions.json()["revisions"]) == 2
                assert revisions.json()["next_cursor"]
                next_revisions = await client.get(
                    f"/v1/sources/{source_id}/revisions",
                    params={"limit": 2, "cursor": revisions.json()["next_cursor"]},
                )
                assert next_revisions.status_code == 200
                assert len(next_revisions.json()["revisions"]) == 2
                assert {
                    r["revision_id"] for r in revisions.json()["revisions"]
                }.isdisjoint(
                    {r["revision_id"] for r in next_revisions.json()["revisions"]}
                )
                assert len(first.content) < 1048576
                assert len(revisions.content) < 1048576
            finally:
                await owner.run_blocking(
                    lambda: owner.db.set_trace_callback(None), ExecutionContext()
                )
            source_queries = [
                statement for statement in statements if "FROM sources s" in statement
            ]
            revision_queries = [
                statement
                for statement in statements
                if "FROM revisions r" in statement and "JOIN kinds k" in statement
            ]
            assert source_queries and all("LIMIT" in q for q in source_queries)
            assert revision_queries and all("LIMIT" in q for q in revision_queries)
            await owner.run_blocking(
                lambda: owner.db.execute(
                    "UPDATE sources SET source_key=? WHERE source_id=?",
                    ("x" * 1048576, source_id),
                ),
                ExecutionContext(),
            )
            too_large = await client.get(f"/v1/sources/{source_id}")
            assert too_large.status_code == 400
            assert too_large.json()["code"] == "BUDGET_TOO_SMALL"
