"""Archive changes retrieval eligibility without destroying retained evidence."""

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.publication import members_of
from tests.helpers.timing import scaled
from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_archive_blocks_work_hides_cached_query_and_retains_inspection(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        app = create_app(
            owner,
            services,
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
            source = {
                "type": "text",
                "source_key": "calibration",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# Calibration\n\nCobalt calibration reference.\n",
                "metadata": {"title": "Calibration"},
            }
            response = await client.post("/v1/sources", json=source)
            job = await services.jobs.wait(
                response.json()["id"], ExecutionContext(), timeout=5
            )
            assert job.state == "succeeded", job
            query = {"question": "cobalt", "modalities": ["lexical"]}
            before = await client.post("/v1/queries", json=query)
            assert before.json()["citations"]
            unit = await owner.run_write(
                lambda: owner.db.execute(
                    "SELECT unit_id FROM evidence_units LIMIT 1"
                ).fetchone()[0],
                ExecutionContext(),
            )
            cid = str(kb.default_collection_id)
            assert (
                await client.post(f"/v1/collections/{cid}/archive")
            ).status_code == 200
            denied = await client.post("/v1/sources", json=source)
            assert denied.status_code == 403, denied.text
            after = await client.post("/v1/queries", json=query)
            assert after.status_code == 200, after.text
            assert not after.json()["citations"]
            from tychora.contracts.operations import InspectRequest

            inspected = await services.inspection.inspect(
                InspectRequest(subject="evidence", id=unit),
                kb.scope,
                ExecutionContext(),
            )
            assert inspected
            assert (await client.get(f"/v1/jobs/{job.id}")).status_code == 200
            assert (await client.get("/v1/documents")).json()["documents"] == []
            assert (
                await client.post(f"/v1/collections/{cid}/reactivate")
            ).status_code == 200
            restored = await client.post("/v1/queries", json=query)
            assert restored.json()["citations"] == before.json()["citations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "rename"])
async def test_captured_bundle_cannot_overwrite_current_collection(
    tmp_path, monkeypatch, change
):
    import asyncio
    from threading import Event

    from tychora.contracts.collections import CollectionCreate, CollectionEdit

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        collection = await services.collections.create(
            CollectionCreate(title="Before", slug="before"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
        original = services.sources._extract
        entered, release = Event(), Event()

        async def paused(*args, **kwargs):
            bundle = await original(*args, **kwargs)
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            return bundle

        monkeypatch.setattr(services.sources, "_extract", paused)
        source = {
            "type": "text",
            "source_key": "synthetic",
            "collection_id": collection.collection_id,
            "kind": "document@1",
            "format": "markdown",
            "text": "# Synthetic\n\nCalibration evidence.",
            "metadata": {"title": "Synthetic"},
        }
        job = kb.sources.add(source)
        assert await asyncio.to_thread(entered.wait, 10)
        try:
            if change == "archive":
                await services.collections.set_archived(
                    collection.collection_id, True, kb.scope, ExecutionContext()
                )
            else:
                await services.collections.edit(
                    collection.collection_id,
                    CollectionEdit(title="After", slug="after"),
                    kb.scope,
                    ExecutionContext(),
                )

                # Canonical consent may change during capture even with no remote dispatch.
                def consent_change():
                    from tychora.configuration.collection_policy import (
                        parse_collection_policy,
                    )

                    raw = owner.db.execute(
                        "SELECT policy_json FROM collections WHERE collection_id=?",
                        (collection.collection_id,),
                    ).fetchone()[0]
                    policy = parse_collection_policy(raw).model_copy(
                        update={
                            "upload_providers": ("voyage",),
                            "owner_upload_providers": ("voyage",),
                        }
                    )
                    owner.db.execute(
                        "UPDATE collections SET policy_json=? WHERE collection_id=?",
                        (policy.model_dump_json(), collection.collection_id),
                    )
                    owner.db.execute(
                        "UPDATE workspaces SET policy_epoch=policy_epoch+1"
                    )
                    owner.db.commit()

                await owner.run_write(consent_change, ExecutionContext())
        finally:
            release.set()
        result = await services.jobs.wait(
            job.id, ExecutionContext(), timeout=scaled(10)
        )
        current = await services.collections.get(
            collection.collection_id, kb.scope, ExecutionContext()
        )
        if change == "archive":
            assert result.state == "failed"
            assert result.error_code == "FORBIDDEN_SCOPE"
            assert (
                await owner.run_write(
                    owner.store.active_snapshot_id, ExecutionContext()
                )
                is None
            )
            assert current.archived_at is not None
        else:
            assert result.state == "succeeded", result
            assert current.title == "After" and current.slug == "after"
            consent = await services.collections.consent(
                collection.collection_id, kb.scope, ExecutionContext()
            )
            assert consent.collection_upload_providers == ("voyage",)


class SparseCounter(Counter):
    """Counts a token per four characters so the small response budget holds a section."""

    def count(self, text: str) -> int:
        return len(text) // 4


@pytest.mark.asyncio
async def test_archived_members_survive_unrelated_publication_and_reactivate(tmp_path):
    from tychora.contracts.collections import CollectionCreate
    from tychora.contracts.requests import QueryRequest

    with Tychora.open(tmp_path / "workspace", token_counter=SparseCounter()) as kb:
        ws = kb._workspace
        service = ws.services.collections
        a = await service.create(
            CollectionCreate(title="A", slug="a"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
        b = await service.create(
            CollectionCreate(title="B", slug="b"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )

        def add(cid, key):
            handle = kb.sources.add(
                {
                    "type": "text",
                    "source_key": key,
                    "collection_id": cid,
                    "kind": "document@1",
                    "format": "markdown",
                    "text": "# Cobalt\n\nCobalt calibration reference.",
                    "metadata": {"title": key},
                }
            )
            assert kb.jobs.wait(handle.id, timeout=5).state == "succeeded"

        add(a.collection_id, "a.md")
        identities = await ws.owner.run_write(
            lambda: ws.owner.db.execute(
                "SELECT source_id,revision_id,extraction_id FROM snapshot_members"
            ).fetchall(),
            ExecutionContext(),
        )
        await service.set_archived(a.collection_id, True, kb.scope, ExecutionContext())
        add(b.collection_id, "b.md")
        # Reuse B's existing immutable extraction while A remains archived.
        add(b.collection_id, "b.md")
        page = await service.documents(kb.scope, ExecutionContext())
        assert [doc.collection_id for doc in page.documents] == [b.collection_id]
        members = await ws.owner.run_write(
            lambda: members_of(ws.owner.store, ws.owner.store.resolve_snapshot(None)),
            ExecutionContext(),
        )
        assert all(
            any(
                (m.source_id, m.revision_id, m.extraction_id) == tuple(row)
                for m in members
            )
            for row in identities
        )
        await service.set_archived(a.collection_id, False, kb.scope, ExecutionContext())
        page = await service.documents(kb.scope, ExecutionContext())
        assert {doc.collection_id for doc in page.documents} == {
            a.collection_id,
            b.collection_id,
        }
        query = await ws.services.query.search(
            QueryRequest.from_mapping(
                {"question": "cobalt", "modalities": ["lexical"]}
            ),
            kb.scope,
            ExecutionContext(),
        )
        assert {c.source_ref for c in query.result.citations} == {"a.md", "b.md"}


@pytest.mark.asyncio
async def test_reuse_target_archived_after_prepared_plan_is_denied(
    tmp_path, monkeypatch
):
    import asyncio
    from threading import Event

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        services = kb._workspace.services
        source = {
            "type": "text",
            "source_key": "reuse",
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": "Cobalt reference.",
            "metadata": {"title": "Reuse"},
        }
        first = kb.sources.add(source)
        assert kb.jobs.wait(first.id, timeout=5).state == "succeeded"
        active = await kb._workspace.owner.run_write(
            kb._workspace.owner.store.active_snapshot_id, ExecutionContext()
        )
        entered, release = Event(), Event()
        original = services.sources._publish

        async def paused(*args, **kwargs):
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            return await original(*args, **kwargs)

        monkeypatch.setattr(services.sources, "_publish", paused)
        second = kb.sources.add(source)
        assert await asyncio.to_thread(entered.wait, 10)
        try:
            await services.collections.set_archived(
                str(kb.default_collection_id), True, kb.scope, ExecutionContext()
            )
        finally:
            release.set()
        result = await services.jobs.wait(
            second.id, ExecutionContext(), timeout=scaled(10)
        )
        assert result.state == "failed" and result.error_code == "FORBIDDEN_SCOPE"
        assert (
            await kb._workspace.owner.run_write(
                kb._workspace.owner.store.active_snapshot_id, ExecutionContext()
            )
            == active
        )


def test_commit_extraction_rechecks_archived_canonical_collection(tmp_path):
    from datetime import UTC, datetime

    from tests.helpers.corpus import reader, registry, seed_bundle
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.corpus import CorpusStore
    from tychora.configuration.collection_policy import parse_collection_policy
    from tychora.contracts.errors import TychoraError

    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        store = CorpusStore(db, registry=registry(), artifacts=reader())
        bundle = seed_bundle()
        store.commit_extraction(bundle)
        raw = db.execute("SELECT policy_json FROM collections").fetchone()[0]
        policy = parse_collection_policy(raw).model_copy(
            update={"archived_at": datetime.now(UTC)}
        )
        db.execute("UPDATE collections SET policy_json=?", (policy.model_dump_json(),))
        db.commit()
        with pytest.raises(TychoraError) as error:
            store.commit_extraction(bundle)
        assert error.value.code == "FORBIDDEN_SCOPE"
    finally:
        db.close()
