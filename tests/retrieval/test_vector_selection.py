"""Vector search reads the view: revision filters, over-fetch, canonical owners."""

import struct
from types import SimpleNamespace

from tests.helpers.eligibility import revision_view
from tychora.adapters.chroma import index as chroma_index
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import digest, family_id, space_id
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import VectorBatch, VectorOwner, partition_size
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import OwnerFact
from tychora.ports.search import RankedOwner, RankedOwners
from tychora.retrieval.vectors import linked_visual_context, vector_candidates


def _spec():
    nonce = "00000000-0000-4000-8000-000000000001"
    return FamilySpec(
        family_id=family_id("workspace", digest("config"), nonce),
        workspace_id="workspace",
        config_sha=digest("config"),
        creation_uuid=nonce,
        modality="text",
        provider="synthetic",
        model="synthetic",
        dimensions=2,
        metric="cosine",
        normalization="unit-l2",
        representation_fingerprint=digest("representation"),
        backend_namespace="text",
        backend_space="ip",
    )


def _index(tmp_path, owners):
    """A family whose record i points at angle i and belongs to owners[i]."""
    index = ChromaVectorIndex(tmp_path / "chroma")
    spec = _spec()
    index.create_family(spec)
    space = space_id(
        "text", "synthetic", "synthetic", 2, "cosine", spec.representation_fingerprint
    )
    records, rows = [], []
    for position, (revision, unit) in enumerate(owners):
        angle = position / 1000
        values = (1.0 - angle * angle / 2, angle)
        norm = (values[0] ** 2 + values[1] ** 2) ** 0.5
        stored = struct.pack("<ff", values[0] / norm, values[1] / norm)
        record_id = digest("record", position)
        records.append(
            EmbeddingRecord.from_normalized_bytes(
                record_id=record_id,
                input_sha=digest("input", position),
                space_id=space,
                stored=stored,
            )
        )
        rows.append(
            VectorOwner(
                record_id=record_id,
                revision_id=revision,
                extraction_id=f"{revision}-extraction",
                owner_kind="unit",
                owner_id=unit,
            )
        )
    index.upsert_batch(
        VectorBatch(
            family_id=spec.family_id, records=tuple(records), owners=tuple(rows)
        ),
        ExecutionContext(),
    )
    return index


def _query():
    return struct.pack("<ff", 1.0, 0.0)


def test_filtered_search_below_512_revisions(tmp_path):
    owners = [(f"r{i % 4}", f"u{i}") for i in range(40)]
    index = _index(tmp_path, owners)
    view = revision_view({"r1": "r1-extraction"})
    result = index.search(view, _query(), 5, ExecutionContext())
    assert [owner.key for owner in result.owners] == [
        ("unit", f"u{i}") for i in (1, 5, 9, 13, 17)
    ]
    assert not result.truncated
    assert result.detail["rescored"] == 0


def test_overfetch_returns_limit_or_truncated_above_512(tmp_path, monkeypatch):
    # Records sit at increasing distance; revision r(i % 25) owns record i.
    monkeypatch.setattr(chroma_index, "REVISION_FILTER_LIMIT", 1)
    owners = [(f"r{i % 25}", f"u{i}") for i in range(500)]
    index = _index(tmp_path, owners)
    narrow = revision_view({"r7": "r7-extraction", "r9": "r9-extraction"})
    result = index.search(narrow, _query(), 5, ExecutionContext())
    # 20 -> 40 -> 80 nearest records hold u7, u9, u32, u34, u57, u59.
    assert [owner.key[1] for owner in result.owners] == [
        "u7",
        "u9",
        "u32",
        "u34",
        "u57",
    ]
    assert not result.truncated
    assert result.detail["queries"] == 3
    monkeypatch.setattr(chroma_index, "OVERFETCH_CAP", 40)
    tiny = revision_view({"r23": "r23-extraction", "r24": "r24-extraction"})
    capped = index.search(tiny, _query(), 5, ExecutionContext())
    assert [owner.key[1] for owner in capped.owners] == ["u23", "u24"]
    assert capped.truncated


def test_upsert_partitions_stay_under_2_mb():
    assert partition_size(1024) == 488
    assert partition_size(1024) * 4 * 1024 <= 2_000_000
    assert partition_size(1_000_000) == 1


def test_vector_batch_partitions_keep_records_and_owners_together(tmp_path):
    spec = _spec()
    space = space_id(
        "text", "synthetic", "synthetic", 2, "cosine", spec.representation_fingerprint
    )
    records = tuple(
        EmbeddingRecord.from_normalized_bytes(
            record_id=digest("r", i),
            input_sha=digest("i", i),
            space_id=space,
            stored=struct.pack("<ff", 1.0, 0.0),
        )
        for i in range(5)
    )
    owners = tuple(
        VectorOwner(
            record_id=r.record_id,
            revision_id="rev",
            extraction_id="ext",
            owner_kind="unit",
            owner_id="u",
        )
        for r in records
    )
    batch = VectorBatch(family_id=spec.family_id, records=records, owners=owners)
    parts = batch.partitions(250_000)
    assert [len(part.records) for part in parts] == [2, 2, 1]
    assert all(
        {o.record_id for o in part.owners} == {r.record_id for r in part.records}
        for part in parts
    )


# Canonical owners: records mapped through one owner-facts read.


def _fact(owner_id, kind, *, deps=("revision",), **fields):
    return OwnerFact(
        owner_id=owner_id,
        kind=kind,
        extraction_id="extraction",
        dependency_ids=frozenset({*deps, owner_id}),
        **fields,
    )


def _record(record_id, *, batch="batch", chunk=None, unit=None, asset=None):
    return _fact(
        record_id,
        "record",
        batch_id=batch,
        chunk_id=chunk,
        unit_id=unit,
        asset_id=asset,
        proxy=chunk is not None and asset is not None,
    )


def _store(*facts):
    known = {fact.owner_id: fact for fact in facts}
    reads: list[tuple[str, ...]] = []

    def owner_facts(ids):
        reads.append(tuple(ids))
        return tuple(known[i] for i in ids if i in known)

    return SimpleNamespace(eligibility=SimpleNamespace(owner_facts=owner_facts)), reads


def _ranked(*rows, truncated=False):
    """Adapter owners as Chroma metadata gives them: ``(owner, score, records)``."""
    return RankedOwners(
        owners=tuple(
            RankedOwner(key=("unit", owner), rank=rank, score=score, window_ids=records)
            for rank, (owner, score, records) in enumerate(rows, 1)
        ),
        truncated=truncated,
    )


def _view(**kwargs):
    return revision_view(batches={"text": ("batch",), "image": ("batch",)}, **kwargs)


def _text_corpus():
    return (
        _fact("u1", "unit", unit_id="u1", citable=True),
        _fact("c1", "chunk", unit_id="u1", chunk_id="c1", citable=True),
        _record("v1", chunk="c1", unit="u1"),
        _fact("p", "unit", unit_id="p", asset_id="a", proxy=True),
        _fact("cp", "chunk", unit_id="p", chunk_id="cp", asset_id="a", proxy=True),
        _record("vp", chunk="cp", unit="p", asset="a"),
    )


def test_text_records_map_to_units_and_proxy_windows_to_their_asset():
    tx, reads = _store(*_text_corpus())
    ranked = _ranked(("u1", -0.1, ("v1",)), ("p", -0.2, ("vp",)))
    candidates, truncated = vector_candidates(tx, _view(), ranked, limit=2)
    assert [(c.key, c.rank, c.matching_window_ids) for c in candidates] == [
        (("unit", "u1"), 1, ("c1",)),
        (("asset", "a"), 1, ("cp",)),
    ]
    assert not truncated
    assert reads[0] == ("v1", "vp")


def test_dropped_records_below_the_limit_mark_the_channel_truncated():
    unserved = _record("v-old", batch="collected", chunk="c1", unit="u1")
    tx, _ = _store(*_text_corpus(), unserved)
    ranked = _ranked(("u1", -0.1, ("v-old",)), ("p", -0.2, ("vp",)))
    candidates, truncated = vector_candidates(tx, _view(), ranked, limit=2)
    assert [c.key for c in candidates] == [("asset", "a")]
    assert truncated
    full, still = vector_candidates(tx, _view(), ranked, limit=1)
    assert [c.key for c in full] == [("asset", "a")]
    assert not still


def test_excluded_unit_record_is_dropped_and_truncates():
    tx, _ = _store(*_text_corpus())
    ranked = _ranked(("u1", -0.1, ("v1",)), ("p", -0.2, ("vp",)))
    view = _view(excluded_units=("u1",))
    candidates, truncated = vector_candidates(tx, view, ranked, limit=80)
    assert [c.key for c in candidates] == [("asset", "a")]
    assert truncated


def test_filtered_query_keeps_an_eligible_image_asset():
    asset = _fact(
        "img",
        "asset",
        deps=("revision", "src"),
        asset_id="img",
        source_owner_ids=frozenset({"src"}),
    )
    source = _fact("src", "unit", unit_id="src", citable=True)
    record = _record("vi", asset="img")
    tx, reads = _store(asset, source, record)
    ranked = RankedOwners(
        owners=(
            RankedOwner(key=("asset", "img"), rank=1, score=-0.1, window_ids=("vi",)),
        )
    )
    kept, _ = vector_candidates(tx, _view(unit_filter=("src",)), ranked, limit=1)
    assert [c.key for c in kept] == [("asset", "img")]
    assert reads == [("vi",), ("img",), ("src",)]
    refused, _ = vector_candidates(tx, _view(unit_filter=("other",)), ranked, limit=1)
    assert refused == ()


def test_linked_visual_context_votes_for_eligible_sources_only():
    asset = _fact(
        "img",
        "asset",
        deps=("revision", "ok", "hidden"),
        asset_id="img",
        source_owner_ids=frozenset({"ok", "hidden"}),
    )
    ok = _fact("ok", "unit", unit_id="ok", citable=True)
    hidden = _fact("hidden", "unit", unit_id="hidden", citable=True)
    tx, _ = _store(asset, ok, hidden)
    linked = linked_visual_context(
        tx,
        _view(excluded_units=("hidden",)),
        (ChannelCandidate(key=("asset", "img"), rank=1),),
    )
    assert [candidate.key for candidate in linked] == [("unit", "ok")]


def test_windows_keep_the_adapter_nearest_first_order():
    tx, _ = _store(
        *_text_corpus(),
        _fact("c0", "chunk", unit_id="u1", chunk_id="c0", citable=True),
        _record("v0", chunk="c0", unit="u1"),
    )
    ranked = _ranked(("u1", -0.1, ("v1", "v0")))
    (candidate,), _ = vector_candidates(tx, _view(), ranked, limit=1)
    assert candidate.matching_window_ids == ("c1", "c0")
