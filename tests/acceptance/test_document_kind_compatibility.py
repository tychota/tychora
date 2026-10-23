"""Real old/new document journeys preserve immutable retained evidence."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text):
        return len(text) // 4


def legacy_workspace(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "workspace.toml").write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="documents@1"\n[defaults]\nprofile="local"\n'
    )
    return root


async def identities(owner):
    return await owner.run_blocking(
        lambda: {
            table: owner.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "sources",
                "revisions",
                "extractions",
                "evidence_units",
                "chunks",
            )
        },
        ExecutionContext(),
    )


@pytest.mark.asyncio
async def test_upgrade_reuses_legacy_extraction_and_adds_title_only_document(tmp_path):
    root = legacy_workspace(tmp_path)
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        old_config = owner.config
        from tychora.adapters.sqlite.history import HistoryRepository

        kb._workspace.services.query.history = HistoryRepository(
            owner.db, artifacts=owner.artifacts, retain_payloads=True
        )
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            source = {
                "type": "text",
                "source_key": "legacy",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "markdown",
                "text": "# Legacy manual\n\nRetain the cobalt calibration record.\n",
                "metadata": {"title": "Legacy", "publisher": "Example", "edition": "1"},
            }

            async def add(value):
                response = await client.post("/v1/sources", json=value)
                assert response.status_code == 202, response.text
                status = await app.state.runtime.services.jobs.wait(
                    response.json()["id"], ExecutionContext(), timeout=5
                )
                assert status.state == "succeeded", status

            await add(source)
            baseline_query = await client.post(
                "/v1/queries", json={"question": "cobalt", "modalities": ["lexical"]}
            )
            assert baseline_query.status_code == 200, baseline_query.text
            assert baseline_query.json()["citations"]
            before = await identities(owner)
            original_snapshot = await owner.run_blocking(
                owner.store.active_snapshot_id, ExecutionContext()
            )
            candidate = (
                (root / "workspace.toml")
                .read_text()
                .replace("documents@1", "general-documents@1")
            )
            response = await client.put(
                "/v1/settings", json={"profile": "local", "workspace_toml": candidate}
            )
            assert response.status_code == 200, response.text
            assert await identities(owner) == before
            assert (
                await owner.run_blocking(
                    owner.store.active_snapshot_id, ExecutionContext()
                )
                == original_snapshot
            )
            assert (
                old_config.extraction_fingerprint != owner.config.extraction_fingerprint
            )
            replay = await client.post(
                f"/v1/queries/{baseline_query.json()['run_id']}/replay"
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["citations"] == baseline_query.json()["citations"]
            for unit in before["evidence_units"]:
                inspected = await client.get(f"/v1/evidence/{unit[0]}")
                assert inspected.status_code == 200, inspected.text
            old_source, old_revision, old_extraction = (
                before["sources"][0][0],
                before["revisions"][0][0],
                before["extractions"][0][0],
            )
            for subject in ("units", "windows"):
                inspected = await client.get(
                    f"/v1/sources/{old_source}/revisions/{old_revision}/extractions/{old_extraction}/{subject}"
                )
                assert inspected.status_code == 200, inspected.text
                assert "cobalt" in inspected.text
            await add(source)
            assert await identities(owner) == before
            synced = await client.post(f"/v1/sources/{old_source}/sync")
            assert synced.status_code == 202, synced.text
            sync_status = await app.state.runtime.services.jobs.wait(
                synced.json()["id"], ExecutionContext(), timeout=5
            )
            assert sync_status.state == "succeeded", sync_status
            assert await identities(owner) == before
            form = await client.get("/v1/discovery/kinds/document@1/metadata-schema")
            assert form.status_code == 200, form.text
            assert {f["name"] for f in form.json()["fields"] if f["required"]} == {
                "title"
            }
            await add(
                {
                    **source,
                    "source_key": "document",
                    "kind": "document@1",
                    "metadata": {"title": "Calibration notes"},
                    "text": "# Cobalt notes\n\nKeep the cobalt calibration observation.\n",
                }
            )
            after = await identities(owner)
            for table, rows in before.items():
                assert all(row in after[table] for row in rows)
            assert len(after["extractions"]) == 2
            mixed = await client.post(
                "/v1/queries", json={"question": "cobalt", "modalities": ["lexical"]}
            )
            assert mixed.status_code == 200, mixed.text
            assert {c["source_ref"] for c in mixed.json()["citations"]} == {
                "legacy",
                "document",
            }
            from tychora.application.backup import BackupService

            archive = tmp_path / "archive"
            await asyncio.to_thread(
                lambda: BackupService(owner).export(
                    archive, kb.scope, ctx=ExecutionContext()
                ),
            )
        await app.state.runtime.services.source_jobs.aclose()

    def restore_and_query():
        from tychora.application.restore import RestoreService

        RestoreService().import_backup(
            archive, tmp_path / "restored", None, ExecutionContext()
        )
        with Tychora.open(tmp_path / "restored", token_counter=Counter()) as restored:
            result = restored.search({"question": "cobalt", "modalities": ["lexical"]})
            assert {c.source_ref for c in result.citations} == {"legacy", "document"}
            reused = restored.sources.add(source)
            assert restored.jobs.wait(reused.id, timeout=5).state == "succeeded"
            restored_owner = restored._workspace.owner
            return restored_owner.run_blocking_sync(
                lambda: {
                    table: restored_owner.db.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    ).fetchall()
                    for table in after
                },
                ExecutionContext(),
            )

    assert await asyncio.to_thread(restore_and_query) == after


@pytest.fixture
def retained_case(tmp_path):
    import json

    from tychora.contracts.extraction import ExtractionArtifact
    from tychora.contracts.identity import canonical_bytes

    root = legacy_workspace(tmp_path)
    with Tychora.open(root, token_counter=Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "text": "Keep original cobalt records.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {"title": "Old", "publisher": "Example", "edition": "1"},
                "source_key": "old",
                "collection_id": "default",
            }
        )
        assert kb.jobs.wait(handle.id, timeout=5).state == "succeeded"
        owner = kb._workspace.owner

        def retained():
            extraction, sha = owner.db.execute(
                "SELECT extraction_id,document_artifact_sha FROM extractions"
            ).fetchone()
            from tychora.contracts.artifacts import ArtifactRef

            row = owner.db.execute(
                "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
                (sha,),
            ).fetchone()
            ref = ArtifactRef(
                sha256=sha, size_bytes=row[0], media_type=row[1], storage_key=row[2]
            )
            with owner.artifacts.open_verified(ref) as stream:
                data = json.load(stream)["data"]
            parsed = ExtractionArtifact.model_validate_json(
                canonical_bytes(
                    {**data, "raw_document_ref": ref.model_dump(mode="json")}
                )
            )
            return extraction, parsed.registration.capture

        extraction, capture = owner.run_blocking_sync(retained, ExecutionContext())
        yield kb, extraction, capture


def test_retained_config_lookup_uses_exact_capture_or_extraction_lineage(retained_case):
    kb, extraction, capture = retained_case
    owner = kb._workspace.owner
    service = kb._workspace.services.sources

    def verify():
        assert (
            service._retained_extraction_config(capture, "not-a-member") == owner.config
        )
        owner.db.execute("UPDATE jobs SET captured_inputs_sha=NULL")
        assert service._retained_extraction_config(capture, extraction) == owner.config
        from tychora.contracts.errors import TychoraError

        with pytest.raises(TychoraError) as err:
            service._retained_extraction_config(capture, "not-a-member")
        assert err.value.code == "INTEGRITY_ERROR"
        with pytest.raises(TychoraError) as err:
            service._retained_extraction_config(
                capture.model_copy(update={"config_sha": "f" * 64}), extraction
            )
        assert err.value.code == "INTEGRITY_ERROR"

    owner.run_blocking_sync(verify, ExecutionContext())


def test_retained_config_lookup_rejects_byte_budget_before_payload_read(retained_case):
    from tychora.contracts.errors import TychoraError

    kb, extraction, capture = retained_case
    owner = kb._workspace.owner

    def verify():
        owner.db.execute(
            "UPDATE artifacts SET size_bytes=? WHERE artifact_sha IN (SELECT effective_config_sha FROM jobs)",
            (16 * 1024 * 1024 + 1,),
        )
        with pytest.raises(TychoraError) as err:
            kb._workspace.services.sources._retained_extraction_config(
                capture, extraction
            )
        assert err.value.code == "BUDGET_TOO_SMALL"

    owner.run_blocking_sync(verify, ExecutionContext())


def test_retained_config_lookup_rejects_65_distinct_exact_lineage_refs(retained_case):
    from tychora.contracts.errors import TychoraError

    kb, extraction, capture = retained_case
    owner = kb._workspace.owner

    def verify():
        for i in range(65):
            sha = f"{i:064x}"
            owner.db.execute(
                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,1,'application/json',?,'2026-01-01')",
                (sha, sha),
            )
            owner.db.execute(
                "INSERT INTO jobs(job_id,operation,principal_id,request_digest,effective_config_sha,captured_inputs_sha,state,created_at,updated_at) VALUES (?,'add','test',?,?,?,'complete','2026-01-01','2026-01-01')",
                (f"lineage-{i}", sha, sha, capture.manifest_ref.sha256),
            )
        with pytest.raises(TychoraError) as err:
            kb._workspace.services.sources._retained_extraction_config(
                capture, extraction
            )
        assert err.value.code == "BUDGET_TOO_SMALL"

    owner.run_blocking_sync(verify, ExecutionContext())


def test_new_workspace_accepts_title_only_plain_text_with_exact_locators(tmp_path):
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        original = "Literal # heading\nKeep cobalt observations.\n"
        handle = kb.sources.add(
            {
                "type": "text",
                "text": original,
                "format": "text",
                "kind": "document@1",
                "metadata": {"title": "Notes"},
                "source_key": "notes",
                "collection_id": "default",
            }
        )
        assert kb.jobs.wait(handle.id, timeout=5).state == "succeeded"
        result = kb.search({"question": "cobalt", "modalities": ["lexical"]})
        assert result.citations
        owner = kb._workspace.owner

        def verify():
            units = owner.store.hydrate(
                tuple(
                    row[0]
                    for row in owner.db.execute("SELECT unit_id FROM evidence_units")
                )
            )
            assert units
            for unit in units:
                for loc in unit.locators:
                    assert loc.type == "text"
                    assert original[loc.char_start : loc.char_end] == unit.text

        owner.run_blocking_sync(verify, ExecutionContext())


def test_retained_config_lookup_verifies_recorded_config_hashes(retained_case):
    from io import BytesIO

    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes

    kb, extraction, capture = retained_case
    owner = kb._workspace.owner

    def verify():
        raw = owner.config.model_dump(mode="json")
        raw["values"]["extraction"]["representation_version"] = "tampered"
        ref = owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {"type": "registry", "schema_version": "1", "data": raw}
                )
            ),
            "application/json",
        )
        owner.db.execute(
            "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,'2026-01-01')",
            (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key),
        )
        owner.db.execute("UPDATE jobs SET effective_config_sha=?", (ref.sha256,))
        with pytest.raises(TychoraError) as err:
            kb._workspace.services.sources._retained_extraction_config(
                capture, extraction
            )
        assert err.value.code == "INTEGRITY_ERROR"

    owner.run_blocking_sync(verify, ExecutionContext())
