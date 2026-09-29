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

        def fail_construction(*args):
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
            candidate = before + '\n[profiles.incompatible]\npath = "custom"\n'
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
