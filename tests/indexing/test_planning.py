"""The next page of a bucket changes only what the incoming member replaces."""

import random
from hashlib import sha256

import pytest

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import (
    MembershipPage,
    PageBatch,
    PageMember,
    bucket_of,
    compute_page_id,
)
from tychora.indexing.planning import member_count_after, next_page


def _sources(bucket: int, count: int) -> list[str]:
    found, index = [], 0
    while len(found) < count:
        if bucket_of(f"s{index}") == bucket:
            found.append(f"s{index}")
        index += 1
    return found


def _member(source: str, revision: str, *, default: bool = True) -> PageMember:
    sha = sha256(f"{source}{revision}".encode()).hexdigest()
    return PageMember(
        source_id=source,
        revision_id=revision,
        extraction_id=f"x-{revision}",
        is_default=default,
        batches=(
            PageBatch(
                slot="lexical",
                batch_id=f"b-{revision}",
                family_id="f" * 64,
                count=1,
                receipt_digest="e" * 64,
                input_manifest=ArtifactRef(
                    sha256=sha,
                    size_bytes=1,
                    media_type="application/json",
                    storage_key=sha,
                ),
            ),
        ),
    )


def _page(*members: PageMember) -> MembershipPage:
    bucket = bucket_of(members[0].source_id)
    ordered = tuple(sorted(members, key=lambda m: (m.source_id, m.revision_id)))
    return MembershipPage(
        bucket=bucket, members=ordered, page_id=compute_page_id(bucket, ordered)
    )


def test_first_member_creates_page():
    (source,) = _sources(4, 1)
    page = next_page(None, _member(source, "r1"), {}, "1")
    assert [m.revision_id for m in page.members] == ["r1"]


def test_other_sources_in_bucket_are_kept():
    a, b = _sources(4, 2)
    base = _page(_member(a, "ra"))
    page = next_page(base, _member(b, "rb"), {}, "1")
    assert {m.revision_id for m in page.members} == {"ra", "rb"}


def test_same_edition_replaces_and_new_edition_is_kept_non_default():
    (source,) = _sources(4, 1)
    base = _page(_member(source, "r1"))
    replaced = next_page(base, _member(source, "r2"), {"r1": "1"}, "1")
    assert [m.revision_id for m in replaced.members] == ["r2"]
    kept = next_page(base, _member(source, "r2"), {"r1": "1"}, "2")
    assert {(m.revision_id, m.is_default) for m in kept.members} == {
        ("r1", False),
        ("r2", True),
    }


def test_member_count_after_replacement():
    (source,) = _sources(4, 1)
    base = _page(_member(source, "r1"))
    page = next_page(base, _member(source, "r2"), {"r1": "1"}, "1")
    assert member_count_after(10, base, page) == 10
    assert member_count_after(10, None, page) == 11


def test_reapplying_the_incoming_member_is_idempotent():
    a, b = _sources(4, 2)
    base = _page(_member(a, "ra"), _member(b, "rb"))
    incoming = _member(a, "ra2")
    once = next_page(base, incoming, {"ra": "1"}, "2")
    twice = next_page(once, incoming, {"ra": "1"}, "2")
    assert twice == once


def test_incoming_replaces_itself_when_revision_is_already_present():
    (source,) = _sources(4, 1)
    base = _page(_member(source, "r1", default=False), _member(source, "r0"))
    page = next_page(base, _member(source, "r1"), {"r0": "1"}, "1")
    assert [(m.revision_id, m.is_default) for m in page.members] == [("r1", True)]


@pytest.mark.parametrize("incoming_edition", ["1", None])
def test_a_retained_revision_without_its_edition_is_refused(incoming_edition):
    (source,) = _sources(4, 1)
    base = _page(_member(source, "r1"))
    with pytest.raises(TychoraError) as raised:
        next_page(base, _member(source, "r2"), {}, incoming_edition)
    assert raised.value.code == "INTEGRITY_ERROR"
    assert raised.value.reason == "canonical.binding_mismatch"


def test_random_sequences_keep_pages_ordered_bounded_and_counted():
    rng = random.Random(8)
    sources = _sources(9, 5)
    page: MembershipPage | None = None
    editions: dict[str, str | None] = {}
    total = 0
    for step in range(60):
        source = rng.choice(sources)
        edition = rng.choice(["1", "2", None])
        incoming = _member(source, f"r{step}")
        retained = {
            m.revision_id: editions[m.revision_id]
            for m in (page.members if page else ())
            if m.source_id == source
        }
        after = next_page(page, incoming, retained, edition)
        total = member_count_after(total, page, after)
        editions[incoming.revision_id] = edition
        keys = [(m.source_id, m.revision_id) for m in after.members]
        assert keys == sorted(set(keys))
        assert total == len(after.members)
        assert after.bucket == 9
        defaults = [m.source_id for m in after.members if m.is_default]
        assert sorted(defaults) == sorted(set(defaults))
        assert any(
            m.revision_id == incoming.revision_id and m.is_default
            for m in after.members
        )
        page = after


def _model_step(
    model: dict[str, dict[str, bool]],
    editions: dict[str, str | None],
    source: str,
    revision: str,
    edition: str | None,
) -> None:
    """Apply one add to an independent membership model: revision -> default."""
    kept = {
        other: False
        for other in model.get(source, {})
        if other != revision and editions[other] != edition
    }
    model[source] = {**kept, revision: True}


def test_random_sequences_match_an_independent_membership_model():
    for seed in range(20):
        rng = random.Random(seed)
        sources = _sources(9, 4)
        page: MembershipPage | None = None
        editions: dict[str, str | None] = {}
        model: dict[str, dict[str, bool]] = {}
        for step in range(40):
            source = rng.choice(sources)
            edition = rng.choice(["1", "2", "3", None])
            # Re-adding an earlier revision of the source exercises self-replacement.
            earlier = [r for r in model.get(source, {}) if rng.random() < 0.2]
            revision = earlier[0] if earlier else f"r{seed}-{step}"
            edition = editions.get(revision, edition)
            retained = {
                m.revision_id: editions[m.revision_id]
                for m in (page.members if page else ())
                if m.source_id == source
            }
            page = next_page(page, _member(source, revision), retained, edition)
            editions[revision] = edition
            _model_step(model, editions, source, revision, edition)
            expected = {
                (s, r, default)
                for s, revisions in model.items()
                for r, default in revisions.items()
            }
            assert {
                (m.source_id, m.revision_id, m.is_default) for m in page.members
            } == (expected), (seed, step)
