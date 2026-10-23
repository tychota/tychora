"""The sort-and-sweep finds exactly the overlaps the pairwise scan found."""

import random
import time
from collections import defaultdict
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

import pytest

from tests.code.test_structure import _registration
from tests.helpers.corpus import registry, seed_bundle
from tests.ingestion.conftest import native_case  # noqa: F401  (fixture)
from tychora.adapters.code.normalize import (
    normalize_code,
    replay_code_extraction,
    retain_code_extraction,
)
from tychora.contracts.corpus import LocatorRow
from tychora.contracts.documents import DocumentSourcePart
from tychora.contracts.evidence import CodeLocator, TextLocator
from tychora.contracts.identity import digest
from tychora.contracts.indexing import OwnedInterval, dependency_digest
from tychora.indexing import representations
from tychora.indexing.intervals import overlapping_pairs
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ingestion.normalize import normalize

FIXTURES = Path(__file__).parents[1] / "fixtures/documents"
LAW = (
    "## Article 4\n\nIntro.\n\n1. First.\n   1. Nested.\n   2. Second nested.\n"
    "2. Second.\n\n## Article 5\n\nNext.\n"
)
FEEDBACK_METADATA = {
    "title": "Annotation set",
    "author": None,
    "target": None,
    "decision": None,
}


def _quadratic(intervals):
    return sorted(
        (i, j)
        for i, (a, b) in enumerate(intervals)
        for j, (c, d) in enumerate(intervals)
        if i < j and a < b and c < d and a < d and c < b
    )


def _quadratic_partition(unit, revision_id, overlaps):
    """The pairwise partition the sweep replaced, kept as the oracle."""
    boundaries = {0, len(unit.text)}
    for start, end, _ in overlaps:
        if not 0 <= start < end <= len(unit.text):
            raise ValueError("Canonical owner interval exceeds unit text")
        boundaries.update((start, end))
    intervals = []
    for start, end in pairwise(sorted(boundaries)):
        owners = {
            owner for left, right, owner in overlaps if left < end and start < right
        }
        owners.add(unit.unit_id)
        intervals.append(
            OwnedInterval(
                char_start=start,
                char_end=end,
                dependency_ids=frozenset({revision_id, *owners}),
            )
        )
    return tuple(intervals)


def _owners_by_locator(rows):
    """The pairwise overlap eligibility read before the facts were derived once."""
    owners = defaultdict(set)
    for uid, artifact, source, start, end in rows:
        for other, other_artifact, other_source, left, right in rows:
            if (
                artifact == other_artifact
                and source == other_source
                and start < right
                and left < end
            ):
                owners[uid].add(other)
    return owners


def _pairwise_dependencies(rows, prepared, chunks):
    """Unit, asset and chunk dependencies exactly as the per-member eligibility block derived them."""
    revision = rows.revision.revision_id
    overlap = _owners_by_locator(
        [
            (
                unit.unit_id,
                loc.artifact_ref,
                loc.native_ref,
                loc.char_start,
                loc.char_end,
            )
            for unit in rows.units
            for loc in unit.locators
            if isinstance(loc, (TextLocator, CodeLocator))
        ]
    )
    ownership = {item.unit.unit_id: item.ownership for item in prepared.units}
    proxies = {proxy.proxy_unit_id: proxy for proxy in rows.proxies}
    result = {}
    for unit in rows.units:
        uid = unit.unit_id
        declared = unit.attributes.get(
            "overlapping_owner_ids", unit.attributes.get("range_dependency_ids")
        )
        owners = set(declared) if declared is not None else set(overlap.get(uid, {uid}))
        owners.add(uid)
        if uid in proxies:
            owners.add(proxies[uid].asset_id)
            owners.update(proxies[uid].required_unit_ids)
        result[uid] = frozenset(
            dep for interval in ownership[uid] for dep in interval.dependency_ids
        ) or frozenset({revision, *owners})
    linked = defaultdict(set)
    for link in rows.unit_assets:
        linked[link.asset_id].add(link.unit_id)
    for asset in rows.assets:
        deps = {revision, asset.asset_id}
        for uid in linked[asset.asset_id]:
            deps.update(result[uid])
        result[asset.asset_id] = frozenset(deps)
    for chunk in chunks:
        result[chunk.chunk_id] = (
            frozenset(
                dep
                for interval in ownership[chunk.unit_id]
                if interval.char_start < chunk.char_end
                and chunk.char_start < interval.char_end
                for dep in interval.dependency_ids
            )
            or result[chunk.unit_id]
        )
    return result


def test_overlapping_pairs_equal_quadratic_on_random_intervals():
    rng = random.Random(20261016)
    for _ in range(1000):
        intervals = [
            (start, start + rng.randint(0, 30))
            for start in (rng.randint(0, 100) for _ in range(rng.randint(0, 40)))
        ]
        pairs = overlapping_pairs(intervals)
        assert sorted(tuple(sorted(pair)) for pair in pairs) == _quadratic(intervals)


@pytest.mark.parametrize(
    "intervals",
    [
        [(0, 5), (5, 9)],  # touching
        [(2, 7), (2, 7), (2, 7)],  # identical
        [(0, 20), (3, 9), (4, 6), (5, 5)],  # nested, with an empty range inside
        [(4, 4), (4, 4), (4, 8), (0, 4)],  # zero-length adjacent
        [(0, 0), (0, 1), (1, 1), (1, 2)],
        [],
    ],
)
def test_overlapping_pairs_edges(intervals):
    pairs = overlapping_pairs(intervals)
    assert len(pairs) == len(set(pairs))
    assert sorted(tuple(sorted(pair)) for pair in pairs) == _quadratic(intervals)


def test_sweep_scales_n_log_n():
    intervals = [(10 * i, 10 * i + 5) for i in range(10_000)] + [(0, 100_000)]
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        pairs = overlapping_pairs(intervals)
        timings.append(time.perf_counter() - started)
    assert min(timings) < 1.0
    assert len(pairs) == 10_000


def _random_locator(rng, start, end):
    artifact = rng.choice(("a" * 64, "b" * 64))
    path = rng.choice(("one.py", "two.py"))
    if rng.random() < 0.5:
        return TextLocator(
            artifact_ref=artifact, native_ref=path, char_start=start, char_end=end
        )
    return CodeLocator.model_construct(
        artifact_ref=artifact,
        native_ref=path,
        relative_path=path,
        char_start=start,
        char_end=end,
    )


def _random_units(rng, host):
    units = {}
    for number in range(rng.randint(0, 25)):
        locators = []
        for _ in range(rng.choice((1, 1, 1, 2))):
            start = rng.randint(0, 60)
            locators.append(
                _random_locator(rng, start, start + rng.choice((0, *range(25))))
            )
        unit = host.model_copy(
            update={"unit_id": f"unit-{number}", "locators": tuple(locators)}
        )
        units[unit.unit_id] = unit
    return units


def test_locator_overlaps_equal_pairwise_scan_across_domains():
    """Several artifacts, source refs and text/code types share one coordinate range."""
    rng = random.Random(7)
    host = seed_bundle().units[0]
    for _ in range(300):
        units = _random_units(rng, host)
        domains = defaultdict(list)
        for owner in units.values():
            for candidate in owner.locators:
                domains[representations._source_domain(candidate)].append(
                    (owner, candidate)
                )
        swept = representations._locator_overlaps(units)
        for unit in units.values():
            if len(unit.locators) != 1:
                continue
            (locator,) = unit.locators
            expected = tuple(
                (owner, candidate)
                for owner, candidate in domains[representations._source_domain(locator)]
                if max(locator.char_start, candidate.char_start)
                < min(locator.char_end, candidate.char_end)
            )
            assert swept.get(unit.unit_id, ()) == expected


def test_point_owners_equal_pairwise_scan_across_domains():
    rng = random.Random(11)
    host = seed_bundle().units[0]
    for _ in range(300):
        units = _random_units(rng, host)
        rows = [
            (
                unit.unit_id,
                loc.artifact_ref,
                loc.native_ref,
                loc.char_start,
                loc.char_end,
            )
            for unit in units.values()
            for loc in unit.locators
        ]
        old = _owners_by_locator(rows)
        new = representations._point_owners(units.values())
        for unit in units.values():
            if all(loc.char_start == loc.char_end for loc in unit.locators):
                assert (new.get(unit.unit_id) or {unit.unit_id}) | {unit.unit_id} == (
                    old.get(unit.unit_id, {unit.unit_id}) | {unit.unit_id}
                )


def test_part_overlaps_equal_pairwise_scan_across_domains():
    rng = random.Random(13)
    for _ in range(300):
        document_parts = {}
        for number in range(rng.randint(0, 12)):
            parts = []
            for _ in range(rng.randint(1, 4)):
                start = rng.randint(0, 40)
                length = rng.choice((0, *range(15)))
                parts.append(
                    DocumentSourcePart.model_construct(
                        native_ref="#/texts/0",
                        field="text",
                        source_start=start,
                        source_end=start + length,
                        unit_start=0,
                        unit_end=length,
                        source_domain=rng.choice(("d1", "d2", "d3")),
                    )
                )
            document_parts[f"unit-{number}"] = tuple(parts)
        swept = representations._part_overlaps(document_parts)
        for unit_id, parts in document_parts.items():
            for position, part in enumerate(parts):
                expected = tuple(
                    (owner_id, candidate)
                    for owner_id, candidates in document_parts.items()
                    for candidate in candidates
                    if candidate.source_domain == part.source_domain
                    and max(part.source_start, candidate.source_start)
                    < min(part.source_end, candidate.source_end)
                )
                assert swept.get((unit_id, position), ()) == expected


def _fixture_bundles(native_case, tmp_path):  # noqa: F811
    for text, kind, format, metadata in (
        (LAW, "law@1", "markdown", None),
        ((FIXTURES / "standards.md").read_text(), "standard@1", "markdown", None),
        ((FIXTURES / "standards.md").read_text(), "manual@1", "markdown", None),
        ((FIXTURES / "template.md").read_text(), "template@1", "markdown", None),
        (
            (FIXTURES / "feedback.json").read_text(),
            "feedback@1",
            "json",
            FEEDBACK_METADATA,
        ),
    ):
        parsed, _, rules = native_case(
            text, kind=kind, format=format, metadata=metadata
        )
        yield normalize(parsed, parsed.registration.revision.revision_id, rules)
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    yield normalize_code(
        replay_code_extraction(retained.raw_document_ref, registration, artifacts),
        registry(),
    )


def _assert_sweep_equals_oracle(bundles, monkeypatch):
    swept = [prepare_extraction(bundle.model_copy()) for bundle in bundles]
    with monkeypatch.context() as patch:
        patch.setattr(representations, "overlapping_pairs", _quadratic)
        patch.setattr(representations, "_partition", _quadratic_partition)
        assert [prepare_extraction(bundle.model_copy()) for bundle in bundles] == swept
    for bundle, prepared in zip(bundles, swept, strict=True):
        chunks = [
            ChunkSpan(chunk.chunk_id, chunk.unit_id, chunk.char_start, chunk.char_end)
            for chunk in bundle.chunks
        ]
        dependencies = representation_facts(bundle, prepared, chunks).dependency_map()
        expected = _pairwise_dependencies(bundle, prepared, chunks)
        assert {key: dependencies[key] for key in expected} == expected
    return swept


def test_sweep_equals_quadratic_overlap_on_fixtures(
    native_case,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    bundles = list(_fixture_bundles(native_case, tmp_path))
    swept = _assert_sweep_equals_oracle(bundles, monkeypatch)
    for prepared in (swept[0], swept[-1]):
        assert any(
            len(interval.dependency_ids) > 2
            for item in prepared.units
            for interval in item.ownership
        )


@pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")
@pytest.mark.parametrize("kind", ["standard@1", "law@1", "feedback@1"])
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_sweep_equals_quadratic_overlap_on_docling_records(
    document_record_case, kind, format, monkeypatch
):
    """Docling record documents own their text through overlapping source parts."""
    bundle = document_record_case(kind, format).bundle
    (prepared,) = _assert_sweep_equals_oracle([bundle], monkeypatch)
    assert any(
        "source_parts" in item.unit.attributes and item.ownership
        for item in prepared.units
    )


def test_empty_unit_depends_on_units_that_strictly_contain_its_position():
    bundle = seed_bundle("Seal checks are quarterly.")
    host = bundle.units[0]
    empty_locator = host.locators[0].model_copy(update={"char_start": 5, "char_end": 5})
    empty = host.model_copy(
        update={
            "unit_id": "empty-point",
            "ordinal": 1,
            "native_ref": "paragraph:2",
            "text": "",
            "text_sha": sha256(b"").hexdigest(),
            "locators": (empty_locator,),
        }
    )
    rows = bundle.model_copy(
        update={
            "units": (host, empty),
            "locators": (
                *bundle.locators,
                LocatorRow(
                    locator_id=digest("locator-v1", "empty-point", 0),
                    unit_id="empty-point",
                    fragment_ordinal=0,
                    locator=empty_locator,
                ),
            ),
        }
    )
    chunk = rows.chunks[0]
    facts = representation_facts(
        rows,
        prepare_extraction(rows),
        (ChunkSpan(chunk.chunk_id, chunk.unit_id, chunk.char_start, chunk.char_end),),
    )
    revision = rows.revision.revision_id
    deps = facts.dependency_map()
    assert deps[host.unit_id] == frozenset({revision, host.unit_id})
    assert deps["empty-point"] == frozenset({revision, "empty-point", host.unit_id})
    assert deps[chunk.chunk_id] == deps[host.unit_id]
    (interval,) = facts.intervals
    assert (interval.unit_id, interval.char_start, interval.char_end) == (
        host.unit_id,
        0,
        len(host.text),
    )
    assert interval.dependency_digest == dependency_digest(deps[host.unit_id])
    assert deps[interval.dependency_digest] == deps[host.unit_id]
