"""Publication rewrites one page, verifies only new batches, and binds through SQL."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from tests.indexing.test_publication import ProjectedCorpus, make_projected_corpus
from tychora.adapters.sqlite.repositories import SqliteSnapshotRepository
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import bucket_of


@pytest.fixture
def projected(tmp_path, config_factory) -> Iterator[ProjectedCorpus]:
    corpus = make_projected_corpus(tmp_path, config_factory)
    yield corpus
    corpus.store.db.close()


def _pages(store, snapshot_id):
    return dict(
        store.db.execute(
            "SELECT bucket,page_id FROM snapshot_pages WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
    )


def _source_id(store, source_key):
    return store.db.execute(
        "SELECT source_id FROM sources WHERE source_key=?", (source_key,)
    ).fetchone()[0]


def test_add_rewrites_only_its_bucket(projected):
    f = projected
    first = f.add_revision("first source text", source_key="a.md")
    second = f.add_revision("second source text", source_key="b.md")
    before, after = _pages(f.store, first), _pages(f.store, second)
    changed = {bucket for bucket in after if before.get(bucket) != after[bucket]}
    assert changed == {bucket_of(_source_id(f.store, "b.md"))}


def test_views_equal_the_plan(projected):
    f = projected
    base = f.store.active_snapshot_id()
    _, _, plan = f.prepare_revision("view equivalence text", source_key="v.md")
    snapshot_id = f.store.publish_snapshot(plan)
    members = set(
        f.store.db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default "
            "FROM snapshot_members WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    repository = SqliteSnapshotRepository(f.store.db)
    pages = list(plan.new_pages)
    for bucket, page_id in _pages(f.store, base).items():
        if page_id in plan.reused_page_ids:
            page = repository.page(base, bucket)
            assert page is not None
            pages.append(page)
    assert sorted(page.page_id for page in pages) == list(plan.page_ids)
    expected = {
        (m.source_id, m.revision_id, m.extraction_id, int(m.is_default))
        for page in pages
        for m in page.members
    }
    assert members == expected
    assert plan.member_count == len(expected)
    batches = {
        row[0]
        for row in f.store.db.execute(
            "SELECT batch_id FROM snapshot_batches WHERE snapshot_id=?", (snapshot_id,)
        )
    }
    bound = {b.batch_id for page in pages for m in page.members for b in m.batches}
    assert batches == bound | {b.batch_id for b in plan.root_batches}
    assert {b.batch_id for b in plan.new_batches} <= batches


def test_reused_batches_are_never_reopened(projected):
    f = projected
    f.add_revision("earlier source text", source_key="early.md")
    _, _, plan = f.prepare_revision("later source text", source_key="late.md")
    new_ids = {b.batch_id for b in plan.new_batches}
    opened = []
    original = f.store.artifacts.open_verified

    def spy(ref):
        opened.append(ref.sha256)
        return original(ref)

    verified = []
    original_verify = f.vector.verify_batch

    def verify_spy(receipt):
        verified.append(receipt.digest)
        return original_verify(receipt)

    with (
        patch.object(f.store.artifacts, "open_verified", spy),
        patch.object(f.vector, "verify_batch", verify_spy),
    ):
        f.store.publish_snapshot(plan)
    new_vectors = {b.vectors.sha256 for b in plan.new_batches if b.vectors is not None}
    new_inputs = {b.input_manifest.sha256 for b in plan.new_batches}
    assert set(opened) <= new_vectors | new_inputs | {plan.manifest.sha256}
    assert len(verified) == sum(1 for b in plan.new_batches if b.slot == "text")
    assert new_ids


def test_new_edition_rewrites_only_its_bucket(projected):
    f = projected
    f.add_revision("neighbour text", source_key="n.md")
    first = f.add_revision("edition one text", source_key="e.md")
    second = f.add_edition("edition two text", source_key="e.md", edition="2")
    before, after = _pages(f.store, first), _pages(f.store, second)
    assert sum(1 for b in after if before.get(b) != after[b]) == 1
    rows = f.store.db.execute(
        "SELECT r.edition_key,m.is_default FROM snapshot_members m "
        "JOIN revisions r USING(revision_id) "
        "JOIN sources s ON s.source_id=m.source_id "
        "WHERE m.snapshot_id=? AND s.source_key='e.md'",
        (second,),
    ).fetchall()
    assert sorted(rows) == [("1", 0), ("2", 1)]


def test_publication_refuses_reused_batch_that_is_not_ready(projected):
    f = projected
    f.add_revision("first", source_key="same.md")
    _, _, plan = f.prepare_revision("first", source_key="same.md")
    page_batches = {
        b.batch_id for page in plan.new_pages for m in page.members for b in m.batches
    } - {b.batch_id for b in plan.new_batches}
    assert page_batches
    f.store.db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?",
        (min(page_batches),),
    )
    f.store.db.commit()
    active = f.store.active_snapshot_id()
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(plan)
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )
    assert f.store.active_snapshot_id() == active


def test_resolve_snapshot_never_reads_the_root_manifest(projected):
    f = projected
    snapshot_id = f.add_revision("resolve from SQL", source_key="r.md")
    opened = []
    original = f.store.artifacts.open_verified

    def spy(ref):
        opened.append(ref.sha256)
        return original(ref)

    with patch.object(f.store.artifacts, "open_verified", spy):
        view = f.store.resolve_snapshot(snapshot_id)
        with f.store.pin_snapshot(snapshot_id):
            pass
    assert opened == []
    assert view.member_count == len(view.members)


@pytest.mark.parametrize("point", ["insert_page", "insert_snapshot", "set_active"])
def test_crash_inside_activation_keeps_the_previous_snapshot(projected, point):
    f = projected
    active = f.store.active_snapshot_id()
    before = f.store.resolve_snapshot(active)
    before_slots = f.store.snapshot_slots(before)
    _, _, plan = f.prepare_revision("crash during activation", source_key="crash.md")
    with (
        patch.object(
            SqliteSnapshotRepository, point, side_effect=RuntimeError("killed")
        ),
        pytest.raises(RuntimeError, match="killed"),
    ):
        f.store.publish_snapshot(plan)
    assert f.store.active_snapshot_id() == active
    # Writing the new lexical batch moves only the corpus-wide statistics epoch.
    after = f.store.resolve_snapshot(None)
    assert after == before.model_copy(
        update={"lexical_statistics_epoch": after.lexical_statistics_epoch}
    )
    assert f.store.snapshot_slots(before) == before_slots
    new_pages = [page.page_id for page in plan.new_pages]
    assert (
        f.store.db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE snapshot_id=?", (plan.snapshot_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        f.store.db.execute(
            "SELECT COUNT(*) FROM membership_pages WHERE page_id IN (?)", new_pages
        ).fetchone()[0]
        == 0
    )
    assert f.store.publish_snapshot(plan) == plan.snapshot_id
    assert f.store.active_snapshot_id() == plan.snapshot_id


def test_crash_after_batches_are_ready_resumes_without_rebuilding(projected):
    f = projected
    f.prepare_revision("resume after a crash", source_key="resume.md")
    upserts, calls = f.vector.upserts, f.embedder.calls
    _, _, plan = f.prepare_revision("resume after a crash", source_key="resume.md")
    assert {batch.slot for batch in plan.new_batches} <= {"graph"}
    assert (f.vector.upserts, f.embedder.calls) == (upserts, calls)
    assert f.store.publish_snapshot(plan) == plan.snapshot_id


def test_stale_base_is_a_retryable_rebase(projected):
    f = projected
    _, _, stale = f.prepare_revision("stale base text", source_key="stale.md")
    f.add_revision("another publication", source_key="other.md")
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(stale)
    assert (error.value.code, error.value.reason) == (
        "WORKSPACE_BUSY",
        "publication.rebase",
    )
    assert error.value.retryable


def test_unknown_snapshot_is_absent(projected):
    with pytest.raises(TychoraError) as error:
        projected.store.resolve_snapshot("0" * 64)
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "snapshot.absent")


def test_retained_slots_rebuild_the_published_batches(projected):
    f = projected
    _, _, plan = f.prepare_revision("retained slot text", source_key="slot.md")
    snapshot_id = f.store.publish_snapshot(plan)
    view = f.store.resolve_snapshot(snapshot_id)
    slots = {slot.slot: slot for slot in f.store.snapshot_slots(view)}
    assert set(slots) == {item.slot for item in plan.families}
    for batch in plan.new_batches:
        retained = next(
            item
            for item in slots[batch.slot].batches
            if item.batch_id == batch.batch_id
        )
        assert (retained.receipt, retained.input_manifest, retained.vectors) == (
            batch.receipt,
            batch.input_manifest,
            batch.vectors,
        )
        if batch.slot == "lexical":
            assert len(retained.lexical_records) == batch.expected_count
        if batch.slot == "text":
            assert len(retained.vector_records) == batch.expected_count


def test_second_query_on_a_snapshot_reads_no_manifest_and_no_lexical_body(projected):
    f = projected
    first_id = f.add_revision("cached slot text", source_key="cache.md")
    view = f.store.resolve_snapshot(first_id)
    statements: list[str] = []
    f.store.db.set_trace_callback(statements.append)
    try:
        first = f.store.read_eligibility_facts(view)
        opened = []
        original = f.store.artifacts.open_verified

        def spy(ref):
            opened.append(ref.sha256)
            return original(ref)

        with patch.object(f.store.artifacts, "open_verified", spy):
            second = f.store.read_eligibility_facts(view)
    finally:
        f.store.db.set_trace_callback(None)
    assert set(opened) <= {view.config_sha}
    assert not any(
        "FROM lexical_records" in sql and "body" in sql for sql in statements
    )
    assert second.slots == first.slots
    fresh_id = f.add_revision("a later activation", source_key="later.md")
    fresh = f.store.read_eligibility_facts(f.store.resolve_snapshot(fresh_id))

    def lexical(facts):
        slot = next(slot for slot in facts.slots if slot.slot == "lexical")
        return {batch.batch_id for batch in slot.batches}

    assert lexical(fresh) - lexical(first)


def test_disabling_a_channel_after_publication_drops_its_batches(projected, tmp_path):
    from tychora.configuration.resolve import resolve_config

    f = projected
    f.add_revision("edition one text", source_key="e.md")
    settings = tmp_path / "lexical-only.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    f.coordinator.config = resolve_config(settings, "test", {})
    f.store.required_slots = frozenset({"lexical"})
    snapshot_id = f.add_edition("edition two text", source_key="e.md", edition="2")
    view = f.store.resolve_snapshot(snapshot_id)
    assert view.family("text") is None
    slots = {
        row[0]
        for row in f.store.db.execute(
            "SELECT DISTINCT b.slot FROM snapshot_pages sp "
            "JOIN page_batches b USING(page_id) WHERE sp.snapshot_id=? AND sp.bucket=?",
            (snapshot_id, bucket_of(_source_id(f.store, "e.md"))),
        )
    }
    assert slots == {"lexical"}


def test_a_pruned_graph_root_batch_is_a_typed_missing_batch(projected):
    from tychora.contracts.graph import (
        GraphBatchReceipt,
        GraphGenerationSpec,
        GraphManifest,
    )
    from tychora.contracts.identity import digest

    f = projected
    view = f.store.resolve_snapshot(None)
    family = f.coordinator._graph_family(
        view.workspace_id, view.config_sha, "2024-01-01T00:00:00Z"
    )
    manifest = GraphManifest(
        spec=GraphGenerationSpec(
            generation_id="generation",
            family_id=family.family_id,
            config_sha=family.config_sha,
            registry_digest=f.store.registry.registry_digest,
            workspace_id=view.workspace_id,
            decision_watermark=0,
        ),
        entity_ids=(),
        evidence_ids=(),
        assertion_ids=(),
        content_digest=digest("content"),
    )
    receipt = GraphBatchReceipt(
        generation_id="generation",
        family_id=family.family_id,
        config_sha=family.config_sha,
        decision_watermark=0,
        manifest_digest=digest("manifest"),
        entity_count=0,
        evidence_count=0,
        assertion_count=0,
    )
    batch = f.coordinator._graph_root_batch(manifest, receipt)
    f.store.db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?", (batch.batch_id,)
    )
    f.store.db.commit()
    with pytest.raises(TychoraError) as error:
        f.coordinator._graph_root_batch(manifest, receipt)
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )


def _shared_bucket_keys() -> tuple[str, str]:
    """A source key in the default collection and one in ``OTHER`` sharing a bucket."""
    from uuid import UUID

    from tests.helpers.corpus import COLLECTION
    from tychora.contracts.identity import source_id

    buckets = {
        bucket_of(source_id(UUID(OTHER), f"y{index}.md")): f"y{index}.md"
        for index in range(600)
    }
    for index in range(600):
        key = f"x{index}.md"
        bucket = bucket_of(source_id(UUID(COLLECTION), key))
        if bucket in buckets:
            return key, buckets[bucket]
    raise AssertionError("no shared bucket")


OTHER = "6b4a3c51-6f39-4a5a-8f9b-0a0a5f6c2d11"


def test_archived_member_is_exempt_only_when_its_prior_row_is_unchanged(projected):
    from datetime import UTC, datetime

    from tests.helpers.corpus import seed_bundle
    from tychora.adapters.sqlite.publication import _check_members
    from tychora.application.collection_policy import parse_collection_policy

    f = projected
    x_key, y_key = _shared_bucket_keys()
    _, _, y_plan = f.prepare_bundle(
        seed_bundle("archived y", source_key=y_key, collection=OTHER)
    )
    f.store.publish_snapshot(y_plan)
    raw = f.store.db.execute(
        "SELECT policy_json FROM collections WHERE collection_id=?", (OTHER,)
    ).fetchone()[0]
    archived = parse_collection_policy(raw).model_copy(
        update={"archived_at": datetime.now(UTC)}
    )
    f.store.db.execute(
        "UPDATE collections SET policy_json=? WHERE collection_id=?",
        (archived.model_dump_json(), OTHER),
    )
    f.store.db.commit()
    bundle, _, plan = f.prepare_revision("active x", source_key=x_key)
    target = bundle.source.source_id
    (page,) = plan.new_pages
    assert {m.source_id for m in page.members} == {
        target,
        y_plan.new_pages[0].members[0].source_id,
    }
    _check_members(f.store, plan, target)
    forged_members = tuple(
        m if m.source_id == target else m.model_copy(update={"is_default": False})
        for m in page.members
    )
    forged = plan.model_copy(
        update={"new_pages": (page.model_copy(update={"members": forged_members}),)}
    )
    with pytest.raises(TychoraError) as error:
        _check_members(f.store, forged, target)
    assert error.value.code == "FORBIDDEN_SCOPE"


def test_text_batch_without_complete_groups_is_refused_when_loaded(projected):
    import json
    from io import BytesIO

    from tychora.contracts.identity import canonical_bytes

    f = projected
    view = f.store.resolve_snapshot(None)
    sha, size, media, key = f.store.db.execute(
        "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
        "FROM snapshot_batches s JOIN index_batches b USING(batch_id) "
        "JOIN artifacts a ON a.artifact_sha=b.input_manifest_sha "
        "WHERE s.snapshot_id=? AND s.slot='text'",
        (view.snapshot_id,),
    ).fetchone()
    from tychora.contracts.artifacts import ArtifactRef

    with f.store.artifacts.open_verified(
        ArtifactRef(sha256=sha, size_bytes=size, media_type=media, storage_key=key)
    ) as stream:
        document = json.load(stream)
    document["data"]["representations"] = document["data"]["representations"][1:]
    forged = f.store.artifacts.put_stream(
        BytesIO(canonical_bytes(document)), "application/json"
    )
    f.store.db.execute(
        "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
        "VALUES (?,?,?,?,'2026-01-01T00:00:00+00:00')",
        (forged.sha256, forged.size_bytes, forged.media_type, forged.storage_key),
    )
    f.store.db.execute(
        "UPDATE index_batches SET input_manifest_sha=? WHERE input_manifest_sha=?",
        (forged.sha256, sha),
    )
    f.store.db.commit()
    with pytest.raises(TychoraError) as error:
        f.store.snapshot_slots(view)
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )
