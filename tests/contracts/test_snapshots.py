"""Membership page identity, determinism and size bounds."""

from hashlib import sha256
from typing import Any

import pytest

from tychora.contracts.artifacts import ArtifactRef, validate_manifest_schema
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.snapshots import (
    MAX_PAGE_BYTES,
    PAGE_BUCKETS,
    IndexFamily,
    MembershipPage,
    PageBatch,
    PageMember,
    SlotFamily,
    SnapshotBinding,
    bucket_of,
    compute_page_id,
    membership_sha,
    snapshot_id_v2,
)


def _ref(seed: str) -> ArtifactRef:
    sha = sha256(seed.encode()).hexdigest()
    return ArtifactRef(
        sha256=sha, size_bytes=10, media_type="application/json", storage_key=sha
    )


def _source_in_bucket(bucket: int, start: int = 0) -> str:
    index = start
    while True:
        candidate = f"source-{index}"
        if bucket_of(candidate) == bucket:
            return candidate
        index += 1


def _member(source_id: str, revision: str = "r", *, slots=("lexical",)) -> PageMember:
    return PageMember(
        source_id=source_id,
        revision_id=f"{source_id}-{revision}",
        extraction_id=f"{source_id}-{revision}-x",
        batches=tuple(
            PageBatch(
                slot=slot,
                batch_id=f"{source_id}-{revision}-{slot}",
                family_id="f" * 64,
                count=3,
                receipt_digest="e" * 64,
                input_manifest=_ref(f"{source_id}-{revision}-{slot}"),
            )
            for slot in sorted(slots)
        ),
    )


def _page(*members: PageMember) -> MembershipPage:
    bucket = bucket_of(members[0].source_id)
    ordered = tuple(sorted(members, key=lambda m: (m.source_id, m.revision_id)))
    return MembershipPage(
        bucket=bucket, members=ordered, page_id=compute_page_id(bucket, ordered)
    )


def test_bucket_is_first_byte_of_source_sha():
    source = "0f1e2d3c-0000-4000-8000-000000000001"
    assert bucket_of(source) == int(sha256(source.encode()).hexdigest()[:2], 16)
    assert 0 <= bucket_of(source) < PAGE_BUCKETS


def test_page_identity_binds_members_and_batches():
    source = _source_in_bucket(7)
    page = _page(_member(source))
    other = _page(_member(source, slots=("lexical", "text")))
    assert page.page_id != other.page_id
    assert page.page_id == compute_page_id(7, page.members)


def test_page_is_deterministic_and_rejects_wrong_identity():
    source = _source_in_bucket(3)
    members = (_member(source),)
    assert compute_page_id(3, members) == compute_page_id(3, members)
    with pytest.raises(ValueError, match="identity"):
        MembershipPage(bucket=3, members=members, page_id="0" * 64)


def test_page_rejects_member_from_another_bucket():
    source = _source_in_bucket(3)
    members = (_member(source),)
    with pytest.raises(ValueError, match="bucket"):
        MembershipPage(bucket=4, members=members, page_id=compute_page_id(4, members))


def test_page_rejects_two_defaults_for_one_source():
    source = _source_in_bucket(9)
    first, second = _member(source, "a"), _member(source, "b")
    with pytest.raises(ValueError, match="default"):
        _page(first, second)
    _page(first, second.model_copy(update={"is_default": False}))


def _with_count(member: PageMember, count: int) -> PageMember:
    return member.model_copy(
        update={
            "batches": tuple(
                batch.model_copy(update={"count": count}) for batch in member.batches
            )
        }
    )


def test_page_size_is_independent_of_window_count():
    source = _source_in_bucket(1)
    member = _member(source, slots=("lexical", "text"))
    small, large = _with_count(member, 10_000), _with_count(member, 25_000)
    assert len(_page(large).manifest_bytes()) == len(_page(small).manifest_bytes())
    assert len(_page(large).manifest_bytes()) < 4096


def test_oversized_page_reports_reason():
    page = _page(_member(_source_in_bucket(2), slots=("image", "lexical", "text")))
    with pytest.raises(TychoraError) as error:
        page.manifest_bytes(limit=64)
    assert error.value.reason == "invariant.page_oversize"
    assert MAX_PAGE_BYTES == 4 * 1024 * 1024


def test_page_manifest_document_lists_inputs_and_vectors():
    source = _source_in_bucket(5)
    member = _member(source, slots=("text",))
    member = member.model_copy(
        update={
            "batches": (
                member.batches[0].model_copy(update={"vectors": _ref("vectors")}),
            )
        }
    )
    document: Any = _page(member).manifest_document()
    assert document["type"] == "membership_page"
    assert document["schema_version"] == "2"
    assert [item["artifact"]["sha256"] for item in document["required_manifests"]] == [
        member.batches[0].input_manifest.sha256
    ]
    assert [item["sha256"] for item in document["required_artifacts"]] == [
        _ref("vectors").sha256
    ]


def _family() -> SlotFamily:
    return SlotFamily(
        slot="lexical",
        family=IndexFamily(
            family_id="a" * 64,
            channel="lexical",
            backend="sqlite-fts5",
            config_sha="b" * 64,
            config_json=FrozenJsonMap({"tokenizer": "unicode61"}),
            backend_namespace="lexical",
            created_at="2026-10-16T00:00:00+00:00",
        ),
    )


def _binding(root_batch_ids: tuple[str, ...] = ()) -> SnapshotBinding:
    return SnapshotBinding(
        workspace_id="w",
        base_snapshot_id=None,
        job_id="j",
        config_sha="c" * 64,
        families=(_family(),),
        root_batch_ids=root_batch_ids,
    )


def test_snapshot_identity_sorts_pages_and_binds_root_batches():
    binding = _binding()
    first = snapshot_id_v2(binding, ("p2", "p1"))
    assert first == snapshot_id_v2(binding, ("p1", "p2"))
    assert first == digest(
        "snapshot-v2",
        "w",
        None,
        "j",
        "c" * 64,
        [{"slot": "lexical", "family_id": "a" * 64, "required": True}],
        ["p1", "p2"],
        [],
    )
    assert first != snapshot_id_v2(_binding(("g",)), ("p1", "p2"))
    assert membership_sha(("p2", "p1")) == membership_sha(("p1", "p2"))


def test_slot_family_requires_matching_channel():
    with pytest.raises(ValueError, match="channel"):
        SlotFamily(slot="text", family=_family().family)


@pytest.mark.parametrize(
    ("kind", "version", "ok"),
    [
        ("snapshot", "2", True),
        ("snapshot", "1", False),
        ("membership_page", "2", True),
        ("publication_plan", "2", True),
        ("index_input", "1", True),
        ("backup", 1, True),
    ],
)
def test_manifest_versions(kind, version, ok):
    if ok:
        validate_manifest_schema(kind, version)
    else:
        with pytest.raises(ValueError):
            validate_manifest_schema(kind, version)
