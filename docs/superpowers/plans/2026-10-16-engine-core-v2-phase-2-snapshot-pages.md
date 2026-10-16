# Engine Core v2 Phase 2: Snapshot Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the corpus-sized snapshot manifest with 256 content-addressed membership pages, publish by verifying only the batches an add wrote, store each vector batch as one artifact, give every index family a persisted creation nonce, and collect unretained snapshots, pages and batches.

**Architecture:** A snapshot becomes a set of SQL rows (`snapshot_pages` → `membership_pages` → `page_members`, `page_batches`) plus two small retention documents (one root, one page per occupied bucket). The projection coordinator writes the new member's lexical, vector and transitional graph batches as `building` rows, marks them `ready`, rebuilds only the page of the member's bucket and reuses every other page ID. `publish_snapshot` verifies the new batches outside the transaction, checks reused batches with one `count_ready` statement, and switches the active snapshot inside one `BEGIN IMMEDIATE` transaction. Read paths resolve snapshots from SQL only; an interim adapter (`adapters/sqlite/retained_slots.py`) rebuilds the per-slot batch contents that eligibility, recovery and inspection consume until phase 4 replaces them.

**Tech Stack:** Python 3.12, Pydantic 2 contracts, SQLite (WAL, FTS5, STRICT, `json_each`), Chroma 1.5.9, Ladybug 0.20.3, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.2, §4.3, §4.4 (vector batches and family identity; family options are phase 7), §4.11 (`native`), §5 (archives). Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` (authoritative cross-phase names). Flow review: D1, D2, D6, D7 (publication and segments half), C5 (family identity), Q2 (manifest half).

## Global Constraints

Every task implicitly includes the index's Global Constraints. Those that bite hardest here, copied verbatim or with exact values from the spec:

- `MAX_MANIFEST_BYTES` stays at 16 MiB as a guard. Publication fails with `INTEGRITY_ERROR` reason `invariant.page_oversize` if a page exceeds 4 MiB.
- `bucket = int(sha256(source_id)[:2], 16)`; 256 buckets.
- `snapshot_id = digest("snapshot-v2", workspace_id, base_snapshot_id, job_id, config_sha, families, sorted(page_ids))` (plus the transitional root batch IDs, see "Spec gaps").
- `family_id = digest("family-v1", workspace_id, config_sha, nonce)` for lexical, text, image and graph alike. Vector record IDs are `vector_record_id(family_id, chunk_or_asset_id, input_sha)`.
- Vector batch artifact: media type `application/vnd.tychora.vector-batch.v1`; magic `TYVB0001`; `u32` header length; canonical JSON header `{family_id, dimension, records: [{record_id, input_sha, value_sha}]}` sorted by `record_id`; `count × dimension` little-endian float32 rows in header order. `value_sha` stays the sha256 of one row's float32 bytes.
- A missing vector batch artifact yields `CAPABILITY_MISSING` with reason `vectors.cache_absent`.
- Snapshot retention: active, leased, referenced by an unexpired `query_runs` row or an evaluation run, or among the last 16 published. The post-publication GC step handles at most 64 snapshots.
- Source, revision, extraction, unit and chunk identities are unchanged.
- Every DDL change updates `SCHEMA_CHECKSUM` in `src/tychora/adapters/sqlite/migrations.py` in the same commit (index "Format changes across phases").
- New `TychoraError` sites pass `reason=` from `src/tychora/contracts/error_reasons.py`; the two moved `CAPABILITY_MISSING` sites for an unopened native index keep `reason=None` because the registry has no matching `CAPABILITY_MISSING` reason (see the self-review). Reasons used in this phase: `canonical.row_invalid`, `canonical.binding_mismatch`, `config.drift`, `payload.hash_mismatch`, `payload.missing`, `invariant.page_oversize`, `invariant.unexpected` (`INTEGRITY_ERROR`); `publication.rebase` (`WORKSPACE_BUSY`); `batch.missing` (`INDEX_INCOMPLETE`); `vectors.cache_absent` (`CAPABILITY_MISSING`). All are in the phase-1 registry.
- No comment or docstring cites a plan task, plan file or review ID (gate G15). New functions ≤ 80 lines and cognitive complexity ≤ 15.
- Test command form: `uv run --locked pytest <path> -v`. Full suite: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`.

## Review Focus

1. **A workspace created before this phase's first DDL change is opened afterwards.** Expect `PROFILE_INCOMPATIBLE` reason `workspace.format_old`, not a raw schema mismatch. Pinned by `test_checksum_change_reports_format_old` in Task 4.
2. **A process dies after a batch is written and before activation.** Expect the `building` (or orphan `ready`) batch to be invisible to the active snapshot, a retried add to succeed, and `manage gc` to delete the orphan rows. Pinned by `test_crash_between_batch_write_and_activation_is_collected` in Task 10.
3. **One bucket holds a document with tens of thousands of windows.** Expect the page to list one batch reference per slot (not per vector), the vector batch to be one artifact, and no root or page artifact over 1 MiB. Pinned by `test_page_size_is_independent_of_window_count` in Task 1 and `test_large_text_batch_is_one_artifact` in Task 5.
4. **The same source is re-added with a new edition while other sources share its bucket.** Expect the earlier edition kept as non-default, the bucket's other members unchanged, and every other bucket's page ID reused. Pinned by `test_new_edition_rewrites_only_its_bucket` in Task 9.
5. **A reused batch was pruned to `pending` by native GC between preparation and publication.** Expect `INDEX_INCOMPLETE` reason `batch.missing` and the active snapshot unchanged, never a partial switch. Pinned by `test_publication_refuses_reused_batch_that_is_not_ready` in Task 9.

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `src/tychora/contracts/snapshots.py` (new) | Buckets, `PageBatch`, `PageMember`, `MembershipPage`, `SlotFamily`, `IndexFamily` (moved), page and snapshot identity, page manifest document | 1 |
| `src/tychora/contracts/artifacts.py` | Manifest type and version table (`snapshot`, `membership_page`, `publication_plan` at "2") | 1 |
| `src/tychora/indexing/vector_batches.py` (new) | `TYVB0001` codec | 2 |
| `src/tychora/configuration/models.py`, `src/tychora/application/sources.py`, `src/tychora/retrieval/segments.py` | `retained_sha`, registry artifact written once, lane and role lookup without dumps | 3 |
| `src/tychora/adapters/sqlite/repositories/families.py` (new), `src/tychora/ports/repositories.py` | Family rows and nonces; `FamilyRepository` | 4 |
| `src/tychora/indexing/coordinator.py` | Nonces and record re-keying (4), one artifact per vector batch (5), owner metadata (6), batch writes, page planning and root document (9) | 4, 5, 6, 9 |
| `src/tychora/contracts/vectors.py`, `src/tychora/adapters/chroma/index.py` | `VectorOwner` metadata on upsert | 6 |
| `src/tychora/adapters/sqlite/repositories/batches.py`, `snapshots.py` (new) | Batch rows, `count_ready`, pages, snapshots, retention, GC, footprint | 7, 9, 10, 13 |
| `src/tychora/adapters/sqlite/repositories/__init__.py` | `SqliteTransaction` gains `families`, `batches`, `snapshots` | 4, 7 |
| `src/tychora/resources/migrations/0001.sql`, `src/tychora/adapters/sqlite/migrations.py` | Format-2 DDL growth and checksum | 4, 5, 7, 9, 10 |
| `src/tychora/indexing/planning.py` (new) | Pure page planning: next page for a bucket, member counts | 8 |
| `src/tychora/contracts/publication.py` | `NewBatch` (7); `PublicationPlan` v2, interim `SnapshotView`, `RetainedSlot`, `RetainedBatch`, root and plan documents (9) | 5, 7, 9 |
| `src/tychora/adapters/sqlite/publication.py` | Publication v2 verifying new batches only; SQL-only receipts | 5, 9, 10 |
| `src/tychora/adapters/sqlite/retained_slots.py` (new) | Interim per-slot batch contents from SQL and input manifests | 9 |
| `src/tychora/adapters/sqlite/corpus.py` | `resolve_snapshot` from SQL, `pin_snapshot` leases only, `snapshot_slots` | 5, 9 |
| `src/tychora/application/snapshot_gc.py` (new), `src/tychora/application/leases.py` | GC orchestration: native drops, purge, artifact release | 10 |
| `src/tychora/application/recovery.py` | Native rebuild from batch artifacts (5, 6); per-batch health without a ceiling (11) | 5, 6, 9, 11 |
| `src/tychora/adapters/files/references.py`, `src/tychora/application/removal.py` | Retention roots for pages, roots, plans and vector batches | 5, 9, 12 |
| `src/tychora/contracts/operations.py`, `src/tychora/application/maintenance.py`, `src/tychora/interfaces/commands/manage.py` | `gc` maintenance operation and `tychora manage gc` | 10 |
| `src/tychora/evaluation/bench/footprint.py` (new) | Publication footprint per add for the scale suite | 13 |

---

### Task 1: Membership page contracts and manifest versions

**Files:**
- Create: `src/tychora/contracts/snapshots.py`
- Modify: `src/tychora/contracts/artifacts.py:38-67` (manifest types and `validate_manifest_schema`)
- Modify: `src/tychora/contracts/publication.py:27-38` (move `IndexFamily` to `snapshots.py`, re-export it)
- Test: `tests/contracts/test_snapshots.py` (new)

**Interfaces:**
- Consumes (phase 1): `TychoraError(code, message, *, reason=...)`.
- Produces: `PAGE_BUCKETS: int = 256`, `MAX_PAGE_BYTES: int = 4 * 1024 * 1024`, `bucket_of(source_id: str) -> int`, `PageBatch`, `PageMember`, `MembershipPage` (with `manifest_document() -> dict` and `manifest_bytes() -> bytes`), `SlotFamily`, `IndexFamily` (now defined here), `compute_page_id(bucket: int, members: Sequence[PageMember]) -> str`, `snapshot_id_v2(workspace_id, base_snapshot_id, job_id, config_sha, families, page_ids, *, root_batch_ids=()) -> str`, `membership_sha(page_ids: Sequence[str]) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/contracts/test_snapshots.py
"""Membership page identity, determinism and size bounds."""

from hashlib import sha256

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


def test_page_size_is_independent_of_window_count():
    source = _source_in_bucket(1)
    member = _member(source, slots=("lexical", "text"))
    large = member.model_copy(
        update={
            "batches": tuple(
                batch.model_copy(update={"count": 25_000}) for batch in member.batches
            )
        }
    )
    assert len(_page(large).manifest_bytes()) == len(_page(member).manifest_bytes())
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
    document = _page(member).manifest_document()
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


def test_snapshot_identity_sorts_pages_and_binds_root_batches():
    families = (_family(),)
    first = snapshot_id_v2("w", None, "j", "c" * 64, families, ("p2", "p1"))
    assert first == snapshot_id_v2("w", None, "j", "c" * 64, families, ("p1", "p2"))
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
    assert first != snapshot_id_v2(
        "w", None, "j", "c" * 64, families, ("p1", "p2"), root_batch_ids=("g",)
    )
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_snapshots.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.contracts.snapshots'`.

- [ ] **Step 3: Move `IndexFamily` and write `contracts/snapshots.py`**

Delete the `IndexFamily` class from `src/tychora/contracts/publication.py:27-38` and add, next to the other imports of that file:

```python
from .snapshots import IndexFamily as IndexFamily
```

Create `src/tychora/contracts/snapshots.py`:

```python
"""Membership pages: the bucketed, content-addressed binding of one snapshot."""

from collections.abc import Sequence
from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from .artifacts import ArtifactRef, ManifestRef
from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .errors import TychoraError
from .identity import JsonValue, canonical_bytes, digest

PAGE_BUCKETS = 256
MAX_PAGE_BYTES = 4 * 1024 * 1024


def bucket_of(source_id: str) -> int:
    """Return the page bucket of a source: the first byte of its SHA-256."""
    return int(sha256(source_id.encode("utf-8")).hexdigest()[:2], 16)


class IndexFamily(Contract):
    family_id: Sha256
    channel: Literal["lexical", "text", "image", "graph"]
    backend: NonEmptyStr
    provider: str | None = None
    model: str | None = None
    dimension: int | None = None
    config_sha: Sha256
    config_json: FrozenJsonMap
    backend_namespace: NonEmptyStr
    created_at: NonEmptyStr


class SlotFamily(Contract):
    slot: NonEmptyStr
    family: IndexFamily
    required: bool = True

    @model_validator(mode="after")
    def slot_is_channel(self) -> Self:
        if self.family.channel != self.slot:
            raise ValueError("Slot family channel differs from its slot")
        return self


class PageBatch(Contract):
    slot: NonEmptyStr
    batch_id: Identifier
    family_id: Sha256
    count: NonNegativeInt
    receipt_digest: Sha256
    input_manifest: ArtifactRef
    vectors: ArtifactRef | None = None


class PageMember(Contract):
    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    is_default: bool = True
    batches: tuple[PageBatch, ...] = ()

    @model_validator(mode="after")
    def one_batch_per_slot(self) -> Self:
        slots = [batch.slot for batch in self.batches]
        if slots != sorted(set(slots)):
            raise ValueError("Page member batches must be unique and sorted by slot")
        return self


def compute_page_id(bucket: int, members: Sequence[PageMember]) -> str:
    """Bind one bucket's members and every batch they publish."""
    return digest(
        "membership-page-v2",
        bucket,
        [member.model_dump(mode="json", exclude={"batches"}) for member in members],
        [[batch.model_dump(mode="json") for batch in member.batches] for member in members],
    )


class MembershipPage(Contract):
    bucket: NonNegativeInt
    members: tuple[PageMember, ...]
    page_id: Identifier

    @model_validator(mode="after")
    def exact_page(self) -> Self:
        if self.bucket >= PAGE_BUCKETS:
            raise ValueError("Page bucket is outside 0..255")
        keys = [(m.source_id, m.revision_id) for m in self.members]
        if keys != sorted(set(keys)):
            raise ValueError("Page members must be unique and sorted")
        if any(bucket_of(m.source_id) != self.bucket for m in self.members):
            raise ValueError("Page member belongs to another bucket")
        defaults = [m.source_id for m in self.members if m.is_default]
        if len(defaults) != len(set(defaults)):
            raise ValueError("Page has two default revisions for one source")
        if self.page_id != compute_page_id(self.bucket, self.members):
            raise ValueError("Page identity differs from its bindings")
        return self

    def manifest_document(self) -> dict[str, JsonValue]:
        """Return the retention document: inputs and vector batches, no content."""
        inputs = {
            batch.input_manifest.sha256: batch.input_manifest
            for member in self.members
            for batch in member.batches
        }
        vectors = {
            batch.vectors.sha256: batch.vectors
            for member in self.members
            for batch in member.batches
            if batch.vectors is not None
        }
        return {
            "type": "membership_page",
            "schema_version": "2",
            "required_manifests": [
                ManifestRef(
                    artifact=ref, type="index_input", schema_version="1"
                ).model_dump(mode="json")
                for ref in sorted(inputs.values(), key=lambda ref: ref.sha256)
            ],
            "required_artifacts": [
                ref.model_dump(mode="json")
                for ref in sorted(vectors.values(), key=lambda ref: ref.sha256)
            ],
            "data": {
                "bucket": self.bucket,
                "page_id": self.page_id,
                "members": [member.model_dump(mode="json") for member in self.members],
            },
        }

    def manifest_bytes(self, *, limit: int = MAX_PAGE_BYTES) -> bytes:
        raw = canonical_bytes(self.manifest_document())
        if len(raw) > limit:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Membership page exceeds its byte limit",
                reason="invariant.page_oversize",
            )
        return raw


def _family_bindings(families: Sequence[SlotFamily]) -> list[JsonValue]:
    return [
        {"slot": item.slot, "family_id": item.family.family_id, "required": item.required}
        for item in sorted(families, key=lambda item: item.slot)
    ]


def snapshot_id_v2(
    workspace_id: str,
    base_snapshot_id: str | None,
    job_id: str,
    config_sha: str,
    families: Sequence[SlotFamily],
    page_ids: Sequence[str],
    *,
    root_batch_ids: Sequence[str] = (),
) -> str:
    """Bind a snapshot to its base, job, configuration, families and pages."""
    return digest(
        "snapshot-v2",
        workspace_id,
        base_snapshot_id,
        job_id,
        config_sha,
        _family_bindings(families),
        sorted(page_ids),
        sorted(root_batch_ids),
    )


def membership_sha(page_ids: Sequence[str]) -> str:
    return digest("snapshot-membership-v2", sorted(page_ids))
```

- [ ] **Step 4: Register the format-2 manifest versions**

In `src/tychora/contracts/artifacts.py` replace `MANIFEST_TYPES` and `validate_manifest_schema` (lines 38-67) with:

```python
MANIFEST_VERSIONS: dict[str, str | int] = {
    "repository_capture": "1",
    "extraction": "1",
    "assertion": "1",
    "assertion_index": "1",
    "registry": "1",
    "index_input": "1",
    "snapshot": "2",
    "membership_page": "2",
    "publication_plan": "2",
    "vector_cache": "1",
    "job_capture": "1",
    "retained_run": "1",
    "evaluation": "1",
    "backup": 1,
}
MANIFEST_TYPES = frozenset(MANIFEST_VERSIONS)
CAPTURE_TYPES = frozenset({"repository_capture", "job_capture"})


def validate_manifest_schema(kind: object, version: object) -> None:
    if not isinstance(kind, str) or kind not in MANIFEST_VERSIONS:
        raise ValueError("Unknown required manifest type")
    expected = MANIFEST_VERSIONS[kind]
    if type(version) is not type(expected) or version != expected:
        raise ValueError("Unknown required manifest schema version")
```

This makes the old `"snapshot"`/`"1"` documents invalid. The only writers are `publication_manifest` and `resolve_snapshot` in `adapters/sqlite/publication.py`; until Task 9 replaces them they use the new version string. In `src/tychora/adapters/sqlite/publication.py` change the three literals `"schema_version": "1"` in `_manifest_document` (line 74), `ManifestRef(artifact=plan.manifest, type="snapshot", schema_version="1")` (line 102) and `document.get("schema_version") != "1"` (line 712) to `"2"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/contracts/test_snapshots.py tests/storage/test_references.py tests/indexing/test_publication.py -v`
Expected: PASS. If `test_oversized_page_reports_reason` fails because `TychoraError` has no `reason` attribute, phase 1 is not merged: stop.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/contracts/snapshots.py src/tychora/contracts/artifacts.py src/tychora/contracts/publication.py src/tychora/adapters/sqlite/publication.py tests/contracts/test_snapshots.py
git commit -m "feat: add membership page contracts and format-2 manifest versions"
```

---

### Task 2: Vector batch artifact codec

**Files:**
- Create: `src/tychora/indexing/vector_batches.py`
- Test: `tests/indexing/test_vector_batches.py` (new)

**Interfaces:**
- Consumes: `tychora.indexing.receipts.vector_value_sha(stored_float32: bytes) -> str`, `canonical_bytes`.
- Produces: `VECTOR_BATCH_MAGIC = b"TYVB0001"`, `VECTOR_BATCH_MEDIA_TYPE = "application/vnd.tychora.vector-batch.v1"`, `VectorBatchRow(record_id, input_sha, values)`, `DecodedVectorBatch(family_id, dimension, rows)`, `encode_vector_batch(family_id: str, dimension: int, rows: Sequence[VectorBatchRow]) -> bytes`, `decode_vector_batch(raw: bytes) -> DecodedVectorBatch`, `row_value_sha(values: Sequence[float]) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/indexing/test_vector_batches.py
"""One artifact per vector batch, verified row by row."""

import json
import struct

import pytest

from tychora.contracts.errors import TychoraError
from tychora.indexing.vector_batches import (
    VECTOR_BATCH_MAGIC,
    VectorBatchRow,
    decode_vector_batch,
    encode_vector_batch,
    row_value_sha,
)


def _rows():
    return (
        VectorBatchRow(record_id="b" * 64, input_sha="1" * 64, values=(0.0, 1.0)),
        VectorBatchRow(record_id="a" * 64, input_sha="2" * 64, values=(1.0, 0.0)),
    )


def test_layout_is_magic_header_then_rows_in_record_order():
    raw = encode_vector_batch("f" * 64, 2, _rows())
    assert raw[:8] == VECTOR_BATCH_MAGIC
    (length,) = struct.unpack("<I", raw[8:12])
    header = json.loads(raw[12 : 12 + length])
    assert header == {
        "dimension": 2,
        "family_id": "f" * 64,
        "records": [
            {"input_sha": "2" * 64, "record_id": "a" * 64, "value_sha": row_value_sha((1.0, 0.0))},
            {"input_sha": "1" * 64, "record_id": "b" * 64, "value_sha": row_value_sha((0.0, 1.0))},
        ],
    }
    body = raw[12 + length :]
    assert struct.unpack("<4f", body) == (1.0, 0.0, 0.0, 1.0)


def test_round_trip_preserves_rows():
    decoded = decode_vector_batch(encode_vector_batch("f" * 64, 2, _rows()))
    assert decoded.family_id == "f" * 64
    assert [row.record_id for row in decoded.rows] == ["a" * 64, "b" * 64]
    assert decoded.rows[0].values == (1.0, 0.0)


def test_value_sha_is_one_row_float32_bytes():
    from hashlib import sha256

    assert row_value_sha((1.0, 0.0)) == sha256(struct.pack("<2f", 1.0, 0.0)).hexdigest()


@pytest.mark.parametrize("damage", ["magic", "row", "truncate", "dimension"])
def test_damaged_batch_is_an_integrity_error(damage):
    raw = bytearray(encode_vector_batch("f" * 64, 2, _rows()))
    if damage == "magic":
        raw[0:8] = b"TYVB0002"
    elif damage == "row":
        raw[-1] ^= 0x01
    elif damage == "truncate":
        raw = raw[:-4]
    else:
        raw = bytearray(encode_vector_batch("f" * 64, 2, _rows()).replace(b'"dimension":2', b'"dimension":3'))
    with pytest.raises(TychoraError) as error:
        decode_vector_batch(bytes(raw))
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason in {"payload.hash_mismatch", "canonical.row_invalid"}


def test_rows_must_match_dimension():
    with pytest.raises(ValueError, match="dimension"):
        encode_vector_batch("f" * 64, 3, _rows())


def test_empty_batch_encodes_header_only():
    raw = encode_vector_batch("f" * 64, 2, ())
    assert decode_vector_batch(raw).rows == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_vector_batches.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.indexing.vector_batches'`.

- [ ] **Step 3: Implement the codec**

```python
# src/tychora/indexing/vector_batches.py
"""Encode and verify one vector batch artifact (`TYVB0001`)."""

import json
import struct
from collections.abc import Sequence

from tychora.contracts.base import Contract, Identifier, PositiveInt, Sha256
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.indexing.receipts import vector_value_sha

VECTOR_BATCH_MAGIC = b"TYVB0001"
VECTOR_BATCH_MEDIA_TYPE = "application/vnd.tychora.vector-batch.v1"
_HEADER_LENGTH = struct.Struct("<I")
_MAX_HEADER_BYTES = 64 * 1024 * 1024


class VectorBatchRow(Contract):
    record_id: Identifier
    input_sha: Sha256
    values: tuple[float, ...]


class DecodedVectorBatch(Contract):
    family_id: Sha256
    dimension: PositiveInt
    rows: tuple[VectorBatchRow, ...]


def _pack(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def row_value_sha(values: Sequence[float]) -> str:
    """Hash one row's little-endian float32 bytes, as receipts do."""
    return vector_value_sha(_pack(values))


def encode_vector_batch(
    family_id: str, dimension: int, rows: Sequence[VectorBatchRow]
) -> bytes:
    """Return the artifact bytes: magic, header length, header, float32 rows."""
    ordered = sorted(rows, key=lambda row: row.record_id)
    if any(len(row.values) != dimension for row in ordered):
        raise ValueError("Vector row length differs from the family dimension")
    header = canonical_bytes(
        {
            "family_id": family_id,
            "dimension": dimension,
            "records": [
                {
                    "record_id": row.record_id,
                    "input_sha": row.input_sha,
                    "value_sha": row_value_sha(row.values),
                }
                for row in ordered
            ],
        }
    )
    body = b"".join(_pack(row.values) for row in ordered)
    return VECTOR_BATCH_MAGIC + _HEADER_LENGTH.pack(len(header)) + header + body


def _invalid(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


def _header(raw: bytes) -> tuple[dict, int]:
    if raw[:8] != VECTOR_BATCH_MAGIC or len(raw) < 12:
        raise _invalid("Vector batch magic differs")
    (length,) = _HEADER_LENGTH.unpack(raw[8:12])
    if length > _MAX_HEADER_BYTES or 12 + length > len(raw):
        raise _invalid("Vector batch header length is invalid")
    encoded = raw[12 : 12 + length]
    try:
        header = json.loads(encoded)
    except (ValueError, UnicodeDecodeError):
        raise _invalid("Vector batch header is malformed") from None
    if not isinstance(header, dict) or canonical_bytes(header) != encoded:
        raise _invalid("Vector batch header is not canonical")
    return header, 12 + length


def decode_vector_batch(raw: bytes) -> DecodedVectorBatch:
    """Decode and verify every row against its recorded value digest."""
    header, offset = _header(raw)
    try:
        dimension = int(header["dimension"])
        records = list(header["records"])
        family_id = str(header["family_id"])
    except (KeyError, TypeError, ValueError):
        raise _invalid("Vector batch header fields are missing") from None
    width = 4 * dimension
    if dimension < 1 or len(raw) - offset != width * len(records):
        raise _invalid("Vector batch body length differs from its header")
    rows = []
    for index, record in enumerate(records):
        chunk = raw[offset + index * width : offset + (index + 1) * width]
        if vector_value_sha(chunk) != record.get("value_sha"):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Vector batch row differs from its digest",
                reason="payload.hash_mismatch",
            )
        rows.append(
            VectorBatchRow(
                record_id=record["record_id"],
                input_sha=record["input_sha"],
                values=struct.unpack(f"<{dimension}f", chunk),
            )
        )
    if [row.record_id for row in rows] != sorted(row.record_id for row in rows):
        raise _invalid("Vector batch records are not sorted")
    return DecodedVectorBatch(family_id=family_id, dimension=dimension, rows=tuple(rows))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/indexing/test_vector_batches.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tychora/indexing/vector_batches.py tests/indexing/test_vector_batches.py
git commit -m "feat: add the TYVB0001 vector batch artifact codec"
```

---

### Task 3: Retained configuration digest and dump-free lane lookup (D7)

**Files:**
- Modify: `src/tychora/configuration/models.py:274-283` (`ResolvedConfig`)
- Modify: `src/tychora/application/sources.py:212-226` (`_config_ref`)
- Modify: `src/tychora/retrieval/segments.py:56-79` (`_lane`, `_role`)
- Test: `tests/configuration/test_resolve.py` (append), `tests/retrieval/test_segments.py` (append), `tests/indexing/test_sources.py` (append)

**Interfaces:**
- Consumes (phase 1): `SqliteUnitOfWork(db)`, `ReadTransaction.artifacts.get(sha256) -> ArtifactRef | None`.
- Produces: `ResolvedConfig.retained_sha: str` (a cached property, computed once per config object; see "Index changes" in the self-review), `retained_registry_document(config: ResolvedConfig) -> dict` in `configuration/models.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/configuration/test_resolve.py`:

```python
def test_retained_sha_is_the_registry_artifact_digest(tmp_path):
    from hashlib import sha256

    from tychora.configuration.models import retained_registry_document
    from tychora.contracts.identity import canonical_bytes

    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    expected = sha256(canonical_bytes(retained_registry_document(config))).hexdigest()
    assert config.retained_sha == expected
    assert "retained_sha" not in config.model_dump(mode="json")
```

Append to `tests/retrieval/test_segments.py`:

```python
def test_lane_and_role_read_the_frozen_registry_without_dumping(tmp_path, monkeypatch):
    from tychora.configuration.models import ResolvedConfig
    from tychora.retrieval import segments

    resolved = config(tmp_path)  # the module's existing helper at line 39

    def forbidden(self, *args, **kwargs):
        raise AssertionError("lane lookup must not dump the configuration")

    monkeypatch.setattr(ResolvedConfig, "model_dump", forbidden)
    assert segments._lane(resolved, "manual@1")
    assert segments._role(resolved, "manual@1", {})
```

Append to `tests/indexing/test_sources.py`:

```python
@pytest.mark.asyncio
async def test_registry_artifact_is_written_once_per_retained_sha(tmp_path, monkeypatch):
    from tychora import SourceInput, Tychora

    from tests.api.test_public_sync import CharacterCounter

    writes = []
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        artifacts = kb._workspace.owner.artifacts
        original = artifacts.put_stream

        def recording(stream, media_type):
            ref = original(stream, media_type)
            writes.append(ref.sha256)
            return ref

        monkeypatch.setattr(artifacts, "put_stream", recording)
        for index in range(2):
            job = kb.sources.add(
                SourceInput(
                    type="text",
                    source_key=f"manual-{index}",
                    collection_id=kb.default_collection_id,
                    kind="manual@1",
                    format="text",
                    metadata={"title": "T", "publisher": "P", "edition": "1"},
                    text=f"Record verification outcome {index}.",
                )
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        retained = kb._workspace.owner.config.retained_sha
    assert retained not in writes
```

The first workspace open writes the registry artifact before the patch is installed; the assertion checks that no add writes it again.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/configuration/test_resolve.py::test_retained_sha_is_the_registry_artifact_digest tests/retrieval/test_segments.py::test_lane_and_role_read_the_frozen_registry_without_dumping tests/indexing/test_sources.py::test_registry_artifact_is_written_once_per_retained_sha -v`
Expected: FAIL (`ImportError: cannot import name 'retained_registry_document'`, `AssertionError: lane lookup must not dump`, and the registry SHA appearing in `writes`).

- [ ] **Step 3: Implement `retained_sha`**

In `src/tychora/configuration/models.py` add `from functools import cached_property`, `from hashlib import sha256` and `from tychora.contracts.identity import canonical_bytes` to the imports, and inside `class ResolvedConfig` (after `output_fingerprint`):

```python
    @cached_property
    def retained_sha(self) -> str:
        """SHA-256 of the retained registry document stored for this configuration."""
        return sha256(canonical_bytes(retained_registry_document(self))).hexdigest()
```

After the class:

```python
def retained_registry_document(config: "ResolvedConfig") -> dict:
    """The canonical registry artifact that jobs and snapshots bind by digest."""
    return {
        "type": "registry",
        "schema_version": "1",
        "data": config.model_dump(mode="json"),
    }
```

Pydantic 2 ignores `functools.cached_property` attributes in `model_dump`, and frozen models allow them because the cache is written to the instance `__dict__`.

- [ ] **Step 4: Write the registry artifact once**

Replace `SourceService._config_ref` in `src/tychora/application/sources.py:212-226`:

```python
    async def _config_ref(self, ctx: ExecutionContext):
        def ensure() -> ArtifactRef:
            config = self.owner.config
            with SqliteUnitOfWork(self.owner.db).read() as tx:
                existing = tx.artifacts.get(config.retained_sha)
            if existing is not None:
                return existing
            ref = self.owner.artifacts.put_stream(
                BytesIO(canonical_bytes(retained_registry_document(config))),
                "application/json",
            )
            if ref.sha256 != config.retained_sha:
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Retained registry digest differs from its document",
                    reason="config.drift",
                )
            return ref

        return await self.owner.run_blocking(ensure, ctx)
```

Add the imports `from tychora.adapters.sqlite.repositories import SqliteUnitOfWork` and `from tychora.configuration.models import retained_registry_document` (`ArtifactRef` is already imported lazily in this module; add `from tychora.contracts.artifacts import ArtifactRef` at the top if the name is not in module scope). The artifact is registered in SQL by `JobRepository.accept` (`adapters/sqlite/jobs.py:167`), which is why the second add finds it.

- [ ] **Step 5: Look up lanes and roles in the frozen registry**

Replace `_lane` and `_role` in `src/tychora/retrieval/segments.py:56-79`:

```python
def _lane(config: ResolvedConfig, kind: str) -> str:
    registry = config.registry
    lanes = cast(Mapping[str, Mapping[str, object]], registry["lanes"])
    for name, lane in lanes.items():
        if kind in cast(tuple[str, ...], lane["kinds"]):
            return str(name)
    compatibility = cast(Mapping[str, object], registry["compatibility"])
    kind_lanes = cast(Mapping[str, object], compatibility.get("kind_lanes", {}))
    if kind in kind_lanes:
        return str(kind_lanes[kind])
    raise TychoraError("INTEGRITY_ERROR", "Selected kind has no lane.", reason="config.drift")


def _role(
    config: ResolvedConfig, kind: str, historical_kind_roles: Mapping[str, str]
) -> str:
    kinds = cast(Mapping[str, Mapping[str, object]], config.registry["kinds"])
    if kind in kinds:
        return str(kinds[kind]["role"])
    if kind in historical_kind_roles and _lane(config, kind):
        role = historical_kind_roles[kind]
        if isinstance(role, str) and role:
            return role
    raise TychoraError(
        "PROFILE_INCOMPATIBLE", "Selected historical kind role is unavailable."
    )
```

Add `from typing import cast` if it is not imported. `config.registry` is a `FrozenJsonMap`; arrays inside it are frozen to tuples, so `in` works on `lane["kinds"]`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/configuration tests/retrieval/test_segments.py tests/indexing/test_sources.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/configuration/models.py src/tychora/application/sources.py src/tychora/retrieval/segments.py tests/configuration/test_resolve.py tests/retrieval/test_segments.py tests/indexing/test_sources.py
git commit -m "perf: write the registry artifact once and read lanes without dumping config"
```

---

### Task 4: Family creation nonces and family-bound vector record IDs (C5)

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql:259-270` (`index_families` gains `identity_sha`, `creation_nonce`)
- Modify: `src/tychora/adapters/sqlite/migrations.py:10-11` (`SCHEMA_CHECKSUM`)
- Modify: `src/tychora/ports/repositories.py` (add `FamilyRepository`)
- Create: `src/tychora/adapters/sqlite/repositories/families.py`
- Modify: `src/tychora/adapters/sqlite/repositories/__init__.py` (`SqliteTransaction.families`)
- Modify: `src/tychora/indexing/coordinator.py:340-353` (`_lexical_family`), `:396-460` (`_text_family`), `:462-552` (`_text_batch`), `:554-617` (`_image_family`), `:680-767` (`_image_batch`), `:769-842` (`_graph_slot` family block)
- Modify: `src/tychora/adapters/sqlite/publication.py:323-338` (family insert becomes a check)
- Test: `tests/storage/test_families.py` (new), `tests/storage/test_schema.py` (append), `tests/indexing/test_publication.py` (update `RecordingEmbedder` users and `PublishedCorpus.plan`), `tests/providers/test_text.py:241` (unchanged: it tests the provider-level ID)

**Interfaces:**
- Consumes (phase 1): `SqliteUnitOfWork`, `SqliteTransaction` (the object `SqliteUnitOfWork.read()`/`write()` yields, holding `workspace` and `artifacts`), `contracts.identity.family_id`, `new_family_nonce`, `vector_record_id`.
- Produces:

```python
class FamilyRepository(Protocol):
    def get(self, family_id: str) -> IndexFamily | None: ...
    def nonce(self, channel: str, identity_sha: str) -> str | None: ...
    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None: ...
```

`identity_sha` is the configuration digest bound into `family_id` (the retained `config_sha` for lexical; the `text-family-config-v1`, `image-family-config-v1` or `graph-family-config-v2` digest for the others). `IndexFamily.config_sha` keeps meaning "native handle configuration" for vector families, so it cannot serve as the lookup key.

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_families.py
"""Persisted family nonces make family identity independent of configuration alone."""

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.identity import family_id, new_family_nonce
from tychora.contracts.snapshots import IndexFamily


def _family(workspace_id: str, identity: str, nonce: str) -> IndexFamily:
    return IndexFamily(
        family_id=family_id(workspace_id, identity, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha=identity,
        config_json=FrozenJsonMap({"effective_config_sha": identity}),
        backend_namespace="lexical",
        created_at="2026-10-16T00:00:00+00:00",
    )


def test_nonce_round_trip_and_idempotent_create(tmp_path):
    db = open_corpus(tmp_path / "c.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    bundle = seed_bundle()
    store.commit_extraction(bundle)
    workspace = bundle.workspace.workspace_id
    uow = SqliteUnitOfWork(db)
    with uow.read() as tx:
        assert tx.families.nonce("lexical", "c" * 64) is None
    nonce = new_family_nonce()
    family = _family(workspace, "c" * 64, nonce)
    with uow.write() as tx:
        tx.families.create(family, nonce, "c" * 64)
        tx.families.create(family, nonce, "c" * 64)
    with uow.read() as tx:
        assert tx.families.nonce("lexical", "c" * 64) == nonce
        assert tx.families.get(family.family_id) == family


def test_two_nonces_give_two_families_for_one_configuration(tmp_path):
    first = _family("w", "c" * 64, new_family_nonce())
    second = _family("w", "c" * 64, new_family_nonce())
    assert first.family_id != second.family_id
```

Append to `tests/storage/test_schema.py`:

```python
def test_checksum_change_reports_format_old(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    db.execute("UPDATE schema_migrations SET checksum=?", ("0" * 64,))
    db.close()
    with pytest.raises(TychoraError) as error:
        open_corpus(path)
    assert error.value.code == "PROFILE_INCOMPATIBLE"
    assert error.value.reason == "workspace.format_old"
```

This pins the phase-1 behaviour that every later DDL change relies on: a workspace created before a checksum change is reported as an old format.

In `tests/indexing/test_publication.py`, `RecordingEmbedder.embed_groups` keeps producing provider-level IDs (`embedding-record-v1`); add this test at the end of the file:

```python
def test_vector_record_ids_are_bound_to_the_family(projected_corpus):
    from tychora.contracts.identity import vector_record_id

    f = projected_corpus
    rows = f.store.db.execute(
        "SELECT v.record_id,v.chunk_id,v.input_sha,b.family_id FROM vector_records v "
        "JOIN index_batches b USING(batch_id)"
    ).fetchall()
    assert rows
    for record_id, chunk_id, input_sha, family in rows:
        assert record_id == vector_record_id(family, chunk_id, input_sha)
    nonce = f.store.db.execute(
        "SELECT creation_nonce FROM index_families WHERE channel='text'"
    ).fetchone()[0]
    assert len(nonce) == 36
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_families.py tests/storage/test_schema.py::test_checksum_change_reports_format_old tests/indexing/test_publication.py::test_vector_record_ids_are_bound_to_the_family -v`
Expected: FAIL (`AttributeError: ... has no attribute 'families'`; the vector test fails on the ID comparison). `test_checksum_change_reports_format_old` passes already if phase 1 is merged; keep it as the regression pin.

- [ ] **Step 3: Add the columns**

In `src/tychora/resources/migrations/0001.sql` replace the `index_families` table (lines 259-270):

```sql
CREATE TABLE index_families (
  family_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL CHECK(channel IN ('lexical','text','image','graph')),
  backend TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  dimension INTEGER CHECK(dimension IS NULL OR dimension>0),
  config_sha TEXT NOT NULL,
  config_json TEXT NOT NULL CHECK(json_valid(config_json)),
  backend_namespace TEXT NOT NULL,
  identity_sha TEXT NOT NULL CHECK(length(identity_sha)=64),
  creation_nonce TEXT NOT NULL CHECK(length(creation_nonce)=36),
  created_at TEXT NOT NULL,
  UNIQUE(channel,identity_sha,creation_nonce)
) STRICT;
CREATE INDEX families_identity ON index_families(channel,identity_sha,created_at);
```

Recompute the checksum and paste it into `SCHEMA_CHECKSUM`:

Run: `uv run --locked python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('src/tychora/resources/migrations/0001.sql').read_bytes()).hexdigest())"`
Expected: one 64-character hex line; set `SCHEMA_CHECKSUM = "<that value>"` in `src/tychora/adapters/sqlite/migrations.py`.

- [ ] **Step 4: Add the port and the SQLite repository**

Append to `src/tychora/ports/repositories.py` (import `IndexFamily` from `tychora.contracts.snapshots`):

```python
class FamilyRepository(Protocol):
    def get(self, family_id: str) -> IndexFamily | None: ...
    def nonce(self, channel: str, identity_sha: str) -> str | None: ...
    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None: ...
```

Add `families: FamilyRepository` to `ReadTransaction` (it is inherited by `WriteTransaction`).

```python
# src/tychora/adapters/sqlite/repositories/families.py
"""Index family rows and their persisted creation nonces."""

import json
import sqlite3

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.snapshots import IndexFamily

_COLUMNS = (
    "family_id,channel,backend,provider,model,dimension,config_sha,config_json,"
    "backend_namespace,created_at"
)


def family_from_row(row: tuple) -> IndexFamily:
    try:
        return IndexFamily(
            family_id=row[0],
            channel=row[1],
            backend=row[2],
            provider=row[3],
            model=row[4],
            dimension=row[5],
            config_sha=row[6],
            config_json=FrozenJsonMap(json.loads(row[7])),
            backend_namespace=row[8],
            created_at=row[9],
        )
    except ValueError:
        raise TychoraError(
            "INTEGRITY_ERROR", "Index family row is invalid", reason="canonical.row_invalid"
        ) from None


class SqliteFamilyRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def get(self, family_id: str) -> IndexFamily | None:
        row = self.db.execute(
            f"SELECT {_COLUMNS} FROM index_families WHERE family_id=?", (family_id,)
        ).fetchone()
        return None if row is None else family_from_row(row)

    def nonce(self, channel: str, identity_sha: str) -> str | None:
        row = self.db.execute(
            "SELECT creation_nonce FROM index_families WHERE channel=? AND identity_sha=? "
            "ORDER BY created_at,family_id LIMIT 1",
            (channel, identity_sha),
        ).fetchone()
        return None if row is None else row[0]

    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None:
        values = (
            family.family_id,
            family.channel,
            family.backend,
            family.provider,
            family.model,
            family.dimension,
            family.config_sha,
            canonical_bytes(family.config_json).decode(),
            family.backend_namespace,
            family.created_at,
        )
        self.db.execute(
            f"INSERT INTO index_families ({_COLUMNS},identity_sha,creation_nonce) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(family_id) DO NOTHING",
            (*values, identity_sha, nonce),
        )
        stored = self.db.execute(
            f"SELECT {_COLUMNS},identity_sha,creation_nonce FROM index_families WHERE family_id=?",
            (family.family_id,),
        ).fetchone()
        if stored != (*values, identity_sha, nonce):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Index family differs from its immutable row",
                reason="canonical.binding_mismatch",
            )
```

In `src/tychora/adapters/sqlite/repositories/__init__.py`, inside `SqliteTransaction.__init__`, add:

```python
        self.families = SqliteFamilyRepository(db)
```

with `from .families import SqliteFamilyRepository`.

- [ ] **Step 5: Mint and reuse nonces in the coordinator**

Add a helper method to `ProjectionCoordinator` (`src/tychora/indexing/coordinator.py`):

```python
    def _family_nonce(self, channel: str, identity_sha: str) -> str:
        """Return the nonce of the family already created for this identity, or a new one."""
        with SqliteUnitOfWork(self.store.db).read() as tx:
            existing = tx.families.nonce(channel, identity_sha)
        return existing or new_family_nonce()

    def _persist_family(self, family: IndexFamily, nonce: str, identity_sha: str) -> None:
        with SqliteUnitOfWork(self.store.db).write() as tx:
            tx.families.create(family, nonce, identity_sha)
```

Imports: `from tychora.adapters.sqlite.repositories import SqliteUnitOfWork`, `from tychora.contracts.identity import new_family_nonce, vector_record_id`, and drop `uuid5`/`UUID` once unused.

Replace `_lexical_family` (lines 340-353) with an instance method:

```python
    def _lexical_family(
        self, workspace_id: str, config_sha: str, created_at: str
    ) -> IndexFamily:
        nonce = self._family_nonce("lexical", config_sha)
        family = IndexFamily(
            family_id=derive_family_id(workspace_id, config_sha, nonce),
            channel="lexical",
            backend="sqlite-fts5",
            config_sha=config_sha,
            config_json=FrozenJsonMap(
                {"tokenizer": "unicode61", "effective_config_sha": config_sha}
            ),
            backend_namespace="lexical",
            created_at=created_at,
        )
        self._persist_family(family, nonce, config_sha)
        return family
```

In `_text_family` (lines 396-460) replace

```python
        creation_uuid = str(uuid5(UUID(workspace_id), "text:" + family_config))
```

with

```python
        creation_uuid = self._family_nonce("text", family_config)
```

and, after `family = IndexFamily(...)` is built, add `self._persist_family(family, creation_uuid, family_config)`. Make the same two changes in `_image_family` (lines 554-617) with channel `"image"`.

In `_graph_slot` (lines 769-800) replace

```python
        family_id = digest("graph-family-v2", workspace_id, graph_config)
```

with

```python
        nonce = self._family_nonce("graph", graph_config)
        family_id = derive_family_id(workspace_id, graph_config, nonce)
```

and add `self._persist_family(family, nonce, graph_config)` after `family = IndexFamily(...)`.

- [ ] **Step 6: Re-key provider records by family**

Providers keep returning their own request-level IDs (`embedding-record-v1`, `visual-embedding-record-v1`); the coordinator maps them to stored IDs. In `_text_batch` (lines 462-552), after the loop that validates each `item` against `expected[item.record_id]`, build stored records:

```python
        stored_records = []
        for item in records:
            window_id, group = expected[item.record_id]
            stored_id = vector_record_id(family.family_id, window_id, item.input_sha)
            stored_records.append(item.model_copy(update={"record_id": stored_id}))
```

Use `stored_id` as `VectorRecordBinding.record_id`, and pass `tuple(sorted(stored_records, key=lambda r: r.record_id))` to `self.text_index.upsert_batch(...)` instead of `ordered`. Do the same in `_image_batch` with `vector_record_id(family.family_id, item.asset.asset_id, record.input_sha)`.

`model_copy(update=...)` skips validation, which is correct here because only the identifier changes and `EmbeddingRecord`'s validator checks values, not IDs.

- [ ] **Step 7: Stop inserting families at publication**

In `insert_snapshot_bindings` (`src/tychora/adapters/sqlite/publication.py:323-338`) replace the `store._insert("index_families", ...)` call with a presence check:

```python
        if (
            store.db.execute(
                "SELECT 1 FROM index_families WHERE family_id=?", (family.family_id,)
            ).fetchone()
            is None
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Publication family was not created before its batches",
                reason="canonical.binding_mismatch",
            )
```

Update `tests/indexing/test_publication.py::PublishedCorpus.plan` (lines 179-276): replace the `family_id = digest("lexical-family-v1", ...)` computation with

```python
        nonce = new_family_nonce()
        family_id = derive_family_id(
            bundle.workspace.workspace_id, self.config.sha256, nonce
        )
```

and, after building `family`, persist it:

```python
        with SqliteUnitOfWork(self.store.db).write() as tx:
            tx.families.create(family, nonce, self.config.sha256)
```

(import `family_id as derive_family_id`, `new_family_nonce` from `tychora.contracts.identity` and `SqliteUnitOfWork`). Apply the same edit to the other hand-built plans that `rg -n '"lexical-family-v1"' tests` lists (expected: `tests/indexing/test_publication.py`, `tests/retrieval/test_eligibility.py`, `tests/operations/test_restore.py`).

- [ ] **Step 8: Run the affected suites**

Run: `uv run --locked pytest tests/storage tests/indexing tests/retrieval/test_eligibility.py tests/operations/test_restore.py tests/acceptance/test_combined_channels.py -v`
Expected: PASS.

- [ ] **Step 9: Run the full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS (no new failures).

- [ ] **Step 10: Commit**

```bash
git add src/tychora/resources/migrations/0001.sql src/tychora/adapters/sqlite/migrations.py src/tychora/ports/repositories.py src/tychora/adapters/sqlite/repositories src/tychora/indexing/coordinator.py src/tychora/adapters/sqlite/publication.py tests/storage/test_families.py tests/storage/test_schema.py tests/indexing/test_publication.py tests/retrieval/test_eligibility.py tests/operations/test_restore.py
git commit -m "feat: persist family creation nonces and bind vector record IDs to families"
```

---
### Task 5: One artifact per vector batch (D6)

This task changes the vector storage under the existing (format-1-shaped) publication plan, so that the later switch to pages does not carry per-vector files along.

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql:272-296` (`index_batches.vectors_sha`; `vector_records.row_index` replaces `embedding_cache_sha`), `src/tychora/adapters/sqlite/migrations.py` (`SCHEMA_CHECKSUM`)
- Modify: `src/tychora/contracts/publication.py:49-89` (`VectorRecordBinding.row_index`, `PublicationBatch.vectors`)
- Modify: `src/tychora/indexing/coordinator.py:462-552` (`_text_batch`), `:680-767` (`_image_batch`)
- Modify: `src/tychora/adapters/sqlite/publication.py:60-150` (`_manifest_document`, `_verify_artifacts`), `:152-286` (`_verify_batches` vector branch), `:304-458` (`insert_snapshot_bindings`), `:532-571` (payload pins)
- Modify: `src/tychora/adapters/sqlite/corpus.py:193-226` (`pin_snapshot` payloads)
- Modify: `src/tychora/application/recovery.py:140-246` (`verify_canonical_native_batch`), `:249-327` (`verified_vector_records`)
- Modify: `src/tychora/adapters/files/references.py:323` (backup root), `src/tychora/application/removal.py:174-183,442` (retention queries)
- Test: `tests/indexing/test_publication.py` (append), `tests/retrieval/test_eligibility.py:40-215` and `tests/operations/test_restore.py:590-670` (hand-built bindings)

**Interfaces:**
- Consumes: `encode_vector_batch`, `decode_vector_batch`, `VectorBatchRow`, `row_value_sha`, `VECTOR_BATCH_MEDIA_TYPE` (Task 2).
- Produces: `VectorRecordBinding.row_index: NonNegativeInt` (replaces `embedding_cache`), `PublicationBatch.vectors: ArtifactRef | None`, `vector_batch_records(family: IndexFamily, batch, artifacts) -> VectorBatch` in `application/recovery.py` (renamed from `verified_vector_records`). Page batches in Task 9 carry the same artifact as `PageBatch.vectors`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/indexing/test_publication.py`:

```python
def test_large_text_batch_is_one_artifact(projected_corpus):
    f = projected_corpus
    text = "\n\n".join(f"Paragraph {index} records one outcome." for index in range(400))
    f.add_revision(text, source_key="large.md")
    media = dict(
        f.store.db.execute(
            "SELECT media_type,COUNT(*) FROM artifacts GROUP BY media_type"
        ).fetchall()
    )
    assert "application/vnd.tychora.float32-le" not in media
    batches = f.store.db.execute(
        "SELECT b.batch_id,b.vectors_sha,COUNT(v.record_id) FROM index_batches b "
        "JOIN vector_records v USING(batch_id) GROUP BY b.batch_id"
    ).fetchall()
    assert batches and all(sha is not None for _, sha, _ in batches)
    assert media["application/vnd.tychora.vector-batch.v1"] == len(batches)
    indexes = [
        row[0]
        for row in f.store.db.execute(
            "SELECT row_index FROM vector_records WHERE batch_id=? ORDER BY record_id",
            (batches[-1][0],),
        )
    ]
    assert indexes == list(range(len(indexes)))


def test_missing_vector_batch_artifact_reports_cache_absent(projected_corpus):
    from tychora.application.recovery import vector_batch_records

    f = projected_corpus
    view = f.store.resolve_snapshot(None)
    slot = next(slot for slot in view.slots if slot.slot == "text")
    batch = slot.batches[0]
    missing = batch.model_copy(
        update={
            "vectors": batch.vectors.model_copy(
                update={"sha256": "0" * 64, "storage_key": "0" * 64}
            )
        }
    )
    with pytest.raises(TychoraError) as error:
        vector_batch_records(slot.family, missing, f.store.artifacts)
    assert error.value.code == "CAPABILITY_MISSING"
    assert error.value.reason == "vectors.cache_absent"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_publication.py::test_large_text_batch_is_one_artifact tests/indexing/test_publication.py::test_missing_vector_batch_artifact_reports_cache_absent -v`
Expected: FAIL (`sqlite3.OperationalError: no such column: b.vectors_sha` and `ImportError: cannot import name 'vector_batch_records'`).

- [ ] **Step 3: Change the DDL**

In `0001.sql`, add to `index_batches` after `receipt_json`:

```sql
  vectors_sha TEXT REFERENCES artifacts(artifact_sha),
```

and replace the `vector_records` table:

```sql
CREATE TABLE vector_records (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  chunk_id TEXT REFERENCES chunks(chunk_id),
  asset_id TEXT REFERENCES assets(asset_id),
  input_sha TEXT NOT NULL,
  row_index INTEGER NOT NULL CHECK(row_index>=0),
  CHECK((chunk_id IS NOT NULL)+(asset_id IS NOT NULL)=1),
  UNIQUE(batch_id,chunk_id),
  UNIQUE(batch_id,asset_id),
  UNIQUE(batch_id,row_index)
) STRICT;
```

Recompute `SCHEMA_CHECKSUM` with the command of Task 4 Step 3.

- [ ] **Step 4: Change the binding contracts**

In `src/tychora/contracts/publication.py`, `VectorRecordBinding` loses `embedding_cache: ArtifactRef` and gains `row_index: NonNegativeInt`. `PublicationBatch` gains:

```python
    vectors: ArtifactRef | None = None

    @model_validator(mode="after")
    def vectors_match_records(self) -> Self:
        if bool(self.vector_records) != (self.vectors is not None):
            raise ValueError("Vector records and their batch artifact must be paired")
        indexes = [row.row_index for row in sorted(self.vector_records, key=lambda r: r.record_id)]
        if indexes != list(range(len(indexes))):
            raise ValueError("Vector row indexes must follow record order")
        return self
```

- [ ] **Step 5: Write one artifact per batch in the coordinator**

Add a helper to `ProjectionCoordinator`:

```python
    def _vector_artifact(
        self, family: IndexFamily, dimension: int, records: tuple[EmbeddingRecord, ...]
    ) -> tuple[ArtifactRef | None, dict[str, int]]:
        """Store the batch's float32 rows once; return the artifact and row positions."""
        if not records:
            return None, {}
        rows = tuple(
            VectorBatchRow(record_id=r.record_id, input_sha=r.input_sha, values=r.values)
            for r in sorted(records, key=lambda r: r.record_id)
        )
        ref = self.artifacts.put_stream(
            BytesIO(encode_vector_batch(family.family_id, dimension, rows)),
            VECTOR_BATCH_MEDIA_TYPE,
        )
        return ref, {row.record_id: index for index, row in enumerate(rows)}
```

In `_text_batch`, delete the per-record `struct.pack` / `put_stream` / `cache.sha256 != item.value_sha` block. After `stored_records` is built (Task 4 Step 6), call

```python
        vectors, positions = self._vector_artifact(family, spec.dimensions, tuple(stored_records))
```

build each `VectorRecordBinding` with `row_index=positions[stored_id]` (no `embedding_cache`), and pass `vectors=vectors` to the returned `PublicationBatch`. Apply the same change to `_image_batch`. Remove the now unused `struct` import if nothing else uses it.

- [ ] **Step 6: Verify and register batch artifacts at publication**

In `src/tychora/adapters/sqlite/publication.py`:

`_manifest_document`: replace the `caches` comprehension by

```python
    caches = {
        batch.vectors.sha256: batch.vectors
        for slot in plan.slots
        for batch in slot.batches
        if batch.vectors is not None
    }
```

`_verify_artifacts`: in `required`, replace the `row.embedding_cache.sha256` generator by `*(batch.vectors.sha256 for slot in plan.slots for batch in slot.batches if batch.vectors is not None)`.

`_verify_batches`, vector branch: replace the `for row in bindings:` loop (lines 230-240) by

```python
                if bindings:
                    assert batch.vectors is not None
                    with store.artifacts.open_verified(batch.vectors) as stream:
                        decoded = decode_vector_batch(stream.read(batch.vectors.size_bytes + 1))
                    if (
                        decoded.family_id != batch.family_id
                        or decoded.dimension != slot.family.dimension
                        or tuple(
                            (row.record_id, row.input_sha, row_value_sha(row.values))
                            for row in decoded.rows
                        )
                        != tuple((b.record_id, b.input_sha, b.value_sha) for b in bindings)
                    ):
                        raise TychoraError(
                            "INTEGRITY_ERROR",
                            "Vector batch artifact differs from its receipt",
                            reason="payload.hash_mismatch",
                        )
```

`insert_snapshot_bindings`: in the artifact registration set replace the `row.embedding_cache` generator by `*(batch.vectors for slot in plan.slots for batch in slot.batches if batch.vectors is not None)`; add `"vectors_sha": batch.vectors.sha256 if batch.vectors else None` to the `index_batches` row; replace `"embedding_cache_sha": row.embedding_cache.sha256` by `"row_index": row.row_index` in the `vector_records` row.

`publish_snapshot`: in `payloads`, replace the `row.embedding_cache.sha256` generator by `*(batch.vectors.sha256 for slot in plan.slots for batch in slot.batches if batch.vectors is not None)`. Make the same replacement in `CorpusStore.pin_snapshot` (`adapters/sqlite/corpus.py:216-221`).

- [ ] **Step 7: Rebuild native vectors from the batch artifact**

Replace `verified_vector_records` in `src/tychora/application/recovery.py:249-327` with:

```python
def vector_batch_records(
    family: IndexFamily, batch: PublicationBatch, artifacts
) -> VectorBatch:
    """Rebuild only the exact retained float32 rows bound to the sealed receipt."""
    receipt = batch.receipt
    spec = _vector_family_spec(family, receipt, batch)
    if batch.vectors is None or not artifacts.stat(batch.vectors).available:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Retained vector batch is absent; rebuild the index to recompute it",
            reason="vectors.cache_absent",
        )
    with artifacts.open_verified(batch.vectors) as stream:
        decoded = decode_vector_batch(stream.read(batch.vectors.size_bytes + 1))
    descriptors = tuple(
        IndexRecordDigest(
            record_id=row.record_id,
            input_sha=row.input_sha,
            value_sha=row_value_sha(row.values),
        )
        for row in decoded.rows
    )
    if (
        decoded.family_id != family.family_id
        or decoded.dimension != spec.dimensions
        or receipt.records != descriptors
        or receipt.digest != receipt_digest(descriptors)
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Vector batch artifact differs from its receipt",
            reason="canonical.binding_mismatch",
        )
    expected_space = space_id(
        spec.modality,
        spec.provider,
        spec.model,
        spec.dimensions,
        spec.metric,
        spec.representation_fingerprint,
    )
    try:
        records = tuple(
            EmbeddingRecord.from_normalized_bytes(
                record_id=row.record_id,
                input_sha=row.input_sha,
                space_id=expected_space,
                stored=struct.pack(f"<{len(row.values)}f", *row.values),
            )
            for row in decoded.rows
        )
    except ValueError:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Retained vector is not normalized float32",
            reason="canonical.row_invalid",
        ) from None
    return VectorBatch(family_id=family.family_id, records=records)
```

Move the family and receipt checks of the old function (the `spec = FamilySpec...` block and the long `if (... ) raise "Vector family or receipt differs"` condition) unchanged into a private `_vector_family_spec(family, receipt, batch) -> FamilySpec` placed above it, so both functions stay under 80 lines. Rename the single caller in `verify_native_vector` (line 359). In `verify_canonical_native_batch` (lines 214-246) replace the per-row `embedding_cache` checks by:

```python
        rows = db.execute(
            "SELECT record_id,batch_id,chunk_id,asset_id,input_sha,row_index "
            "FROM vector_records WHERE batch_id=? ORDER BY record_id",
            (batch.batch_id,),
        ).fetchall()
        expected_rows = sorted(
            (row.record_id, batch.batch_id, row.chunk_id, row.asset_id, row.input_sha, row.row_index)
            for row in batch.vector_records
        )
        if rows != expected_rows:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Native vector SQL differs from sealed plan",
                reason="canonical.binding_mismatch",
            )
```

and add `batch.vectors.sha256 if batch.vectors else None` as the last element of `expected_batch` with `vectors_sha` appended to the `SELECT` at line 150.

- [ ] **Step 8: Retention roots**

`src/tychora/adapters/files/references.py:323`: replace `"SELECT embedding_cache_sha FROM vector_records WHERE embedding_cache_sha IS NOT NULL"` with `"SELECT vectors_sha FROM index_batches WHERE vectors_sha IS NOT NULL"`.

`src/tychora/application/removal.py:174-183`: replace the query with

```python
                "SELECT DISTINCT b.vectors_sha FROM index_batches b "
                "JOIN extractions e USING(extraction_id) "
                "WHERE b.vectors_sha IS NOT NULL AND e.revision_id=?",
                (revision_id,),
```

`src/tychora/application/removal.py:442`: replace `UNION ALL SELECT 1 FROM vector_records WHERE embedding_cache_sha=?` with `UNION ALL SELECT 1 FROM index_batches WHERE vectors_sha=?` (the parameter count stays 18).

- [ ] **Step 9: Update hand-built test bindings**

In `tests/retrieval/test_eligibility.py` (lines 40-215) and `tests/operations/test_restore.py` (lines 590-670), every `VectorRecordBinding(..., embedding_cache=artifact)` becomes `VectorRecordBinding(..., row_index=0)` (or its position in record order), and the enclosing `PublicationBatch` gains

```python
            vectors=reader().put_stream(
                BytesIO(
                    encode_vector_batch(
                        family_id,
                        2,
                        (VectorBatchRow(record_id=record_id, input_sha=input_sha, values=(1.0, 0.0)),),
                    )
                ),
                VECTOR_BATCH_MEDIA_TYPE,
            ),
```

using the record ID, input SHA and stored values the test already uses for the binding (both files pack `(1.0, 0.0)` today). The `value_sha` in the test's `IndexRecordDigest` stays `sha256(struct.pack("<ff", 1.0, 0.0))`, which equals `row_value_sha((1.0, 0.0))`.

- [ ] **Step 10: Run the affected suites, then the full suite**

Run: `uv run --locked pytest tests/indexing tests/storage tests/retrieval tests/operations tests/jobs tests/acceptance -v`
Expected: PASS.

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/tychora tests
git commit -m "perf: store each vector batch as one verified artifact"
```

---

### Task 6: Chroma owner metadata

**Files:**
- Modify: `src/tychora/contracts/vectors.py:20-29` (`VectorOwner`, `VectorBatch.owners`)
- Modify: `src/tychora/adapters/chroma/index.py:236-244` (metadata on `collection.add`)
- Modify: `src/tychora/indexing/coordinator.py` (`_text_batch`, `_image_batch` pass owners)
- Modify: `src/tychora/application/recovery.py` (`vector_batch_records` rebuilds owners from SQL)
- Test: `tests/storage/test_vector_contract.py` (append)

**Interfaces:**
- Produces: `VectorOwner(record_id: Identifier, revision_id: Identifier, extraction_id: Identifier, owner_kind: Literal["unit", "asset"], owner_id: Identifier)`, `VectorBatch.owners: tuple[VectorOwner, ...] = ()` (empty or one per record). Phase 4's vector search filters on `owner_kind` and `revision_id` through this metadata.

- [ ] **Step 1: Write the failing test**

```python
def test_upsert_writes_owner_metadata(vector_index, vector_family, execution):
    from tychora.contracts.vectors import VectorOwner

    index = vector_index  # fixtures from tests/storage/conftest.py
    batch = vector_family.batch([("record", "u0", (1.0, 0.0))])
    owner = VectorOwner(
        record_id=batch.records[0].record_id,
        revision_id="r" * 64,
        extraction_id="x" * 64,
        owner_kind="unit",
        owner_id="u" * 64,
    )
    index.upsert_batch(batch.model_copy(update={"owners": (owner,)}), execution.ctx)
    collection, _ = index._require()
    stored = collection.get(ids=[owner.record_id], include=["metadatas"])["metadatas"][0]
    assert stored["revision_id"] == "r" * 64
    assert stored["extraction_id"] == "x" * 64
    assert stored["owner_kind"] == "unit"
    assert stored["owner_id"] == "u" * 64
```

`vector_family.batch(...)` (`tests/storage/conftest.py`) builds a `VectorBatch` from `(record_id, unit_id, values)` triples, as `test_only_eligible_owners_enter_vector_candidates` uses it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/storage/test_vector_contract.py::test_upsert_writes_owner_metadata -v`
Expected: FAIL with `ImportError: cannot import name 'VectorOwner'`.

- [ ] **Step 3: Implement**

In `src/tychora/contracts/vectors.py`:

```python
class VectorOwner(Contract):
    record_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    owner_kind: Literal["unit", "asset"]
    owner_id: Identifier


class VectorBatch(Contract):
    family_id: Sha256
    records: tuple[EmbeddingRecord, ...] = Field(min_length=1)
    owners: tuple[VectorOwner, ...] = ()

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if len({item.record_id for item in self.records}) != len(self.records):
            raise ValueError("Duplicate vector record ID")
        if self.owners and sorted(o.record_id for o in self.owners) != sorted(
            r.record_id for r in self.records
        ):
            raise ValueError("Vector owners must cover each record once")
        return self
```

In `ChromaVectorIndex.upsert_batch` (`adapters/chroma/index.py:236-244`), build the metadata per record:

```python
        owners = {owner.record_id: owner for owner in batch.owners}

        def metadata(item: EmbeddingRecord) -> dict[str, str]:
            values = {"input_sha": item.input_sha, "value_sha": item.value_sha}
            owner = owners.get(item.record_id)
            if owner is not None:
                values.update(
                    revision_id=owner.revision_id,
                    extraction_id=owner.extraction_id,
                    owner_kind=owner.owner_kind,
                    owner_id=owner.owner_id,
                )
            return values
```

and pass `metadatas=[metadata(item) for item in chunk]`. `_present` reads only `input_sha` and `value_sha`, so verification is unchanged.

In the coordinator, `_text_batch` passes `owners=tuple(VectorOwner(record_id=stored_id, revision_id=bundle.revision.revision_id, extraction_id=bundle.extraction.extraction_id, owner_kind="unit", owner_id=window_unit_id) ...)` where `window_unit_id` is the window's `unit_id` (build a `{window.chunk_id: window.unit_id for window in windows}` map at the top of the method). `_image_batch` uses `owner_kind="asset"`, `owner_id=item.asset.asset_id`. In `vector_batch_records`, add owners from SQL so a native rebuild restores the metadata:

```python
    owners = tuple(
        VectorOwner(record_id=row[0], revision_id=row[1], extraction_id=row[2],
                    owner_kind="unit" if row[3] else "asset", owner_id=row[3] or row[4])
        for row in owner_rows
    )
```

where `owner_rows` is passed in by the caller (`verify_native_vector` reads them with `SELECT v.record_id,e.revision_id,b.extraction_id,c.unit_id,v.asset_id FROM vector_records v JOIN index_batches b USING(batch_id) JOIN extractions e ON e.extraction_id=b.extraction_id LEFT JOIN chunks c ON c.chunk_id=v.chunk_id WHERE v.batch_id=? ORDER BY v.record_id`); give `vector_batch_records` a keyword parameter `owner_rows: tuple[tuple, ...] = ()`.

- [ ] **Step 4: Run the tests**

Run: `uv run --locked pytest tests/storage/test_vector_contract.py tests/indexing tests/acceptance/test_combined_channels.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tychora/contracts/vectors.py src/tychora/adapters/chroma/index.py src/tychora/indexing/coordinator.py src/tychora/application/recovery.py tests/storage/test_vector_contract.py
git commit -m "feat: record revision, extraction and owner in vector index metadata"
```

---

### Task 7: Page tables and the batch and snapshot repositories

The page tables are added next to today's `snapshot_members` and `snapshot_batches` tables, so nothing switches yet. Task 9 turns the old tables into views.

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (new tables; `snapshots.job_id`, `snapshots.member_count`), `src/tychora/adapters/sqlite/migrations.py`
- Modify: `src/tychora/ports/repositories.py` (`BatchRow`, `VectorRecordRow`, `GcReport`, `PublicationFootprint`, `BatchRepository`, `SnapshotRepository`)
- Modify: `src/tychora/contracts/publication.py` (add `NewBatch` and `receipt_document_digest`; the plan stays v1 until Task 9)
- Create: `src/tychora/adapters/sqlite/repositories/batches.py`, `src/tychora/adapters/sqlite/repositories/snapshots.py`
- Modify: `src/tychora/adapters/sqlite/repositories/__init__.py`
- Test: `tests/storage/test_batch_repository.py` (new), `tests/storage/test_snapshot_repository.py` (new), `tests/storage/test_schema.py:24` (table count +5)

**Interfaces:**
- Consumes: `MembershipPage`, `PageBatch`, `PageMember`, `SlotFamily`, `compute_page_id`, `membership_sha` (Task 1), `SqliteFamilyRepository` (Task 4).
- Produces (ports; `ReadTransaction` gains `batches` and `snapshots`):

```python
class BatchRow(Contract):
    batch_id: Identifier; family_id: Sha256; slot: NonEmptyStr; extraction_id: Identifier | None
    state: Literal["pending", "building", "ready", "failed", "removed"]
    expected_count: NonNegativeInt; actual_count: NonNegativeInt | None
    receipt_digest: Sha256 | None; input_manifest: ArtifactRef; vectors: ArtifactRef | None
    def page_batch(self) -> PageBatch: ...
class VectorRecordRow(Contract):
    record_id: Identifier; chunk_id: Identifier | None; asset_id: Identifier | None
    input_sha: Sha256; row_index: NonNegativeInt
class GcReport(Contract):
    snapshots: tuple[Identifier, ...]; pages: tuple[Identifier, ...]
    orphan_batches: tuple[BatchRow, ...]; released_artifacts: tuple[Sha256, ...]
class PublicationFootprint(Contract):
    snapshot_id: Identifier; root_bytes: NonNegativeInt; new_page_bytes: NonNegativeInt
    new_pages: NonNegativeInt; largest_artifact_bytes: NonNegativeInt
class BatchRepository(Protocol):
    def begin(self, batch: NewBatch) -> None: ...
    def write_lexical(self, batch_id: str, records: tuple[LexicalRecord, ...]) -> None: ...
    def write_vectors(self, batch_id: str, rows: tuple[VectorRecordRow, ...]) -> None: ...
    def mark_ready(self, batch_id: str, actual_count: int) -> None: ...
    def count_ready(self, batch_ids: tuple[str, ...]) -> int: ...
    def get(self, batch_id: str) -> BatchRow | None: ...
    def find(self, family_id: str, extraction_id: str) -> BatchRow | None: ...
    def for_snapshot(self, snapshot_id: str, slot: str) -> tuple[BatchRow, ...]: ...
    # purge, receipt and record_ids are added by Task 10
class SnapshotRepository(Protocol):
    def active_id(self) -> str | None: ...
    def view(self, snapshot_id: str) -> SnapshotView: ...        # implemented in Task 9
    def page(self, snapshot_id: str, bucket: int) -> MembershipPage | None: ...
    def page_manifests(self, page_ids: tuple[str, ...]) -> Mapping[str, ArtifactRef]: ...
    def insert_page(self, page: MembershipPage, manifest: ArtifactRef) -> None: ...
    def insert_snapshot(self, plan: PublicationPlan) -> None: ...  # implemented in Task 9
    def set_active(self, snapshot_id: str) -> None: ...
    def retained_ids(self, keep_last: int = 16) -> frozenset[str]: ...
    def collect(self, limit: int = 64, *, leased: frozenset[str] = frozenset(), include_building: bool = False) -> GcReport: ...  # Task 10
    def footprint(self, snapshot_id: str) -> PublicationFootprint: ...  # Task 13
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_batch_repository.py
"""Batch rows move building -> ready; ready counts are one statement."""

from io import BytesIO

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest, family_id, new_family_nonce
from tychora.contracts.publication import LexicalBatchReceipt, LexicalRecord, NewBatch
from tychora.contracts.snapshots import IndexFamily


@pytest.fixture
def seeded(tmp_path):
    db = open_corpus(tmp_path / "c.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    bundle = seed_bundle("Record verification outcomes.")
    store.commit_extraction(bundle)
    nonce = new_family_nonce()
    family = IndexFamily(
        family_id=family_id(bundle.workspace.workspace_id, "c" * 64, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha="c" * 64,
        config_json=FrozenJsonMap({"effective_config_sha": "c" * 64}),
        backend_namespace="lexical",
        created_at=bundle.workspace.created_at,
    )
    uow = SqliteUnitOfWork(db)
    with uow.write() as tx:
        tx.families.create(family, nonce, "c" * 64)
    entries = (bundle.chunks[0].chunk_id,)
    manifest = reader().put_stream(
        BytesIO(canonical_bytes({"type": "index_input", "schema_version": "1", "data": {"entries": entries}})),
        "application/json",
    )
    batch = NewBatch(
        slot="lexical",
        batch_id=digest("index-batch-v1", family.family_id, bundle.extraction.extraction_id, manifest.sha256),
        family_id=family.family_id,
        extraction_id=bundle.extraction.extraction_id,
        input_manifest=manifest,
        expected_count=1,
        receipt=LexicalBatchReceipt(
            family_id=family.family_id, count=1, entry_ids_sha=digest("index-entries-v1", entries)
        ),
    )
    yield db, uow, batch, entries
    db.close()


def test_batch_moves_from_building_to_ready_and_bumps_epoch_once(seeded):
    db, uow, batch, entries = seeded
    before = db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0]
    with uow.write() as tx:
        tx.batches.begin(batch)
        assert tx.batches.get(batch.batch_id).state == "building"
        tx.batches.write_lexical(batch.batch_id, (LexicalRecord(chunk_id=entries[0], body="Record"),))
        tx.batches.mark_ready(batch.batch_id, 1)
    after = db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0]
    assert after == before + 1
    with uow.read() as tx:
        row = tx.batches.find(batch.family_id, batch.extraction_id)
        assert row is not None and row.state == "ready" and row.actual_count == 1
        assert tx.batches.count_ready((batch.batch_id, "missing")) == 1
        assert row.page_batch().batch_id == batch.batch_id


def test_begin_is_idempotent_for_the_same_batch(seeded):
    _, uow, batch, _ = seeded
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.begin(batch)


def test_mark_ready_requires_a_known_batch(seeded):
    _, uow, _, _ = seeded
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.mark_ready("unknown", 0)
    assert error.value.reason == "canonical.binding_mismatch"
```

```python
# tests/storage/test_snapshot_repository.py
"""Pages are content-addressed rows; inserting one twice is a no-op."""

from hashlib import sha256
from io import BytesIO

from tests.helpers.corpus import reader
from tests.storage.test_batch_repository import seeded  # noqa: F401 - shared fixture
from tychora.contracts.snapshots import MembershipPage, PageMember, bucket_of, compute_page_id


def test_insert_page_round_trips_and_is_idempotent(seeded):
    db, uow, batch, entries = seeded
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.mark_ready(batch.batch_id, 1)
    with uow.read() as tx:
        page_batch = tx.batches.get(batch.batch_id).page_batch()
    source_id, revision_id = db.execute(
        "SELECT source_id,revision_id FROM revisions"
    ).fetchone()
    member = PageMember(
        source_id=source_id,
        revision_id=revision_id,
        extraction_id=batch.extraction_id,
        batches=(page_batch,),
    )
    bucket = bucket_of(source_id)
    page = MembershipPage(bucket=bucket, members=(member,), page_id=compute_page_id(bucket, (member,)))
    manifest = reader().put_stream(BytesIO(page.manifest_bytes()), "application/json")
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        tx.snapshots.insert_page(page, manifest)
    with uow.read() as tx:
        assert tx.snapshots.page_manifests((page.page_id,)) == {page.page_id: manifest}
    assert db.execute("SELECT COUNT(*) FROM page_members").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM page_batches").fetchone()[0] == 1
    assert manifest.sha256 == sha256(page.manifest_bytes()).hexdigest()
```

In `tests/storage/test_schema.py:24`, change `== 39` to `== 44` (five new tables: `membership_pages`, `page_members`, `page_batches`, `snapshot_pages`, `snapshot_root_batches`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_batch_repository.py tests/storage/test_snapshot_repository.py tests/storage/test_schema.py::test_canonical_schema_integrity -v`
Expected: FAIL (`ImportError: cannot import name 'NewBatch'`).

- [ ] **Step 3: Add the tables**

Append after `snapshot_batches` in `0001.sql`:

```sql
CREATE TABLE membership_pages (
  page_id TEXT PRIMARY KEY,
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  member_count INTEGER NOT NULL CHECK(member_count>=0)
) STRICT;
CREATE TABLE page_members (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  source_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  is_default INTEGER NOT NULL CHECK(is_default IN (0,1)),
  PRIMARY KEY(page_id,revision_id),
  FOREIGN KEY(revision_id,source_id) REFERENCES revisions(revision_id,source_id),
  FOREIGN KEY(extraction_id,revision_id) REFERENCES extractions(extraction_id,revision_id)
) STRICT;
CREATE INDEX page_members_extraction ON page_members(extraction_id,page_id);
CREATE TABLE page_batches (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  slot TEXT NOT NULL,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  PRIMARY KEY(page_id,slot,batch_id)
) STRICT;
CREATE INDEX page_batches_batch ON page_batches(batch_id);
CREATE TABLE snapshot_pages (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  PRIMARY KEY(snapshot_id,bucket)
) STRICT;
CREATE INDEX snapshot_pages_page ON snapshot_pages(page_id);
CREATE TABLE snapshot_root_batches (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  slot TEXT NOT NULL,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  PRIMARY KEY(snapshot_id,slot,batch_id)
) STRICT;
```

and add to `snapshots` (after `published_at`):

```sql
  job_id TEXT,
  member_count INTEGER CHECK(member_count IS NULL OR member_count>=0)
```

(`NOT NULL` arrives in Task 9, when every publication sets them). Recompute `SCHEMA_CHECKSUM`.

- [ ] **Step 4: Add `NewBatch` and the ports**

In `src/tychora/contracts/publication.py` add (imports: `sha256` from `hashlib`, `PageBatch` from `.snapshots`):

```python
def receipt_document_digest(
    receipt: "LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt",
) -> str:
    """SHA-256 of a batch receipt's canonical JSON, as pages bind it."""
    return sha256(canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()


class NewBatch(Contract):
    slot: NonEmptyStr
    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt
    vectors: ArtifactRef | None = None

    @model_validator(mode="after")
    def bound_identity(self) -> Self:
        expected = digest(
            "index-batch-v1", self.family_id, self.extraction_id, self.input_manifest.sha256
        )
        if self.batch_id != expected:
            raise ValueError("Index batch identity differs from exact input")
        return self

    @property
    def count(self) -> int:
        receipt = self.receipt
        return receipt.evidence_count if isinstance(receipt, GraphBatchReceipt) else receipt.count

    def page_batch(self) -> PageBatch:
        return PageBatch(
            slot=self.slot,
            batch_id=self.batch_id,
            family_id=self.family_id,
            count=self.count,
            receipt_digest=receipt_document_digest(self.receipt),
            input_manifest=self.input_manifest,
            vectors=self.vectors,
        )
```

Add the port contracts and protocols of the Interfaces block to `src/tychora/ports/repositories.py`, with `BatchRow.page_batch()`:

```python
    def page_batch(self) -> PageBatch:
        if self.state != "ready" or self.receipt_digest is None or self.actual_count is None:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Batch is not ready for publication", reason="batch.missing"
            )
        return PageBatch(
            slot=self.slot,
            batch_id=self.batch_id,
            family_id=self.family_id,
            count=self.actual_count,
            receipt_digest=self.receipt_digest,
            input_manifest=self.input_manifest,
            vectors=self.vectors,
        )
```

`SnapshotView` and `PublicationPlan` are referenced by name only in `SnapshotRepository`'s signatures (`from tychora.contracts.publication import PublicationPlan, SnapshotView`); their v2 shapes land in Task 9.

- [ ] **Step 5: Implement the SQLite batch repository**

```python
# src/tychora/adapters/sqlite/repositories/batches.py
"""Index batch rows, their lexical and vector rows, and their state."""

import json
import sqlite3

from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    LexicalRecord,
    NewBatch,
    receipt_document_digest,
)
from tychora.contracts.vectors import BatchReceipt
from tychora.ports.repositories import BatchRow, VectorRecordRow

from . import artifact_ref
from .artifacts import SqliteArtifactRepository

_SELECT = (
    "SELECT b.batch_id,b.family_id,f.channel,b.extraction_id,b.state,b.expected_count,"
    "b.actual_count,b.receipt_json,i.artifact_sha,i.size_bytes,i.media_type,i.storage_key,"
    "v.artifact_sha,v.size_bytes,v.media_type,v.storage_key "
    "FROM index_batches b JOIN index_families f USING(family_id) "
    "JOIN artifacts i ON i.artifact_sha=b.input_manifest_sha "
    "LEFT JOIN artifacts v ON v.artifact_sha=b.vectors_sha "
)


def _receipt_digest(receipt_json: str | None) -> str | None:
    if receipt_json is None:
        return None
    from hashlib import sha256

    return sha256(canonical_bytes(json.loads(receipt_json))).hexdigest()


def _row(row: tuple) -> BatchRow:
    return BatchRow(
        batch_id=row[0],
        family_id=row[1],
        slot=row[2],
        extraction_id=row[3],
        state=row[4],
        expected_count=row[5],
        actual_count=row[6],
        receipt_digest=_receipt_digest(row[7]),
        input_manifest=artifact_ref(row[8:12]),
        vectors=None if row[12] is None else artifact_ref(row[12:16]),
    )


def _entry_ids_sha(batch: NewBatch) -> str | None:
    receipt = batch.receipt
    if isinstance(receipt, LexicalBatchReceipt):
        return receipt.entry_ids_sha
    if isinstance(receipt, BatchReceipt):
        return digest("index-entries-v1", tuple(r.record_id for r in receipt.records))
    return None


class SqliteBatchRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.artifacts = SqliteArtifactRepository(db)

    def begin(self, batch: NewBatch) -> None:
        self.artifacts.register(batch.input_manifest)
        if batch.vectors is not None:
            self.artifacts.register(batch.vectors)
        receipt_json = canonical_bytes(batch.receipt.model_dump(mode="json")).decode()
        values = (
            batch.batch_id,
            batch.family_id,
            batch.extraction_id,
            batch.input_manifest.sha256,
            batch.expected_count,
            _entry_ids_sha(batch),
            receipt_json,
            batch.vectors.sha256 if batch.vectors else None,
        )
        self.db.execute(
            "INSERT INTO index_batches (batch_id,family_id,extraction_id,input_manifest_sha,"
            "expected_count,entry_ids_sha,receipt_json,vectors_sha,state) "
            "VALUES (?,?,?,?,?,?,?,?,'building') ON CONFLICT(batch_id) DO NOTHING",
            values,
        )
        stored = self.db.execute(
            "SELECT batch_id,family_id,extraction_id,input_manifest_sha,expected_count,"
            "entry_ids_sha,receipt_json,vectors_sha FROM index_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
        if stored != values:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Index batch differs from its immutable row",
                reason="canonical.binding_mismatch",
            )

    def write_lexical(self, batch_id: str, records: tuple[LexicalRecord, ...]) -> None:
        self.db.executemany(
            "INSERT INTO lexical_records (batch_id,chunk_id,identifiers,title,headings,body,context) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(batch_id,chunk_id) DO NOTHING",
            [
                (batch_id, r.chunk_id, r.identifiers, r.title, r.headings, r.body, r.context)
                for r in records
            ],
        )

    def write_vectors(self, batch_id: str, rows: tuple[VectorRecordRow, ...]) -> None:
        self.db.executemany(
            "INSERT INTO vector_records (record_id,batch_id,chunk_id,asset_id,input_sha,row_index) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(record_id) DO NOTHING",
            [
                (r.record_id, batch_id, r.chunk_id, r.asset_id, r.input_sha, r.row_index)
                for r in rows
            ],
        )

    def mark_ready(self, batch_id: str, actual_count: int) -> None:
        updated = self.db.execute(
            "UPDATE index_batches SET state='ready',actual_count=? "
            "WHERE batch_id=? AND state IN ('building','ready')",
            (actual_count, batch_id),
        ).rowcount
        if updated != 1:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Only a building batch can become ready",
                reason="canonical.binding_mismatch",
            )

    def count_ready(self, batch_ids: tuple[str, ...]) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM index_batches WHERE state='ready' "
            "AND batch_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(set(batch_ids))),),
        ).fetchone()[0]

    def get(self, batch_id: str) -> BatchRow | None:
        row = self.db.execute(_SELECT + "WHERE b.batch_id=?", (batch_id,)).fetchone()
        return None if row is None else _row(row)

    def find(self, family_id: str, extraction_id: str) -> BatchRow | None:
        row = self.db.execute(
            _SELECT + "WHERE b.family_id=? AND b.extraction_id=?", (family_id, extraction_id)
        ).fetchone()
        return None if row is None else _row(row)

    def for_snapshot(self, snapshot_id: str, slot: str) -> tuple[BatchRow, ...]:
        rows = self.db.execute(
            _SELECT + "JOIN snapshot_batches s ON s.batch_id=b.batch_id "
            "WHERE s.snapshot_id=? AND s.slot=? ORDER BY b.batch_id",
            (snapshot_id, slot),
        ).fetchall()
        return tuple(_row(row) for row in rows)
```

`purge`, `receipt` and `record_ids` arrive with garbage collection (Task 10), which owns their tests. `receipt_document_digest` and `GraphBatchReceipt` are imported for Task 9's use of the same module; drop them if ruff reports them unused at this step.

- [ ] **Step 6: Implement the page half of the snapshot repository**

```python
# src/tychora/adapters/sqlite/repositories/snapshots.py
"""Snapshots, membership pages, retention and collection."""

import sqlite3
from collections.abc import Mapping

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import MembershipPage, PageMember

from . import artifact_ref
from .batches import _SELECT, _row


def _integrity(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


class SqliteSnapshotRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def active_id(self) -> str | None:
        rows = self.db.execute("SELECT active_snapshot_id FROM workspaces").fetchall()
        if len(rows) != 1:
            raise _integrity("Canonical workspace identity is ambiguous")
        return rows[0][0]

    def set_active(self, snapshot_id: str) -> None:
        if not self.db.in_transaction:
            raise _integrity("Active snapshot switch requires a write transaction")
        if self.db.execute("UPDATE workspaces SET active_snapshot_id=?", (snapshot_id,)).rowcount != 1:
            raise _integrity("Canonical workspace identity is ambiguous")

    def insert_page(self, page: MembershipPage, manifest: ArtifactRef) -> None:
        self.db.execute(
            "INSERT INTO membership_pages (page_id,bucket,manifest_sha,member_count) "
            "VALUES (?,?,?,?) ON CONFLICT(page_id) DO NOTHING",
            (page.page_id, page.bucket, manifest.sha256, len(page.members)),
        )
        stored = self.db.execute(
            "SELECT bucket,manifest_sha,member_count FROM membership_pages WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
        if stored != (page.bucket, manifest.sha256, len(page.members)):
            raise _integrity("Membership page differs from its immutable row")
        self.db.executemany(
            "INSERT INTO page_members (page_id,source_id,revision_id,extraction_id,is_default) "
            "VALUES (?,?,?,?,?) ON CONFLICT(page_id,revision_id) DO NOTHING",
            [
                (page.page_id, m.source_id, m.revision_id, m.extraction_id, int(m.is_default))
                for m in page.members
            ],
        )
        self.db.executemany(
            "INSERT INTO page_batches (page_id,slot,batch_id) VALUES (?,?,?) "
            "ON CONFLICT(page_id,slot,batch_id) DO NOTHING",
            [(page.page_id, b.slot, b.batch_id) for m in page.members for b in m.batches],
        )

    def page_manifests(self, page_ids: tuple[str, ...]) -> Mapping[str, ArtifactRef]:
        import json

        rows = self.db.execute(
            "SELECT p.page_id,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
            "FROM membership_pages p JOIN artifacts a ON a.artifact_sha=p.manifest_sha "
            "WHERE p.page_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(set(page_ids))),),
        ).fetchall()
        found = {row[0]: artifact_ref(row[1:5]) for row in rows}
        if set(found) != set(page_ids):
            raise _integrity("Membership page manifest is not registered")
        return found

    def page(self, snapshot_id: str, bucket: int) -> MembershipPage | None:
        row = self.db.execute(
            "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? AND bucket=?",
            (snapshot_id, bucket),
        ).fetchone()
        return None if row is None else self._load_page(row[0], bucket)

    def _load_page(self, page_id: str, bucket: int) -> MembershipPage:
        members = self.db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM page_members "
            "WHERE page_id=? ORDER BY source_id,revision_id",
            (page_id,),
        ).fetchall()
        batches: dict[str, list] = {}
        for row in self.db.execute(
            _SELECT + "JOIN page_batches p ON p.batch_id=b.batch_id WHERE p.page_id=? "
            "ORDER BY f.channel",
            (page_id,),
        ):
            batch = _row(row)
            batches.setdefault(batch.extraction_id or "", []).append(batch.page_batch())
        try:
            return MembershipPage(
                bucket=bucket,
                page_id=page_id,
                members=tuple(
                    PageMember(
                        source_id=m[0],
                        revision_id=m[1],
                        extraction_id=m[2],
                        is_default=bool(m[3]),
                        batches=tuple(batches.get(m[2], ())),
                    )
                    for m in members
                ),
            )
        except ValueError:
            raise _integrity("Membership page rows differ from its identity") from None
```

The remaining `SnapshotRepository` methods are added by the tasks that need them (`view` and `insert_snapshot` in Task 9, `retained_ids` and `collect` in Task 10, `footprint` in Task 13).

In `adapters/sqlite/repositories/__init__.py`, `SqliteTransaction.__init__` gains:

```python
        self.batches = SqliteBatchRepository(db)
        self.snapshots = SqliteSnapshotRepository(db)
```

- [ ] **Step 7: Run the tests**

Run: `uv run --locked pytest tests/storage -v`
Expected: PASS. The phase-1 port-neutrality test (`tests/architecture/`) also runs: `uv run --locked pytest tests/architecture -v` → PASS (no `sqlite3` type crosses the new protocols).

- [ ] **Step 8: Commit**

```bash
git add src/tychora/resources/migrations/0001.sql src/tychora/adapters/sqlite/migrations.py src/tychora/ports/repositories.py src/tychora/contracts/publication.py src/tychora/adapters/sqlite/repositories tests/storage
git commit -m "feat: add membership page tables and batch and snapshot repositories"
```

---

### Task 8: Pure page planning

**Files:**
- Create: `src/tychora/indexing/planning.py`
- Test: `tests/indexing/test_planning.py` (new)

**Interfaces:**
- Consumes: `MembershipPage`, `PageMember`, `PageBatch`, `bucket_of`, `compute_page_id` (Task 1).
- Produces:

```python
def next_page(
    base: MembershipPage | None,
    incoming: PageMember,
    retained_editions: Mapping[str, str | None],   # revision_id -> edition_key, for base members of incoming.source_id
    incoming_edition: str | None,
) -> MembershipPage
def member_count_after(base_count: int, base: MembershipPage | None, page: MembershipPage) -> int
```

`next_page` keeps today's membership rule (`indexing/coordinator.py:917-933`) restricted to one bucket: other sources' members are kept unchanged; an earlier revision of the incoming source is kept as non-default when its edition differs and dropped otherwise; the incoming member is default.

- [ ] **Step 1: Write the failing tests**

```python
# tests/indexing/test_planning.py
"""The next page of a bucket changes only what the incoming member replaces."""

from hashlib import sha256

from tychora.contracts.artifacts import ArtifactRef
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
                input_manifest=ArtifactRef(sha256=sha, size_bytes=1, media_type="application/json", storage_key=sha),
            ),
        ),
    )


def _page(*members: PageMember) -> MembershipPage:
    bucket = bucket_of(members[0].source_id)
    ordered = tuple(sorted(members, key=lambda m: (m.source_id, m.revision_id)))
    return MembershipPage(bucket=bucket, members=ordered, page_id=compute_page_id(bucket, ordered))


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
    assert {(m.revision_id, m.is_default) for m in kept.members} == {("r1", False), ("r2", True)}


def test_member_count_after_replacement():
    (source,) = _sources(4, 1)
    base = _page(_member(source, "r1"))
    page = next_page(base, _member(source, "r2"), {"r1": "1"}, "1")
    assert member_count_after(10, base, page) == 10
    assert member_count_after(10, None, page) == 11
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_planning.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.indexing.planning'`.

- [ ] **Step 3: Implement**

```python
# src/tychora/indexing/planning.py
"""Compute the next membership page of one bucket for one incoming member."""

from collections.abc import Mapping

from tychora.contracts.snapshots import MembershipPage, PageMember, bucket_of, compute_page_id


def _kept(
    member: PageMember,
    incoming: PageMember,
    retained_editions: Mapping[str, str | None],
    incoming_edition: str | None,
) -> PageMember | None:
    if member.source_id != incoming.source_id:
        return member
    if member.revision_id == incoming.revision_id:
        return None
    if retained_editions.get(member.revision_id) != incoming_edition:
        return member.model_copy(update={"is_default": False})
    return None


def next_page(
    base: MembershipPage | None,
    incoming: PageMember,
    retained_editions: Mapping[str, str | None],
    incoming_edition: str | None,
) -> MembershipPage:
    """Keep the bucket's other members; supersede or keep earlier editions."""
    bucket = bucket_of(incoming.source_id)
    members = [
        kept
        for member in (base.members if base is not None else ())
        if (kept := _kept(member, incoming, retained_editions, incoming_edition)) is not None
    ]
    members.append(incoming.model_copy(update={"is_default": True}))
    ordered = tuple(sorted(members, key=lambda m: (m.source_id, m.revision_id)))
    return MembershipPage(bucket=bucket, members=ordered, page_id=compute_page_id(bucket, ordered))


def member_count_after(base_count: int, base: MembershipPage | None, page: MembershipPage) -> int:
    return base_count - (len(base.members) if base is not None else 0) + len(page.members)
```

- [ ] **Step 4: Run the tests**

Run: `uv run --locked pytest tests/indexing/test_planning.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tychora/indexing/planning.py tests/indexing/test_planning.py
git commit -m "feat: plan the next membership page of one bucket"
```

---
### Task 9: Switch publication to membership pages (D1, D2, Q2)

This is the one task that cannot be split without breaking `main`: the old tables become views, the plan contract changes shape, and every producer and consumer of the plan moves at once. Work through the steps in order and run the full suite only at Step 14.

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (old `snapshot_members` and `snapshot_batches` tables become views; `snapshots.job_id`, `member_count` become `NOT NULL`), `src/tychora/adapters/sqlite/migrations.py`
- Rewrite: `src/tychora/contracts/publication.py`
- Modify: `src/tychora/adapters/sqlite/repositories/snapshots.py` (`view`, `insert_snapshot`)
- Create: `src/tychora/adapters/sqlite/retained_slots.py`
- Rewrite: `src/tychora/adapters/sqlite/publication.py` (all of it; `check_publication_target` is kept verbatim)
- Modify: `src/tychora/adapters/sqlite/corpus.py:88-108` (`insert_snapshot_bindings`, `publish_snapshot` delegators), `:187-226` (`resolve_snapshot`, `pin_snapshot`, new `snapshot_slots`)
- Modify: `src/tychora/indexing/coordinator.py:101-116` (`PreparedProjection`), `:141-193` (`_retained_vector_spec` takes `SlotFamily`), `:340-395` (lexical), `:462-552` and `:680-767` (vector batches return `NewBatch`), `:769-883` (graph), `:896-1347` (`prepare`, `finalize`, `_plan`, `reuse_existing`, `attach`, `build`)
- Modify: `src/tychora/adapters/sqlite/eligibility.py:216-240,489-560`, `src/tychora/retrieval/eligibility.py:183-193,600-660`
- Modify: `src/tychora/application/query.py:375`, `src/tychora/application/default_bindings.py:243-300`, `src/tychora/application/recovery.py:100-140,560-660,810-900`, `src/tychora/application/sources.py:300-350,780-795`, `src/tychora/application/inspection.py:686-695`, `src/tychora/application/removal.py:351-366,439-490`, `src/tychora/adapters/files/references.py:279-302`
- Create: `tests/helpers/publication.py`
- Modify tests: `tests/indexing/test_publication.py`, `tests/storage/test_schema.py`, `tests/api/test_read_grants.py:138`, `tests/ingestion/test_tables.py:893`, `tests/retrieval/test_eligibility.py`, `tests/operations/test_restore.py`, and every `snapshot.slots` / `view.slots` user listed by `rg -n "\.slots\b" tests` (`tests/acceptance/combined_store_probe.py`, `tests/acceptance/test_combined_channels.py`, `tests/acceptance/test_crash_matrix.py`, `tests/api/test_configured_runtime.py`, `tests/evaluation/test_code_scenarios.py`, `tests/evaluation/test_graph_scenarios.py`, `tests/evaluation/test_image_scenario.py`, `tests/evaluation/test_temporal_scenarios.py`, `tests/indexing/test_sources.py`)
- Test (new): `tests/indexing/test_page_publication.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5, 7, 8.
- Produces (exactly the index's phase-2 block, plus the additions listed in the self-review):

```python
class PublicationPlan(Contract):
    job_id: Identifier; snapshot_id: Identifier; workspace_id: Identifier
    base_snapshot_id: Identifier | None; config_sha: Sha256
    families: tuple[SlotFamily, ...]; member_count: NonNegativeInt
    new_pages: tuple[MembershipPage, ...]; reused_page_ids: tuple[Identifier, ...]
    new_batches: tuple[NewBatch, ...]; root_batches: tuple[PageBatch, ...] = ()
    graph_watermark: NonNegativeInt | None = None
    manifest: ArtifactRef
    page_ids: tuple[str, ...]          # property: sorted new + reused
class SnapshotView(Contract):          # interim; phase 4 removes `members`
    snapshot_id; workspace_id; parent_snapshot_id; config_sha; membership_sha
    member_count; page_ids; families: tuple[SlotFamily, ...]; members: tuple[SnapshotMember, ...]
    lexical_statistics_epoch: NonNegativeInt = 0
class RetainedBatch(Contract): batch_id; family_id; extraction_id; input_manifest; expected_count; receipt
    vectors; graph_manifest; lexical_records; vector_records          # renamed PublicationBatch
class RetainedSlot(Contract): slot; family; required; batches: tuple[RetainedBatch, ...]  # renamed PublicationSlot
def snapshot_root_document(plan: PublicationPlan, page_manifests: Mapping[str, ArtifactRef]) -> dict
def publication_plan_document(plan: PublicationPlan) -> dict
# adapters/sqlite/retained_slots.py
def load_retained_slots(db: sqlite3.Connection, artifacts: Artifacts, view: SnapshotView) -> tuple[RetainedSlot, ...]
# CorpusStore
def snapshot_slots(self, view: SnapshotView) -> tuple[RetainedSlot, ...]
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/indexing/test_page_publication.py
"""Publication rewrites one page, verifies only new batches, and binds through SQL."""

from unittest.mock import patch

import pytest

from tests.indexing.test_publication import projected_corpus  # noqa: F401 - fixture
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import bucket_of


def _pages(store, snapshot_id):
    return dict(
        store.db.execute(
            "SELECT bucket,page_id FROM snapshot_pages WHERE snapshot_id=?", (snapshot_id,)
        ).fetchall()
    )


def test_add_rewrites_only_its_bucket(projected_corpus):
    f = projected_corpus
    first = f.add_revision("first source text", source_key="a.md")
    second = f.add_revision("second source text", source_key="b.md")
    before, after = _pages(f.store, first), _pages(f.store, second)
    changed = {bucket for bucket in after if before.get(bucket) != after[bucket]}
    source = f.store.db.execute(
        "SELECT source_id FROM sources WHERE source_key='b.md'"
    ).fetchone()[0]
    assert changed == {bucket_of(source)}


def test_views_equal_the_plan(projected_corpus):
    f = projected_corpus
    _, _, plan = f.prepare_revision("view equivalence text", source_key="v.md")
    snapshot_id = f.store.publish_snapshot(plan)
    members = set(
        f.store.db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    expected_members = {
        (m.source_id, m.revision_id, m.extraction_id, int(m.is_default))
        for page_id in plan.page_ids
        for m in f.store.snapshots_repository().page_by_id(page_id).members
    }
    assert members == expected_members
    batches = {
        row[0]
        for row in f.store.db.execute(
            "SELECT batch_id FROM snapshot_batches WHERE snapshot_id=?", (snapshot_id,)
        )
    }
    assert {b.batch_id for b in plan.new_batches} <= batches
    assert {b.batch_id for b in plan.root_batches} <= batches


def test_reused_batches_are_never_reopened(projected_corpus):
    f = projected_corpus
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

    with patch.object(f.store.artifacts, "open_verified", spy), patch.object(
        f.vector, "verify_batch", verify_spy
    ):
        f.store.publish_snapshot(plan)
    new_vectors = {b.vectors.sha256 for b in plan.new_batches if b.vectors is not None}
    new_inputs = {b.input_manifest.sha256 for b in plan.new_batches}
    assert set(opened) <= new_vectors | new_inputs | {plan.manifest.sha256}
    assert len(verified) == sum(1 for b in plan.new_batches if b.vectors is not None)
    assert new_ids


def test_new_edition_rewrites_only_its_bucket(projected_corpus):
    f = projected_corpus
    f.add_revision("neighbour text", source_key="n.md")
    first = f.add_revision("edition one text", source_key="e.md")
    second = f.add_edition("edition two text", source_key="e.md", edition="2")
    before, after = _pages(f.store, first), _pages(f.store, second)
    assert sum(1 for b in after if before.get(b) != after[b]) == 1
    rows = f.store.db.execute(
        "SELECT r.edition_key,m.is_default FROM snapshot_members m JOIN revisions r USING(revision_id) "
        "JOIN sources s ON s.source_id=m.source_id WHERE m.snapshot_id=? AND s.source_key='e.md'",
        (second,),
    ).fetchall()
    assert sorted(rows) == [("1", 0), ("2", 1)]


def test_publication_refuses_reused_batch_that_is_not_ready(projected_corpus):
    f = projected_corpus
    f.add_revision("first", source_key="same.md")
    _, _, plan = f.prepare_revision("first", source_key="same.md")
    page_batches = {
        b.batch_id for page in plan.new_pages for m in page.members for b in m.batches
    } - {b.batch_id for b in plan.new_batches}
    assert page_batches
    f.store.db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?", (sorted(page_batches)[0],)
    )
    active = f.store.active_snapshot_id()
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(plan)
    assert (error.value.code, error.value.reason) == ("INDEX_INCOMPLETE", "batch.missing")
    assert f.store.active_snapshot_id() == active


def test_resolve_snapshot_never_reads_the_root_manifest(projected_corpus):
    f = projected_corpus
    snapshot_id = f.add_revision("resolve from SQL", source_key="r.md")
    root = f.store.db.execute(
        "SELECT manifest_sha FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()[0]
    opened = []
    original = f.store.artifacts.open_verified

    def spy(ref):
        opened.append(ref.sha256)
        return original(ref)

    with patch.object(f.store.artifacts, "open_verified", spy):
        view = f.store.resolve_snapshot(snapshot_id)
        with f.store.pin_snapshot(snapshot_id):
            pass
    assert root not in opened and opened == []
    assert view.member_count == len(view.members)
```

Add to `ProjectedCorpus` in `tests/indexing/test_publication.py` an `add_edition(text, *, source_key, edition)` helper that calls `seed_bundle` and then rebuilds the revision with `edition_key=edition` (copy `prepare_revision` and pass the edited bundle; the revision ID must be recomputed with `identity.revision_id(sid, original_sha, edition, metadata_sha)` and the extraction and unit IDs from it, as `seed_bundle` does). Add `CorpusStore.snapshots_repository()` returning `SqliteSnapshotRepository(self.db)` and `SqliteSnapshotRepository.page_by_id(page_id) -> MembershipPage` (the bucket is read from `membership_pages`), used by the view-equivalence test.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_page_publication.py -v`
Expected: FAIL (`AttributeError: 'PublicationPlan' object has no attribute 'page_ids'`).

- [ ] **Step 3: Turn the old tables into views**

In `0001.sql` delete the `snapshot_members` table, its `snapshot_default_source` index, the `snapshot_batches` table, and the `members_extraction` index (line 478). Make `snapshots.job_id` and `snapshots.member_count` `NOT NULL`. Append at the very end of the file (views go after every table they read):

```sql
CREATE VIEW snapshot_members AS
  SELECT sp.snapshot_id, m.source_id, m.revision_id, m.extraction_id, m.is_default
  FROM snapshot_pages sp JOIN page_members m ON m.page_id=sp.page_id;
CREATE VIEW snapshot_batches AS
  SELECT sp.snapshot_id, pb.slot, ib.family_id, pb.batch_id
  FROM snapshot_pages sp
  JOIN page_batches pb ON pb.page_id=sp.page_id
  JOIN index_batches ib ON ib.batch_id=pb.batch_id
  UNION ALL
  SELECT rb.snapshot_id, rb.slot, ib.family_id, rb.batch_id
  FROM snapshot_root_batches rb JOIN index_batches ib ON ib.batch_id=rb.batch_id;
```

The views keep the column names every existing reader uses (`adapters/sqlite/lexical.py:75,167`, `application/collections.py:323,352`, `application/default_bindings.py:199,268`, `application/sources.py:966,995`, `application/recovery.py:459,554`), so those queries need no change. Recompute `SCHEMA_CHECKSUM`.

Update `tests/storage/test_schema.py`: the table count becomes `42` (two tables became views); the fixture rows that inserted into `snapshot_members` and `snapshot_batches` (lines 125-155) insert a `membership_pages` row (`page_id="page"`, `bucket=0`, `manifest_sha=sha`, `member_count=1`), a `page_members` row, a `page_batches` row and a `snapshot_pages` row instead; the `snapshot_wrong_family` case becomes "a `page_batches` row naming an unknown batch fails the foreign key", and `snapshot_default_source` moves to `tests/contracts/test_snapshots.py::test_page_rejects_two_defaults_for_one_source` (Task 1), so delete it from the parametrization. `tests/api/test_read_grants.py:138` deletes from `page_members` (`DELETE FROM page_members WHERE extraction_id=?`); `tests/ingestion/test_tables.py:893` updates `page_members` (`UPDATE page_members SET extraction_id=? ...`) with the same `WHERE`.

- [ ] **Step 4: Rewrite `contracts/publication.py`**

Replace the file with:

```python
"""Publication plans, retained batch contents and snapshot views."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Self

from pydantic import field_serializer, model_validator

from .artifacts import ArtifactRef, ManifestRef
from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt, Sha256
from .graph import GraphBatchReceipt, GraphManifest
from .identity import JsonValue, canonical_bytes, digest
from .snapshots import IndexFamily as IndexFamily
from .snapshots import (
    PAGE_BUCKETS,
    MembershipPage,
    PageBatch,
    SlotFamily,
    membership_sha,
    snapshot_id_v2,
)
from .vectors import BatchReceipt


class SnapshotMember(Contract):
    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    is_default: bool = True


class LexicalRecord(Contract):
    chunk_id: Identifier
    identifiers: str = ""
    title: str = ""
    headings: str = ""
    body: str = ""
    context: str = ""


class VectorRecordBinding(Contract):
    record_id: Identifier
    input_sha: Sha256
    value_sha: Sha256
    chunk_id: Identifier | None = None
    asset_id: Identifier | None = None
    row_index: NonNegativeInt
    group_id: Identifier | None = None
    dependency_ids: frozenset[Identifier] = frozenset()

    @field_serializer("dependency_ids")
    def sorted_dependencies(self, dependencies: frozenset[str]) -> list[str]:
        return sorted(dependencies)

    @model_validator(mode="after")
    def one_owner(self) -> Self:
        if (self.chunk_id is None) == (self.asset_id is None):
            raise ValueError("Vector record requires exactly one canonical owner")
        if (self.group_id is None) != (not self.dependency_ids):
            raise ValueError("Text group identity and dependencies must be paired")
        return self


class LexicalBatchReceipt(Contract):
    family_id: Sha256
    count: NonNegativeInt
    entry_ids_sha: Sha256


type BatchReceiptVariant = LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt


def receipt_document_digest(receipt: BatchReceiptVariant) -> str:
    """SHA-256 of a batch receipt's canonical JSON, as pages bind it."""
    return sha256(canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()


class NewBatch(Contract):
    slot: NonEmptyStr
    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: BatchReceiptVariant
    vectors: ArtifactRef | None = None

    @model_validator(mode="after")
    def bound_identity(self) -> Self:
        expected = digest(
            "index-batch-v1", self.family_id, self.extraction_id, self.input_manifest.sha256
        )
        if self.batch_id != expected:
            raise ValueError("Index batch identity differs from exact input")
        return self

    @property
    def count(self) -> int:
        receipt = self.receipt
        return receipt.evidence_count if isinstance(receipt, GraphBatchReceipt) else receipt.count

    def page_batch(self) -> PageBatch:
        return PageBatch(
            slot=self.slot,
            batch_id=self.batch_id,
            family_id=self.family_id,
            count=self.count,
            receipt_digest=receipt_document_digest(self.receipt),
            input_manifest=self.input_manifest,
            vectors=self.vectors,
        )


class RetainedBatch(Contract):
    """One published batch with the rows read paths need, rebuilt from SQL."""

    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None = None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: BatchReceiptVariant
    vectors: ArtifactRef | None = None
    graph_manifest: GraphManifest | None = None
    lexical_records: tuple[LexicalRecord, ...] = ()
    vector_records: tuple[VectorRecordBinding, ...] = ()

    @model_validator(mode="after")
    def vectors_match_records(self) -> Self:
        if bool(self.vector_records) != (self.vectors is not None):
            raise ValueError("Vector records and their batch artifact must be paired")
        return self


class RetainedSlot(Contract):
    slot: NonEmptyStr
    family: IndexFamily
    required: bool = True
    batches: tuple[RetainedBatch, ...]

    @model_validator(mode="after")
    def bound_batches(self) -> Self:
        if any(batch.family_id != self.family.family_id for batch in self.batches):
            raise ValueError("Snapshot slot batch family differs")
        if len({batch.batch_id for batch in self.batches}) != len(self.batches):
            raise ValueError("Snapshot slot duplicates a batch")
        return self


class PublicationPlan(Contract):
    job_id: Identifier
    snapshot_id: Identifier
    workspace_id: Identifier
    base_snapshot_id: Identifier | None = None
    config_sha: Sha256
    families: tuple[SlotFamily, ...]
    member_count: NonNegativeInt
    new_pages: tuple[MembershipPage, ...]
    reused_page_ids: tuple[Identifier, ...]
    new_batches: tuple[NewBatch, ...]
    root_batches: tuple[PageBatch, ...] = ()
    graph_watermark: NonNegativeInt | None = None
    manifest: ArtifactRef

    @property
    def page_ids(self) -> tuple[str, ...]:
        return tuple(sorted({*self.reused_page_ids, *(p.page_id for p in self.new_pages)}))

    @model_validator(mode="after")
    def exact_bindings(self) -> Self:
        slots = [item.slot for item in self.families]
        if len(slots) != len(set(slots)):
            raise ValueError("Snapshot slot membership differs")
        buckets = [page.bucket for page in self.new_pages]
        if len(buckets) != len(set(buckets)) or len(self.page_ids) > PAGE_BUCKETS:
            raise ValueError("Snapshot pages overlap")
        if {p.page_id for p in self.new_pages} & set(self.reused_page_ids):
            raise ValueError("A new page is also listed as reused")
        bound = {
            batch.batch_id
            for page in self.new_pages
            for member in page.members
            for batch in member.batches
        } | {batch.batch_id for batch in self.root_batches}
        if not {batch.batch_id for batch in self.new_batches} <= bound:
            raise ValueError("A new batch is not bound by any page")
        expected = snapshot_id_v2(
            self.workspace_id,
            self.base_snapshot_id,
            self.job_id,
            self.config_sha,
            self.families,
            self.page_ids,
            root_batch_ids=[batch.batch_id for batch in self.root_batches],
        )
        if self.snapshot_id != expected:
            raise ValueError("Snapshot identity differs from complete bindings")
        return self


def snapshot_root_document(
    plan: PublicationPlan, page_manifests: Mapping[str, ArtifactRef]
) -> dict[str, JsonValue]:
    """The snapshot's retention document: page references, no member content."""
    links = [
        ManifestRef(artifact=page_manifests[page_id], type="membership_page", schema_version="2")
        for page_id in plan.page_ids
    ] + [
        ManifestRef(artifact=batch.input_manifest, type="index_input", schema_version="1")
        for batch in plan.root_batches
    ]
    return {
        "type": "snapshot",
        "schema_version": "2",
        "required_manifests": [
            link.model_dump(mode="json")
            for link in sorted(links, key=lambda link: link.artifact.sha256)
        ],
        "data": {
            "workspace_id": plan.workspace_id,
            "base_snapshot_id": plan.base_snapshot_id,
            "job_id": plan.job_id,
            "config_sha": plan.config_sha,
            "families": [item.model_dump(mode="json") for item in plan.families],
            "membership_sha": membership_sha(plan.page_ids),
            "member_count": plan.member_count,
            "page_ids": list(plan.page_ids),
            "root_batches": [batch.model_dump(mode="json") for batch in plan.root_batches],
        },
    }


def publication_plan_document(plan: PublicationPlan) -> dict[str, JsonValue]:
    """The job checkpoint that lets a resumed job publish the same plan."""
    return {
        "type": "publication_plan",
        "schema_version": "2",
        "required_manifests": [
            ManifestRef(artifact=plan.manifest, type="snapshot", schema_version="2").model_dump(
                mode="json"
            )
        ],
        "data": plan.model_dump(mode="json"),
    }


class SnapshotView(Contract):
    snapshot_id: Identifier
    workspace_id: Identifier
    parent_snapshot_id: Identifier | None = None
    config_sha: Sha256
    membership_sha: Sha256
    member_count: NonNegativeInt
    page_ids: tuple[Identifier, ...]
    families: tuple[SlotFamily, ...]
    members: tuple[SnapshotMember, ...]
    lexical_statistics_epoch: NonNegativeInt = 0

    def family(self, slot: str) -> SlotFamily | None:
        return next((item for item in self.families if item.slot == slot), None)
```

`NewBatch` is the class added in the batch repository task, now typed with `BatchReceiptVariant`.

- [ ] **Step 5: Read snapshots from SQL in the repository**

Add to `SqliteSnapshotRepository` (`adapters/sqlite/repositories/snapshots.py`; import `json`, `SlotFamily`, `SnapshotMember`, `SnapshotView`, `PublicationPlan`, `membership_sha`, and `family_from_row` from `.families`):

```python
    def view(self, snapshot_id: str) -> SnapshotView:
        row = self.db.execute(
            "SELECT workspace_id,parent_snapshot_id,membership_sha,config_sha,member_count "
            "FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Snapshot is unavailable")
        page_ids = tuple(
            r[0]
            for r in self.db.execute(
                "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? ORDER BY bucket",
                (snapshot_id,),
            )
        )
        families = tuple(
            SlotFamily(slot=r[0], required=bool(r[1]), family=family_from_row(r[2:]))
            for r in self.db.execute(
                "SELECT si.slot,si.required,f.family_id,f.channel,f.backend,f.provider,f.model,"
                "f.dimension,f.config_sha,f.config_json,f.backend_namespace,f.created_at "
                "FROM snapshot_indexes si JOIN index_families f USING(family_id) "
                "WHERE si.snapshot_id=? ORDER BY si.slot",
                (snapshot_id,),
            )
        )
        members = tuple(
            SnapshotMember(source_id=r[0], revision_id=r[1], extraction_id=r[2], is_default=bool(r[3]))
            for r in self.db.execute(
                "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members "
                "WHERE snapshot_id=? ORDER BY source_id,revision_id,extraction_id",
                (snapshot_id,),
            )
        )
        if membership_sha(page_ids) != row[2] or len(members) != row[4]:
            raise _integrity("Snapshot rows differ from their membership digest")
        epoch = self.db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0]
        return SnapshotView(
            snapshot_id=snapshot_id,
            workspace_id=row[0],
            parent_snapshot_id=row[1],
            config_sha=row[3],
            membership_sha=row[2],
            member_count=row[4],
            page_ids=page_ids,
            families=families,
            members=members,
            lexical_statistics_epoch=epoch,
        )

    def page_by_id(self, page_id: str) -> MembershipPage:
        row = self.db.execute(
            "SELECT bucket FROM membership_pages WHERE page_id=?", (page_id,)
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Membership page is unavailable")
        return self._load_page(page_id, row[0])

    def insert_snapshot(self, plan: PublicationPlan) -> None:
        buckets = dict(
            self.db.execute(
                "SELECT page_id,bucket FROM membership_pages "
                "WHERE page_id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(plan.page_ids)),),
            ).fetchall()
        )
        if set(buckets) != set(plan.page_ids) or len(set(buckets.values())) != len(buckets):
            raise _integrity("Snapshot pages are missing or share a bucket")
        self.db.execute(
            "INSERT INTO snapshots (snapshot_id,workspace_id,parent_snapshot_id,membership_sha,"
            "config_sha,manifest_sha,published_at,job_id,member_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                plan.snapshot_id,
                plan.workspace_id,
                plan.base_snapshot_id,
                membership_sha(plan.page_ids),
                plan.config_sha,
                plan.manifest.sha256,
                datetime.now(UTC).isoformat(),
                plan.job_id,
                plan.member_count,
            ),
        )
        self.db.executemany(
            "INSERT INTO snapshot_indexes (snapshot_id,slot,family_id,required) VALUES (?,?,?,?)",
            [(plan.snapshot_id, f.slot, f.family.family_id, int(f.required)) for f in plan.families],
        )
        self.db.executemany(
            "INSERT INTO snapshot_pages (snapshot_id,bucket,page_id) VALUES (?,?,?)",
            [(plan.snapshot_id, buckets[page_id], page_id) for page_id in plan.page_ids],
        )
        self.db.executemany(
            "INSERT INTO snapshot_root_batches (snapshot_id,slot,batch_id) VALUES (?,?,?)",
            [(plan.snapshot_id, b.slot, b.batch_id) for b in plan.root_batches],
        )
```

(import `from datetime import UTC, datetime`). `snapshot_root_batches` exists only for the corpus-wide graph generation; phase 6 removes it.

- [ ] **Step 6: Rebuild retained batch contents for read paths**

```python
# src/tychora/adapters/sqlite/retained_slots.py
"""Rebuild per-slot batch contents from SQL rows and batch input manifests.

Eligibility, native recovery and default bindings read these until the query
path joins against batches directly.
"""

import json
import sqlite3

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, GraphManifest
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    LexicalRecord,
    RetainedBatch,
    RetainedSlot,
    SnapshotView,
    VectorRecordBinding,
)
from tychora.contracts.snapshots import SlotFamily
from tychora.contracts.vectors import BatchReceipt
from tychora.ports.artifacts import Artifacts
from tychora.ports.repositories import BatchRow

from .repositories.batches import SqliteBatchRepository

_RECEIPTS = {"lexical": LexicalBatchReceipt, "text": BatchReceipt, "image": BatchReceipt, "graph": GraphBatchReceipt}


def _invalid(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


def _input(artifacts: Artifacts, ref: ArtifactRef) -> dict:
    with artifacts.open_verified(ref) as stream:
        raw = stream.read(ref.size_bytes + 1)
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _invalid("Batch input manifest is malformed") from None
    if canonical_bytes(document) != raw or document.get("type") != "index_input":
        raise _invalid("Batch input manifest is not canonical")
    return document["data"]


def _lexical(db: sqlite3.Connection, batch_id: str, entries: list[str]) -> tuple[LexicalRecord, ...]:
    rows = {
        row[0]: LexicalRecord(chunk_id=row[0], identifiers=row[1], title=row[2], headings=row[3], body=row[4], context=row[5])
        for row in db.execute(
            "SELECT chunk_id,identifiers,title,headings,body,context FROM lexical_records WHERE batch_id=?",
            (batch_id,),
        )
    }
    if set(rows) != set(entries):
        raise _invalid("Lexical rows differ from their batch input")
    return tuple(rows[chunk_id] for chunk_id in entries)


def _vectors(db: sqlite3.Connection, row: BatchRow, receipt: BatchReceipt, data: dict) -> tuple[VectorRecordBinding, ...]:
    values = {item.record_id: item.value_sha for item in receipt.records}
    groups = {item["record_id"]: item for item in data.get("representations", ())}
    bindings = []
    for record_id, chunk_id, asset_id, input_sha, row_index in db.execute(
        "SELECT record_id,chunk_id,asset_id,input_sha,row_index FROM vector_records "
        "WHERE batch_id=? ORDER BY record_id",
        (row.batch_id,),
    ):
        group = groups.get(record_id, {})
        bindings.append(
            VectorRecordBinding(
                record_id=record_id,
                input_sha=input_sha,
                value_sha=values[record_id],
                chunk_id=chunk_id,
                asset_id=asset_id,
                row_index=row_index,
                group_id=group.get("group_id"),
                dependency_ids=frozenset(group.get("dependency_ids", ())),
            )
        )
    return tuple(bindings)


def _batch(db: sqlite3.Connection, artifacts: Artifacts, item: SlotFamily, row: BatchRow) -> RetainedBatch:
    receipt_json = db.execute(
        "SELECT receipt_json FROM index_batches WHERE batch_id=?", (row.batch_id,)
    ).fetchone()[0]
    receipt = _RECEIPTS[item.slot].model_validate_json(receipt_json)
    data = _input(artifacts, row.input_manifest)
    return RetainedBatch(
        batch_id=row.batch_id,
        family_id=row.family_id,
        extraction_id=row.extraction_id,
        input_manifest=row.input_manifest,
        expected_count=row.expected_count,
        receipt=receipt,
        vectors=row.vectors,
        graph_manifest=GraphManifest.model_validate(data["graph_manifest"]) if item.slot == "graph" else None,
        lexical_records=_lexical(db, row.batch_id, list(data["entries"])) if item.slot == "lexical" else (),
        vector_records=_vectors(db, row, receipt, data) if item.slot in {"text", "image"} else (),
    )


def load_retained_slots(
    db: sqlite3.Connection, artifacts: Artifacts, view: SnapshotView
) -> tuple[RetainedSlot, ...]:
    batches = SqliteBatchRepository(db)
    return tuple(
        RetainedSlot(
            slot=item.slot,
            family=item.family,
            required=item.required,
            batches=tuple(
                _batch(db, artifacts, item, row)
                for row in batches.for_snapshot(view.snapshot_id, item.slot)
            ),
        )
        for item in view.families
    )
```

In `CorpusStore` (`adapters/sqlite/corpus.py`) replace `resolve_snapshot`, `pin_snapshot` and the two publication delegators with:

```python
    def resolve_snapshot(self, selector: str | None) -> SnapshotView:
        snapshot_id = selector or self.active_snapshot_id()
        if snapshot_id is None:
            raise TychoraError("NOT_FOUND", "No published snapshot is available")
        return SqliteSnapshotRepository(self.db).view(snapshot_id)

    @contextmanager
    def pin_snapshot(self, selector: str | None):
        """Lease one snapshot for a read; retention and GC honour the lease."""
        view = self.resolve_snapshot(selector)
        with self.leases.pin(frozenset({view.snapshot_id})):
            yield view

    def snapshot_slots(self, view: SnapshotView) -> tuple[RetainedSlot, ...]:
        return load_retained_slots(self.db, self.artifacts, view)

    def snapshots_repository(self) -> SqliteSnapshotRepository:
        return SqliteSnapshotRepository(self.db)

    def publish_snapshot(self, plan: PublicationPlan) -> str:
        from .publication import publish_snapshot

        return publish_snapshot(self, plan)
```

Delete `insert_snapshot_bindings` and `set_active_snapshot` from `CorpusStore` (the repository owns both). The config artifact check in `read_eligibility_facts` (lines 110-131) is unchanged in this phase.

- [ ] **Step 7: Rewrite publication**

Replace `src/tychora/adapters/sqlite/publication.py` with the module below; keep `check_publication_target` (lines 461-529) verbatim at the place marked.

```python
"""Verify the batches an add wrote, then switch the active snapshot in one transaction."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from tychora.adapters.files.references import MAX_MANIFEST_BYTES
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.application.collection_policy import (
    collection_is_active,
    require_active_collection,
)
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, GraphManifest
from tychora.contracts.identity import canonical_bytes, digest, source_id
from tychora.contracts.jobs import JobAuthorization, PublicationReceipt
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    NewBatch,
    PublicationPlan,
    SnapshotView,
    snapshot_root_document,
)
from tychora.contracts.snapshots import MembershipPage
from tychora.contracts.vectors import BatchReceipt
from tychora.indexing.receipts import receipt_digest
from tychora.indexing.vector_batches import decode_vector_batch, row_value_sha
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.adapters.sqlite.corpus import CorpusStore


def _fail(message: str, reason: str = "canonical.binding_mismatch") -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message, reason=reason)


def _read_json(store: CorpusStore, ref: ArtifactRef) -> dict:
    with store.artifacts.open_verified(ref) as stream:
        raw = stream.read(ref.size_bytes + 1)
    if len(raw) != ref.size_bytes:
        _fail("Publication artifact size differs", "payload.hash_mismatch")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _fail("Publication manifest is malformed", "canonical.row_invalid")
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        _fail("Publication manifest is not canonical JSON", "canonical.row_invalid")
    return value


def page_ref(page: MembershipPage) -> ArtifactRef:
    raw = page.manifest_bytes()
    sha = sha256(raw).hexdigest()
    return ArtifactRef(sha256=sha, size_bytes=len(raw), media_type="application/json", storage_key=sha)


def _verify_documents(store: CorpusStore, plan: PublicationPlan) -> None:
    """Check the root and new pages exist and the root binds exactly these pages."""
    manifests = dict(store.snapshots_repository().page_manifests(plan.reused_page_ids))
    for page in plan.new_pages:
        ref = page_ref(page)
        if not store.artifacts.stat(ref).available:
            _fail("Membership page bytes are missing", "payload.missing")
        manifests[page.page_id] = ref
    if _read_json(store, plan.manifest) != snapshot_root_document(plan, manifests):
        _fail("Snapshot root differs from its publication plan")


def _verify_families(store: CorpusStore, plan: PublicationPlan, base: SnapshotView | None) -> None:
    slots = {item.slot: item for item in plan.families}
    if not store.required_slots <= slots.keys() or any(not slots[key].required for key in store.required_slots):
        _fail("Publication omits a required index slot")
    for item in plan.families:
        origin = item.family.config_json.get("effective_config_sha")
        if not isinstance(origin, str):
            _fail("Snapshot slot family has no configuration binding", "config.drift")
        if origin == plan.config_sha:
            continue
        pinned = base.family(item.slot) if base is not None else None
        if (
            pinned is None
            or pinned.family != item.family
            or not pinned_runtime_compatible(
                store.retained_config_sha(origin), store.retained_config_sha(plan.config_sha)
            )
        ):
            _fail("Snapshot slot family or configuration differs", "config.drift")


def _verify_lexical(store: CorpusStore, batch: NewBatch, receipt: LexicalBatchReceipt) -> None:
    entries = tuple(_read_json(store, batch.input_manifest)["data"]["entries"])
    staged = {
        row[0]
        for row in store.db.execute("SELECT chunk_id FROM lexical_records WHERE batch_id=?", (batch.batch_id,))
    }
    if (
        receipt.count != batch.expected_count
        or len(entries) != receipt.count
        or staged != set(entries)
        or receipt.entry_ids_sha != digest("index-entries-v1", entries)
    ):
        _fail("Lexical batch rows differ from their receipt")


def _verify_vector(store: CorpusStore, plan: PublicationPlan, batch: NewBatch, receipt: BatchReceipt) -> None:
    family = next(item.family for item in plan.families if item.family.family_id == batch.family_id)
    if receipt.count != batch.expected_count or receipt.digest != receipt_digest(receipt.records):
        _fail("Vector batch receipt differs from its records")
    if receipt.records:
        if batch.vectors is None:
            _fail("Vector batch artifact is missing", "payload.missing")
        with store.artifacts.open_verified(batch.vectors) as stream:
            decoded = decode_vector_batch(stream.read(batch.vectors.size_bytes + 1))
        if decoded.family_id != batch.family_id or decoded.dimension != family.dimension or tuple(
            (row.record_id, row.input_sha, row_value_sha(row.values)) for row in decoded.rows
        ) != tuple((r.record_id, r.input_sha, r.value_sha) for r in receipt.records):
            _fail("Vector batch artifact differs from its receipt", "payload.hash_mismatch")
    index = store.vector_indexes.get(batch.family_id)
    if index is None:
        raise TychoraError("CAPABILITY_MISSING", "Required vector index is unavailable")
    verification = index.verify_batch(receipt)
    if not verification.ready or not verification.config_matches or verification.missing_record_ids or verification.mismatched_record_ids:
        _fail("Native vector batch is incomplete")


def _verify_graph(store: CorpusStore, batch: NewBatch, receipt: GraphBatchReceipt, ctx: ExecutionContext) -> None:
    manifest = GraphManifest.model_validate(_read_json(store, batch.input_manifest)["data"]["graph_manifest"])
    graph = store.graph_indexes.get(receipt.generation_id)
    if graph is None:
        raise TychoraError("CAPABILITY_MISSING", "Required graph generation is unavailable")
    if graph.seal_generation(receipt.generation_id, manifest, ctx) != receipt:
        _fail("Native graph generation receipt differs")


def _verify_new_batches(store: CorpusStore, plan: PublicationPlan, ctx: ExecutionContext) -> None:
    """Verify only what this publication wrote; reused batches are counted, not opened."""
    bound = {
        batch.batch_id for page in plan.new_pages for member in page.members for batch in member.batches
    } | {batch.batch_id for batch in plan.root_batches}
    with SqliteUnitOfWork(store.db).read() as tx:
        ready = tx.batches.count_ready(tuple(bound))
    if ready != len(bound):
        raise TychoraError("INDEX_INCOMPLETE", "A bound batch is not ready", reason="batch.missing")
    for batch in plan.new_batches:
        ctx.checkpoint()
        receipt = batch.receipt
        if isinstance(receipt, LexicalBatchReceipt):
            _verify_lexical(store, batch, receipt)
        elif isinstance(receipt, BatchReceipt):
            _verify_vector(store, plan, batch, receipt)
        else:
            _verify_graph(store, batch, receipt, ctx)
```

The transaction half:

```python
def _check_base_and_job(store: CorpusStore, plan: PublicationPlan, tx) -> None:
    if tx.snapshots.active_id() != plan.base_snapshot_id:
        raise TychoraError("WORKSPACE_BUSY", "Publication base changed; rebase required.", retryable=True, reason="publication.rebase")
    if plan.graph_watermark is not None and tx.workspace.decision_watermark() != plan.graph_watermark:
        raise TychoraError("WORKSPACE_BUSY", "Graph decisions changed; rebase required.", retryable=True, reason="publication.rebase")
    job = store.db.execute(
        "SELECT state,effective_config_sha,result_snapshot_id FROM jobs WHERE job_id=?", (plan.job_id,)
    ).fetchone()
    if job is None or job[0] != "running" or job[1] != plan.config_sha or job[2] not in (None, plan.snapshot_id):
        _fail("Publication job is absent, cancelled, or has conflicting bindings")
    if tx.workspace.workspace_id() != plan.workspace_id:
        _fail("Publication workspace identity differs")


def _check_members(store: CorpusStore, plan: PublicationPlan, target: str | None) -> None:
    """Check new pages' members in one statement: registered, not withdrawn, active collection."""
    members = {m.extraction_id: m for page in plan.new_pages for m in page.members}
    rows = {
        row[0]: row[1:]
        for row in store.db.execute(
            "SELECT e.extraction_id,r.source_id,e.revision_id,c.workspace_id,c.policy_json "
            "FROM extractions e JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) "
            "WHERE e.extraction_id IN (SELECT value FROM json_each(?)) AND s.withdrawn_at IS NULL",
            (json.dumps(sorted(members)),),
        )
    }
    prior = {
        row[0]
        for row in store.db.execute(
            "SELECT extraction_id FROM snapshot_members WHERE snapshot_id=?", (plan.base_snapshot_id,)
        )
    } if plan.base_snapshot_id else set()
    for extraction_id, member in members.items():
        row = rows.get(extraction_id)
        if row is None or row[:3] != (member.source_id, member.revision_id, plan.workspace_id):
            _fail("Snapshot member is not a registered extraction")
        if not collection_is_active(row[3]) and (target is None or member.source_id == target or extraction_id not in prior):
            require_active_collection(row[3])


def _check_ownership(store: CorpusStore, plan: PublicationPlan) -> None:
    """New batch rows must belong to their batch's extraction (one grouped query per kind)."""
    expected = {b.batch_id: b.extraction_id for b in plan.new_batches if b.extraction_id is not None}
    ids = json.dumps(sorted(expected))
    found = list(
        store.db.execute(
            "SELECT l.batch_id,c.extraction_id FROM lexical_records l JOIN chunks c USING(chunk_id) "
            "WHERE l.batch_id IN (SELECT value FROM json_each(?)) GROUP BY 1,2",
            (ids,),
        )
    ) + list(
        store.db.execute(
            "SELECT v.batch_id,COALESCE(c.extraction_id,a.extraction_id) FROM vector_records v "
            "LEFT JOIN chunks c USING(chunk_id) LEFT JOIN assets a USING(asset_id) "
            "WHERE v.batch_id IN (SELECT value FROM json_each(?)) GROUP BY 1,2",
            (ids,),
        )
    )
    if any(expected.get(batch_id) != extraction_id for batch_id, extraction_id in found):
        _fail("Index record is not owned by its batch's extraction")


def publish_snapshot(store: CorpusStore, plan: PublicationPlan) -> str:
    try:
        plan = PublicationPlan.model_validate_json(canonical_bytes(plan.model_dump(mode="json")))
    except ValueError:
        _fail("Publication plan is invalid", "canonical.row_invalid")
    if store.db.in_transaction:
        _fail("Publication requires its own transaction", "invariant.unexpected")
    ctx = store.publication_context or ExecutionContext()
    base = store.resolve_snapshot(plan.base_snapshot_id) if plan.base_snapshot_id else None
    uow = SqliteUnitOfWork(store.db)
    with store.leases.pin(frozenset({plan.snapshot_id, *(b.batch_id for b in plan.new_batches)})):
        _verify_families(store, plan, base)
        _verify_documents(store, plan)
        _verify_new_batches(store, plan, ctx)
        ctx.checkpoint()
        try:
            with uow.write() as tx:
                ctx.checkpoint()
                _check_base_and_job(store, plan, tx)
                _check_members(store, plan, check_publication_target(store, plan))
                _check_ownership(store, plan)
                for page in plan.new_pages:
                    ref = page_ref(page)
                    tx.artifacts.register(ref)
                    tx.snapshots.insert_page(page, ref)
                tx.artifacts.register(plan.manifest)
                tx.snapshots.insert_snapshot(plan)
                store.db.execute(
                    "UPDATE jobs SET base_snapshot_id=?,result_snapshot_id=?,updated_at=? WHERE job_id=?",
                    (plan.base_snapshot_id, plan.snapshot_id, datetime.now(UTC).isoformat(), plan.job_id),
                )
                ctx.checkpoint()
                tx.snapshots.set_active(plan.snapshot_id)
        except sqlite3.IntegrityError:
            _fail("Publication foreign-key or uniqueness constraint failed")
    return plan.snapshot_id


def resolve_snapshot(store: CorpusStore, selector: str | None) -> SnapshotView:
    return store.resolve_snapshot(selector)


# check_publication_target: keep the existing function verbatim here.


class PublicationReceipts:
    """Trusted canonical job lookup, including publication before the completion event."""

    def __init__(self, store: CorpusStore):
        self.store = store

    def lookup(self, job_id: str) -> PublicationReceipt | None:
        row = self.store.db.execute(
            "SELECT j.result_snapshot_id,s.job_id FROM jobs j "
            "LEFT JOIN snapshots s ON s.snapshot_id=j.result_snapshot_id WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        if row[1] != job_id:
            _fail("Publication receipt job binding differs")
        return PublicationReceipt(job_id=job_id, result_snapshot_id=row[0])
```

Every function above is under 80 lines. The functions `_manifest_document`, `publication_manifest`, `_verify_artifacts`, `_verify_batches`, `_register_artifact` and `insert_snapshot_bindings` are deleted.

- [ ] **Step 8: Coordinator: preparation**

Replace `PreparedProjection` (lines 101-116):

```python
@dataclass(frozen=True)
class PreparedProjection:
    bundle: ExtractionBundle
    base_snapshot_id: str | None
    base: SnapshotView | None
    base_page: MembershipPage | None
    job: JobHandle
    config_ref: ArtifactRef
    windows: tuple[SearchWindow, ...]
    old_families: dict[str, SlotFamily]
    lexical_family: IndexFamily
    text_family: IndexFamily | None
    text_spec: FamilySpec | None
    text_groups: tuple[ContextGroup, ...]
    image_family: IndexFamily | None
    image_spec: FamilySpec | None
    visual_inputs: tuple[VisualInput, ...]
```

`_retained_vector_spec(self, slot: SlotFamily | None, channel: str)` keeps its body (it only reads `slot.family`). Replace `prepare` (lines 896-1037) with:

```python
    def prepare(self, bundle, base_snapshot_id, job, ctx) -> PreparedProjection:
        ctx.checkpoint()
        self._require_collection_open(bundle.collection.collection_id)
        config_ref = self._config_ref(job)
        windows = self._windows(bundle)
        canonical = self._canonical_bundle(bundle, windows)
        self._commit_or_reuse(canonical)
        base = self.store.resolve_snapshot(base_snapshot_id) if base_snapshot_id else None
        base_page = (
            self.store.snapshots_repository().page(base_snapshot_id, bucket_of(canonical.source.source_id))
            if base_snapshot_id
            else None
        )
        old = {item.slot: item for item in base.families} if base else {}
        text_family, text_spec, text_groups = self._prepare_text(old.get("text"), canonical, windows, config_ref)
        image_family, image_spec, visual_inputs = self._prepare_image(old.get("image"), canonical, config_ref)
        return PreparedProjection(
            bundle=canonical,
            base_snapshot_id=base_snapshot_id,
            base=base,
            base_page=base_page,
            job=job,
            config_ref=config_ref,
            windows=windows,
            old_families=old,
            lexical_family=self._select_lexical_family(old.get("lexical"), canonical, config_ref),
            text_family=text_family,
            text_spec=text_spec,
            text_groups=text_groups,
            image_family=image_family,
            image_spec=image_spec,
            visual_inputs=visual_inputs,
        )

    def _require_collection_open(self, collection_id: str) -> None:
        row = self.store.db.execute(
            "SELECT policy_json FROM collections WHERE collection_id=?", (collection_id,)
        ).fetchone()
        if row is not None:
            require_active_collection(row[0])

    def _select_lexical_family(self, old: SlotFamily | None, bundle: ExtractionBundle, config_ref: ArtifactRef) -> IndexFamily:
        origin = old.family.config_json.get("effective_config_sha") if old is not None else None
        if old is not None and isinstance(origin, str) and (
            origin == config_ref.sha256
            or pinned_runtime_compatible(self.store.retained_config_sha(origin), self.config)
        ):
            return old.family
        return self._lexical_family(bundle.workspace.workspace_id, config_ref.sha256, bundle.workspace.created_at)

    def _ready_batch(self, family: IndexFamily, bundle: ExtractionBundle) -> BatchRow | None:
        with SqliteUnitOfWork(self.store.db).read() as tx:
            row = tx.batches.find(family.family_id, bundle.extraction.extraction_id)
        return row if row is not None and row.state == "ready" else None

    def _prepare_text(self, old, canonical, windows, config_ref):
        if self.config.values.index.text.backend == "disabled":
            return None, None, ()
        spec = self._retained_vector_spec(old, "text")
        if spec is not None:
            family = old.family
        else:
            family, spec = self._text_family(canonical.workspace.workspace_id, config_ref.sha256, canonical.workspace.created_at)
        if self._ready_batch(family, canonical) is not None:
            return family, spec, ()
        return family, spec, build_groups(windows, self.config, self.counter)

    def _prepare_image(self, old, canonical, config_ref):
        if self.config.values.index.image.backend == "disabled":
            return None, None, ()
        spec = self._retained_vector_spec(old, "image")
        if spec is not None:
            family = old.family
        else:
            family, spec = self._image_family(canonical.workspace.workspace_id, config_ref.sha256, canonical.workspace.created_at)
        if self._ready_batch(family, canonical) is not None:
            return family, spec, ()
        return family, spec, self._visual_inputs(canonical)
```

The O(corpus) withdrawn-source loop of the old `prepare` is gone: only the member's bucket is rewritten, and a withdrawn source in another bucket stays in that bucket's page until the page is next rewritten; eligibility already excludes withdrawn sources, so search results do not change.

- [ ] **Step 9: Coordinator: batch writes**

`_lexical_batch` becomes:

```python
    def _lexical_page_batch(self, prepared: PreparedProjection, written: list[NewBatch]) -> PageBatch:
        family = prepared.lexical_family
        existing = self._ready_batch(family, prepared.bundle)
        if existing is not None:
            return existing.page_batch()
        records = self._lexical_records(prepared.bundle, prepared.windows)
        entries = tuple(row.chunk_id for row in records)
        manifest = _input_manifest(self.artifacts, entries)
        batch = NewBatch(
            slot="lexical",
            batch_id=_batch_id(family.family_id, prepared.bundle.extraction.extraction_id, manifest),
            family_id=family.family_id,
            extraction_id=prepared.bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(records),
            receipt=LexicalBatchReceipt(
                family_id=family.family_id, count=len(records), entry_ids_sha=digest("index-entries-v1", entries)
            ),
        )
        with SqliteUnitOfWork(self.store.db).write() as tx:
            tx.batches.begin(batch)
            tx.batches.write_lexical(batch.batch_id, records)
            tx.batches.mark_ready(batch.batch_id, len(records))
            tx.workspace.bump_lexical_epoch()
        written.append(batch)
        return batch.page_batch()
```

where `_lexical_records(bundle, windows)` is the `records = tuple(LexicalRecord(...) for window in windows)` expression of the old `_lexical_batch`, moved into its own method.

`_text_batch` and `_image_batch` keep their validation, re-keying (Task 4) and artifact (Task 5) code; their tail changes from building a `PublicationBatch` to:

```python
        batch = NewBatch(
            slot="text",
            batch_id=_batch_id(family.family_id, bundle.extraction.extraction_id, manifest),
            family_id=family.family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(bindings),
            receipt=receipt,
            vectors=vectors,
        )
        rows = tuple(
            VectorRecordRow(
                record_id=b.record_id, chunk_id=b.chunk_id, asset_id=b.asset_id,
                input_sha=b.input_sha, row_index=b.row_index,
            )
            for b in bindings
        )
        with SqliteUnitOfWork(self.store.db).write() as tx:
            tx.batches.begin(batch)
            tx.batches.write_vectors(batch.batch_id, rows)
            tx.batches.mark_ready(batch.batch_id, len(rows))
        return batch
```

(`slot="image"` in `_image_batch`). Add a wrapper used by `finalize`:

```python
    def _vector_page_batch(self, slot, prepared, family, spec, records, written, ctx) -> PageBatch:
        existing = self._ready_batch(family, prepared.bundle)
        if existing is not None:
            if records:
                _fail("Unrequested provider records cannot replace an immutable source batch")
            return existing.page_batch()
        batch = (
            self._text_batch(prepared.bundle, prepared.windows, family, spec, prepared.text_groups, records, ctx)
            if slot == "text"
            else self._image_batch(prepared.bundle, family, spec, prepared.visual_inputs, records, ctx)
        )
        written.append(batch)
        return batch.page_batch()
```

`_graph_slot` becomes `_graph_batch(self, members, workspace_id, config_sha, created_at, ctx) -> tuple[IndexFamily, NewBatch]`: its family and generation code is unchanged; the input manifest now carries the generation manifest so read paths and publication can load it without the snapshot manifest:

```python
        input_ref = self.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "index_input",
                        "schema_version": "1",
                        "data": {
                            "entries": sorted(manifest.evidence_ids),
                            "graph_manifest": manifest.model_dump(mode="json"),
                        },
                    }
                )
            ),
            "application/json",
        )
        batch = NewBatch(
            slot="graph",
            batch_id=_batch_id(family_id, None, input_ref),
            family_id=family_id,
            extraction_id=None,
            input_manifest=input_ref,
            expected_count=receipt.evidence_count,
            receipt=receipt,
        )
        with SqliteUnitOfWork(self.store.db).write() as tx:
            if tx.batches.get(batch.batch_id) is None:
                tx.batches.begin(batch)
                tx.batches.mark_ready(batch.batch_id, receipt.evidence_count)
        return family, batch
```

This transitional generation is still corpus-wide (one per add), exactly as today; phase 6 replaces it with per-member contributions. Update `verify_canonical_native_batch` in `application/recovery.py:192-207` so the expected graph input document is `{"entries": entries, "graph_manifest": batch.graph_manifest.model_dump(mode="json")}`.

- [ ] **Step 10: Coordinator: finalize, plan, reuse and attach**

```python
    def finalize(self, prepared, text_records, ctx, visual_records=()) -> PublicationPlan:
        ctx.checkpoint()
        if self._config_ref(prepared.job) != prepared.config_ref:
            _fail("Projection configuration changed before finalization")
        self._require_active_source(prepared.bundle.source.source_id)
        written: list[NewBatch] = []
        families = [SlotFamily(slot="lexical", family=prepared.lexical_family)]
        batches = [self._lexical_page_batch(prepared, written)]
        for slot, family, spec, records in (
            ("text", prepared.text_family, prepared.text_spec, text_records),
            ("image", prepared.image_family, prepared.image_spec, visual_records),
        ):
            if family is None or spec is None:
                if records:
                    _fail(f"{slot.capitalize()} records were supplied to a disabled slot")
                continue
            families.append(SlotFamily(slot=slot, family=family))
            batches.append(self._vector_page_batch(slot, prepared, family, spec, records, written, ctx))
        page = self._next_page(prepared, tuple(batches))
        for item in families:
            self._require_rebase_free(prepared, item, page)
        root_batches: tuple[PageBatch, ...] = ()
        watermark = None
        if self.config.values.index.graph != "disabled":
            graph_family, graph_batch = self._graph_batch(
                self._snapshot_members(prepared.base, page),
                prepared.bundle.workspace.workspace_id,
                prepared.config_ref.sha256,
                prepared.bundle.workspace.created_at,
                ctx,
            )
            families.append(SlotFamily(slot="graph", family=graph_family))
            written.append(graph_batch)
            root_batches = (graph_batch.page_batch(),)
            watermark = graph_batch.receipt.decision_watermark
        return self._plan(
            job=prepared.job,
            workspace_id=prepared.bundle.workspace.workspace_id,
            base=prepared.base,
            base_page=prepared.base_page,
            config_ref=prepared.config_ref,
            families=tuple(families),
            page=page,
            written=tuple(written),
            root_batches=root_batches,
            watermark=watermark,
        )

    def _next_page(self, prepared, batches) -> MembershipPage:
        bundle = prepared.bundle
        member = PageMember(
            source_id=bundle.source.source_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
            batches=tuple(sorted(batches, key=lambda batch: batch.slot)),
        )
        same_source = [
            m.revision_id for m in (prepared.base_page.members if prepared.base_page else ()) if m.source_id == member.source_id
        ]
        editions = {
            revision: self.store.db.execute(
                "SELECT edition_key FROM revisions WHERE revision_id=?", (revision,)
            ).fetchone()[0]
            for revision in same_source
        }
        return next_page(prepared.base_page, member, editions, bundle.revision.edition_key)

    def _require_rebase_free(self, prepared, item: SlotFamily, page: MembershipPage) -> None:
        """Earlier members must already have a batch in the family this add publishes."""
        old = prepared.old_families.get(item.slot)
        if old is not None and old.family.family_id == item.family.family_id:
            return
        survivors = member_count_after(
            prepared.base.member_count if prepared.base else 0, prepared.base_page, page
        ) - 1
        if survivors > 0:
            raise TychoraError(
                "INDEX_INCOMPLETE",
                f"Earlier {item.slot} source batches are unavailable for rebase",
                reason="batch.missing",
            )

    @staticmethod
    def _snapshot_members(base: SnapshotView | None, page: MembershipPage) -> tuple[SnapshotMember, ...]:
        kept = [m for m in (base.members if base else ()) if bucket_of(m.source_id) != page.bucket]
        added = [
            SnapshotMember(source_id=m.source_id, revision_id=m.revision_id, extraction_id=m.extraction_id, is_default=m.is_default)
            for m in page.members
        ]
        return tuple(sorted(kept + added, key=lambda m: (m.source_id, m.revision_id, m.extraction_id)))

    def _plan(
        self, *, job, workspace_id, base, base_page, config_ref, families, page, written, root_batches, watermark
    ) -> PublicationPlan:
        base_snapshot_id = base.snapshot_id if base else None
        replaced = base_page.page_id if base_page else None
        reused = tuple(p for p in (base.page_ids if base else ()) if p != replaced)
        page_bytes = page.manifest_bytes()
        manifests = dict(self.store.snapshots_repository().page_manifests(reused)) if reused else {}
        manifests[page.page_id] = self.artifacts.put_stream(BytesIO(page_bytes), "application/json")
        draft = PublicationPlan(
            job_id=job.id,
            snapshot_id=snapshot_id_v2(
                workspace_id, base_snapshot_id, job.id, config_ref.sha256, families,
                (*reused, page.page_id), root_batch_ids=[batch.batch_id for batch in root_batches],
            ),
            workspace_id=workspace_id,
            base_snapshot_id=base_snapshot_id,
            config_sha=config_ref.sha256,
            families=families,
            member_count=member_count_after(base.member_count if base else 0, base_page, page),
            new_pages=(page,),
            reused_page_ids=reused,
            new_batches=written,
            root_batches=root_batches,
            graph_watermark=watermark,
            manifest=config_ref,
        )
        root = self.artifacts.put_stream(
            BytesIO(canonical_bytes(snapshot_root_document(draft, manifests))), "application/json"
        )
        return draft.model_copy(update={"manifest": root})
```

`manifest=config_ref` is a placeholder `ArtifactRef` that the validator does not inspect; the returned plan always carries the root. Replace `reuse_existing` (lines 1216-1274):

```python
    def reuse_existing(self, source_id, revision_id, base_snapshot_id, job, ctx) -> PublicationPlan:
        """A fresh capture of an active immutable revision republishes its retained batches."""
        ctx.checkpoint()
        self._require_active_source(source_id)
        base = self.store.resolve_snapshot(base_snapshot_id)
        base_page = self.store.snapshots_repository().page(base_snapshot_id, bucket_of(source_id))
        if base_page is None or not any(
            m.source_id == source_id and m.revision_id == revision_id for m in base_page.members
        ):
            _fail("Captured revision is not the selected canonical source")
        members = tuple(
            m.model_copy(update={"is_default": m.revision_id == revision_id}) if m.source_id == source_id else m
            for m in base_page.members
        )
        page = MembershipPage(
            bucket=base_page.bucket, members=members, page_id=compute_page_id(base_page.bucket, members)
        )
        config_ref = self._config_ref(job)
        if base.config_sha != config_ref.sha256 and not pinned_runtime_compatible(
            self.store.retained_config(base), self.config
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE", "Existing projection configuration differs", reason="batch.missing"
            )
        families = tuple(item for item in base.families if item.slot != "graph")
        written: tuple[NewBatch, ...] = ()
        root_batches: tuple[PageBatch, ...] = ()
        watermark = None
        if self.config.values.index.graph != "disabled":
            created_at = self.store.db.execute("SELECT created_at FROM workspaces").fetchone()[0]
            graph_family, graph_batch = self._graph_batch(
                self._snapshot_members(base, page), base.workspace_id, config_ref.sha256, created_at, ctx
            )
            families = (*families, SlotFamily(slot="graph", family=graph_family))
            written, root_batches = (graph_batch,), (graph_batch.page_batch(),)
            watermark = graph_batch.receipt.decision_watermark
        return self._plan(
            job=job,
            workspace_id=base.workspace_id,
            base=base,
            base_page=base_page,
            config_ref=config_ref,
            families=families,
            page=page,
            written=written,
            root_batches=root_batches,
            watermark=watermark,
        )
```

`_graph_batch` returns the existing batch unchanged when membership and decision watermark are unchanged (its `batch_id` is content-addressed and `begin` is skipped), so a no-op re-add costs one generation read, as today.

Replace the loop of `attach` (lines 1276-1319):

```python
    def attach(self, plan: PublicationPlan, ctx: ExecutionContext) -> None:
        """Reopen configured native ports for a retained plan before verification."""
        from tychora.adapters.sqlite.publication import check_publication_target

        check_publication_target(self.store, plan)
        for item in plan.families:
            ctx.checkpoint()
            if item.slot in {"text", "image"}:
                self._attach_vector(item)
        for batch in plan.root_batches:
            self._attach_graph(batch, ctx)

    def _attach_vector(self, item: SlotFamily) -> None:
        if item.family.family_id in self.store.vector_indexes:
            return
        index = self.text_index if item.slot == "text" else self.image_index
        if index is None:
            raise TychoraError("CAPABILITY_MISSING", "Required native vector index is unavailable")
        try:
            spec = FamilySpec.model_validate_json(canonical_bytes(item.family.config_json.get("spec")))
        except (TypeError, ValueError):
            _fail("Retained vector family specification is malformed")
        if spec.family_id != item.family.family_id:
            _fail("Retained vector family identity differs")
        if index.create_family(spec).config_sha != item.family.config_sha:
            _fail("Retained native vector family configuration differs")
        self.store.vector_indexes[item.family.family_id] = index

    def _attach_graph(self, batch: PageBatch, ctx: ExecutionContext) -> None:
        if self.graph_factory is None:
            raise TychoraError("CAPABILITY_MISSING", "Required graph generation factory is unavailable")
        with self.artifacts.open_verified(batch.input_manifest) as stream:
            data = json.load(stream)["data"]
        manifest = GraphManifest.model_validate(data["graph_manifest"])
        if manifest.spec.generation_id in self.store.graph_indexes:
            return
        build = read_graph_build(self.store, manifest.spec, ctx)
        if GraphManifest.from_build(build) != manifest:
            _fail("Retained graph canonical build differs")
        self.store.graph_indexes[manifest.spec.generation_id] = self.graph_factory(build)
```

`build` is unchanged. Imports added to the coordinator: `json`, `BatchRow` and `VectorRecordRow` from `tychora.ports.repositories`, `MembershipPage`, `PageBatch`, `PageMember`, `SlotFamily`, `bucket_of`, `compute_page_id`, `snapshot_id_v2` from `tychora.contracts.snapshots`, `NewBatch`, `PublicationPlan`, `SnapshotMember`, `SnapshotView`, `snapshot_root_document` from `tychora.contracts.publication`, and `member_count_after`, `next_page` from `tychora.indexing.planning`; remove `publication_manifest`, `PublicationBatch`, `PublicationSlot`.

- [ ] **Step 11: Move consumers**

- `src/tychora/retrieval/eligibility.py`: `EligibilityFacts` gains `slots: tuple[RetainedSlot, ...] = ()`; at lines 605, 613 and 641 replace `snapshot.slots` with `facts.slots`.
- `src/tychora/adapters/sqlite/eligibility.py`: inside `read_eligibility_facts`, after `db.execute("BEGIN")`, add `slots = load_retained_slots(db, store.artifacts, snapshot)`; replace `for slot in snapshot.slots:` (line 489) with `for slot in slots:`; pass `slots=slots` to `EligibilityFacts(...)`.
- `src/tychora/application/query.py:375`: `slot = snapshot.family(channel)` (the loop only reads `slot.family`).
- `src/tychora/application/default_bindings.py:244,255`: `for slot in owner.store.snapshot_slots(view):`.
- `src/tychora/application/recovery.py:565,586,814`: `slots = store.snapshot_slots(snapshot)` once, then `for slot in slots:`. Type hints `PublicationBatch` → `RetainedBatch`, `PublicationSlot` → `RetainedSlot` throughout the module.
- `src/tychora/application/inspection.py:690-695`: the synthetic view update becomes `{"snapshot_id": membership, "membership_sha": membership, "members": members, "member_count": len(members), "page_ids": (), "families": ()}`; `load_retained_slots` returns `()` for a view with no families, exactly as the old `"slots": ()` did.
- `src/tychora/application/sources.py`: `_plan_watermark(plan)` returns `plan.graph_watermark`. The checkpoint at lines 780-791 stores the plan document instead of the root:

```python
                plan_ref = await self.owner.run_blocking(
                    lambda plan=plan: self.owner.artifacts.put_stream(
                        BytesIO(canonical_bytes(publication_plan_document(plan))), "application/json"
                    ),
                    ctx,
                )
```

and passes `plan_ref` to `jobs.checkpoint(...)`. `_completed_plan` (lines 329-342) requires `manifest.get("type") == "publication_plan"` and parses `PublicationPlan.model_validate_json(canonical_bytes(manifest["data"]))`.
- `src/tychora/adapters/files/references.py:279-302`: `add_manifest` builds `ManifestRef(artifact=ref, type=kind, schema_version=MANIFEST_VERSIONS[kind])`; add the root `("SELECT manifest_sha FROM membership_pages", "membership_page")`.
- `src/tychora/application/removal.py`: `_manifest_ref` uses `schema_version=MANIFEST_VERSIONS[kind]`; `typed_roots` gains `("SELECT manifest_sha FROM membership_pages", "membership_page")`; the `direct` query gains `UNION ALL SELECT 1 FROM membership_pages WHERE manifest_sha=? ` and passes `(sha,) * 19`.

- [ ] **Step 12: Replace hand-built plans in tests**

```python
# tests/helpers/publication.py
"""Build a one-member lexical publication plan with production planning code."""

from io import BytesIO

from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.identity import canonical_bytes, digest, family_id, new_family_nonce
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    LexicalRecord,
    NewBatch,
    PublicationPlan,
    snapshot_root_document,
)
from tychora.contracts.snapshots import IndexFamily, PageMember, SlotFamily, bucket_of, snapshot_id_v2
from tychora.indexing.planning import member_count_after, next_page


def lexical_plan(store, artifacts, bundle, job_id: str, config_sha: str, base: str | None, body: str) -> PublicationPlan:
    uow = SqliteUnitOfWork(store.db)
    with uow.read() as tx:
        nonce = tx.families.nonce("lexical", config_sha) or new_family_nonce()
    family = IndexFamily(
        family_id=family_id(bundle.workspace.workspace_id, config_sha, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha=config_sha,
        config_json=FrozenJsonMap({"tokenizer": "unicode61", "effective_config_sha": config_sha}),
        backend_namespace="lexical",
        created_at=bundle.workspace.created_at,
    )
    entries = (bundle.chunks[0].chunk_id,)
    manifest = artifacts.put_stream(
        BytesIO(canonical_bytes({"type": "index_input", "schema_version": "1", "data": {"entries": entries}})),
        "application/json",
    )
    batch = NewBatch(
        slot="lexical",
        batch_id=digest("index-batch-v1", family.family_id, bundle.extraction.extraction_id, manifest.sha256),
        family_id=family.family_id,
        extraction_id=bundle.extraction.extraction_id,
        input_manifest=manifest,
        expected_count=1,
        receipt=LexicalBatchReceipt(family_id=family.family_id, count=1, entry_ids_sha=digest("index-entries-v1", entries)),
    )
    with uow.write() as tx:
        tx.families.create(family, nonce, config_sha)
        if tx.batches.get(batch.batch_id) is None:
            tx.batches.begin(batch)
            tx.batches.write_lexical(batch.batch_id, (LexicalRecord(chunk_id=entries[0], body=body),))
            tx.batches.mark_ready(batch.batch_id, 1)
    repository = store.snapshots_repository()
    view = store.resolve_snapshot(base) if base else None
    bucket = bucket_of(bundle.source.source_id)
    base_page = repository.page(base, bucket) if base else None
    member = PageMember(
        source_id=bundle.source.source_id,
        revision_id=bundle.revision.revision_id,
        extraction_id=bundle.extraction.extraction_id,
        batches=(batch.page_batch(),),
    )
    page = next_page(base_page, member, {}, bundle.revision.edition_key)
    reused = tuple(p for p in (view.page_ids if view else ()) if p != (base_page.page_id if base_page else None))
    manifests = dict(repository.page_manifests(reused)) if reused else {}
    manifests[page.page_id] = artifacts.put_stream(BytesIO(page.manifest_bytes()), "application/json")
    families = (SlotFamily(slot="lexical", family=family),)
    draft = PublicationPlan(
        job_id=job_id,
        snapshot_id=snapshot_id_v2(bundle.workspace.workspace_id, base, job_id, config_sha, families, (*reused, page.page_id)),
        workspace_id=bundle.workspace.workspace_id,
        base_snapshot_id=base,
        config_sha=config_sha,
        families=families,
        member_count=member_count_after(view.member_count if view else 0, base_page, page),
        new_pages=(page,),
        reused_page_ids=reused,
        new_batches=(batch,),
        manifest=manifest,
    )
    root = artifacts.put_stream(BytesIO(canonical_bytes(snapshot_root_document(draft, manifests))), "application/json")
    return draft.model_copy(update={"manifest": root})
```

`PublishedCorpus.plan` in `tests/indexing/test_publication.py` becomes: commit the bundle, accept and start the job (unchanged), then `return lexical_plan(self.store, reader(), bundle, job.id, self.config.sha256, base, text)`. Remove the `publication_manifest`, `PublicationBatch`, `PublicationSlot` imports from the test modules; `tests/retrieval/test_eligibility.py` and `tests/operations/test_restore.py` build their snapshots with `lexical_plan` and `store.publish_snapshot(...)`, and their `RetainedSlot`/`RetainedBatch` fixtures (renamed from `PublicationSlot`/`PublicationBatch`) are compared against `store.snapshot_slots(view)`.

For the slot-reading tests (`rg -n "\.slots\b" tests`), replace `snapshot.slots` / `view.slots` / `refreshed.slots` with `store.snapshot_slots(snapshot)` (respectively the store in scope: `kb._workspace.owner.store`, `f.store`, `owner.store`), and `plan.slots` in `tests/indexing/test_sources.py:465` with `plan.families`. `tests/evaluation/test_temporal_scenarios.py:88` compares `current.families == second_snapshot.families`. `tests/api/test_configured_runtime.py:112` checks `any(item.slot == "graph" and item.required for item in view.families)`.

- [ ] **Step 13: Run the focused suites**

Run: `uv run --locked pytest tests/indexing tests/storage tests/contracts tests/retrieval tests/operations tests/jobs -v`
Expected: PASS, including the six new tests of `tests/indexing/test_page_publication.py`.

- [ ] **Step 14: Run the full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS. Typical leftovers: an unconverted `.slots` access (fix with `snapshot_slots`), or a test that asserted the old `"snapshot"`/`"1"` manifest schema (update to `"2"`).

- [ ] **Step 15: Commit**

```bash
git add src/tychora tests
git commit -m "feat: publish snapshots as membership pages and verify only new batches"
```

---
### Task 10: Snapshot retention and garbage collection

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (drop the foreign keys from `snapshots.parent_snapshot_id`, `jobs.base_snapshot_id`, `jobs.result_snapshot_id`, `query_runs.snapshot_id` to `snapshots`), `src/tychora/adapters/sqlite/migrations.py`
- Modify: `src/tychora/ports/repositories.py` (`GcReport.unpublished_skipped`, `BatchRepository.receipt`, `BatchRepository.record_ids`)
- Modify: `src/tychora/adapters/sqlite/repositories/snapshots.py` (`retained_ids`, `collect`), `src/tychora/adapters/sqlite/repositories/batches.py` (`purge`, `receipt`, `record_ids`)
- Modify: `src/tychora/application/leases.py` (`LeaseSet.pinned`)
- Create: `src/tychora/application/snapshot_gc.py`
- Modify: `src/tychora/application/sources.py:841-863` (`_publish` runs the bounded step)
- Modify: `src/tychora/adapters/sqlite/publication.py` (`PublicationReceipts.lookup` tolerates a collected snapshot)
- Modify: `src/tychora/contracts/operations.py` (`GcMaintenance`, `GcStatus`, `MaintenanceResult.gc`), `src/tychora/application/maintenance.py`, `src/tychora/interfaces/commands/manage.py`
- Test: `tests/operations/test_snapshot_gc.py` (new), `tests/cli/test_commands.py` (append)

**Interfaces:**
- Consumes: Task 7 and Task 9 repositories; `RemovalService.reconcile_pending_deletions(candidates: frozenset[str] | None) -> dict[str, int]` (`application/removal.py:506`).
- Produces:

```python
class SnapshotRepository(Protocol):
    def retained_ids(self, keep_last: int = 16) -> frozenset[str]: ...
    def collect(self, limit: int = 64, *, leased: frozenset[str] = frozenset(),
                include_unpublished: bool = False) -> GcReport: ...
class BatchRepository(Protocol):
    def receipt(self, batch_id: str) -> LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt | None: ...
    def record_ids(self, batch_id: str) -> tuple[str, ...]: ...
    def purge(self, batch_ids: tuple[str, ...]) -> tuple[str, ...]: ...
class GcReport(Contract): snapshots; pages; orphan_batches: tuple[BatchRow, ...]; released_artifacts; unpublished_skipped: bool = False
# application/snapshot_gc.py
@dataclass(frozen=True)
class GcSummary: snapshots: int; pages: int; batches: int; deferred_batches: int; released_artifacts: int; unpublished_skipped: bool
class SnapshotGc:
    def __init__(self, owner: WorkspaceOwner) -> None: ...
    def collect(self, ctx: ExecutionContext, *, limit: int = 64, include_unpublished: bool = False) -> GcSummary: ...
# contracts/operations.py
class GcMaintenance(Contract): operation: Literal["gc"] = "gc"; limit: int (1..100_000, default 1024); include_unpublished: bool = True
class GcStatus(Contract): snapshots; pages; batches; deferred_batches; released_artifacts: NonNegativeInt; unpublished_skipped: bool
```

Retention (spec §4.2) plus one rule the spec omits: the base snapshot of a queued, running or cancelling job is retained, because that job's plan reuses the base's pages.

- [ ] **Step 1: Write the failing tests**

```python
# tests/operations/test_snapshot_gc.py
"""Unretained snapshots, orphan pages and orphan batches are collected."""

import pytest

from tests.api.test_public_sync import CharacterCounter
from tychora import SourceInput, Tychora


def _add(kb, key: str, text: str):
    job = kb.sources.add(
        SourceInput(
            type="text",
            source_key=key,
            collection_id=kb.default_collection_id,
            kind="manual@1",
            format="text",
            metadata={"title": key, "publisher": "Example Team", "edition": "1"},
            text=text,
        )
    )
    return kb.jobs.wait(job.id)


def _count(db, sql):
    return db.execute(sql).fetchone()[0]


def test_post_publication_step_keeps_the_last_sixteen(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        db = kb._workspace.owner.db
        for index in range(20):
            assert _add(kb, "same.md", f"Version {index} of the verification outcome.").state == "succeeded"
        assert _count(db, "SELECT COUNT(*) FROM snapshots") == 16
        assert _count(
            db,
            "SELECT COUNT(*) FROM membership_pages WHERE page_id NOT IN (SELECT page_id FROM snapshot_pages)",
        ) == 0
        assert _count(
            db,
            "SELECT COUNT(*) FROM index_batches b WHERE NOT EXISTS "
            "(SELECT 1 FROM snapshot_batches s WHERE s.batch_id=b.batch_id)",
        ) == 0
        assert _count(db, "SELECT COUNT(*) FROM lexical_records") == _count(
            db,
            "SELECT COUNT(*) FROM lexical_records l WHERE l.batch_id IN (SELECT batch_id FROM snapshot_batches)",
        )


def test_leased_snapshot_is_retained(tmp_path):
    from tychora.application.snapshot_gc import SnapshotGc
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "First verification outcome.")
        first = owner.store.active_snapshot_id()
        for index in range(17):
            with owner.store.pin_snapshot(first):
                _add(kb, "a.md", f"Later outcome {index}.")
        with owner.store.pin_snapshot(first):
            SnapshotGc(owner).collect(ExecutionContext(), limit=1000)
            assert owner.db.execute(
                "SELECT 1 FROM snapshots WHERE snapshot_id=?", (first,)
            ).fetchone()


def test_crash_between_batch_write_and_activation_is_collected(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Published verification outcome.")
        active = owner.store.active_snapshot_id()

        def crash(plan):
            raise RuntimeError("simulated crash before activation")

        monkeypatch.setattr(owner.store, "publish_snapshot", crash)
        assert _add(kb, "b.md", "Unpublished verification outcome.").state != "succeeded"
        monkeypatch.undo()
        assert owner.store.active_snapshot_id() == active
        orphan_sql = (
            "SELECT COUNT(*) FROM index_batches b WHERE NOT EXISTS "
            "(SELECT 1 FROM snapshot_batches s WHERE s.batch_id=b.batch_id)"
        )
        assert _count(owner.db, orphan_sql) > 0
        result = kb.management.run({"operation": "gc"})
        assert result.gc is not None and result.gc.batches > 0
        assert _count(owner.db, orphan_sql) == 0
        assert _add(kb, "b.md", "Unpublished verification outcome.").state == "succeeded"
        result = kb.search({"question": "unpublished", "modalities": ["lexical"]})
        assert result.segments
```

Append to `tests/cli/test_commands.py`:

```python
def test_manage_gc_reports_collection(tmp_path):
    result = CliRunner().invoke(
        app, ["--workspace", str(tmp_path / "workspace"), "manage", "gc", "--json"]
    )
    assert result.exit_code == 0, result.stderr
    assert '"gc"' in result.stdout
```

`CliRunner` and `app` are already imported at the top of `tests/cli/test_commands.py` (`from typer.testing import CliRunner`, `from tychora.interfaces.cli import app`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/operations/test_snapshot_gc.py tests/cli/test_commands.py::test_manage_gc_reports_collection -v`
Expected: FAIL (`assert 20 == 16`, `ModuleNotFoundError: No module named 'tychora.application.snapshot_gc'`, unknown maintenance operation `gc`).

- [ ] **Step 3: Drop the snapshot foreign keys that block collection**

In `0001.sql`: `snapshots.parent_snapshot_id TEXT` (no `REFERENCES`), `jobs.base_snapshot_id TEXT`, `jobs.result_snapshot_id TEXT`, `query_runs.snapshot_id TEXT NOT NULL`. `workspaces.active_snapshot_id` and `evaluation_runs.snapshot_id` keep their foreign keys (both are always retained). Recompute `SCHEMA_CHECKSUM`.

- [ ] **Step 4: Retention and collection in the repository**

```python
    def retained_ids(self, keep_last: int = 16) -> frozenset[str]:
        now = datetime.now(UTC).isoformat()
        rows = self.db.execute(
            "SELECT active_snapshot_id FROM workspaces WHERE active_snapshot_id IS NOT NULL "
            "UNION SELECT snapshot_id FROM (SELECT snapshot_id FROM snapshots "
            "ORDER BY published_at DESC,snapshot_id DESC LIMIT ?) "
            "UNION SELECT snapshot_id FROM query_runs WHERE expires_at>? "
            "UNION SELECT snapshot_id FROM evaluation_runs "
            "UNION SELECT base_snapshot_id FROM jobs WHERE base_snapshot_id IS NOT NULL "
            "AND state IN ('queued','running','cancelling')",
            (keep_last, now),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def collect(
        self, limit: int = 64, *, leased: frozenset[str] = frozenset(), include_unpublished: bool = False
    ) -> GcReport:
        """Delete unretained snapshots, then orphan pages; mark orphan batches removed."""
        if not self.db.in_transaction:
            raise _integrity("Snapshot collection requires a write transaction")
        keep = json.dumps(sorted(self.retained_ids() | leased))
        doomed = [
            row[0]
            for row in self.db.execute(
                "SELECT snapshot_id FROM snapshots WHERE snapshot_id NOT IN (SELECT value FROM json_each(?)) "
                "ORDER BY published_at,snapshot_id LIMIT ?",
                (keep, limit),
            )
        ]
        released, candidates = self._delete_snapshots(doomed)
        pages, page_manifests, page_batches = self._delete_orphan_pages()
        candidates |= page_batches
        skipped = include_unpublished and self._jobs_running()
        orphan_ids = self._orphan_batches(candidates, include_unpublished and not skipped)
        self.db.execute(
            "UPDATE index_batches SET state='removed' WHERE batch_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(orphan_ids)),),
        )
        batches = SqliteBatchRepository(self.db)
        return GcReport(
            snapshots=tuple(doomed),
            pages=tuple(pages),
            orphan_batches=tuple(row for b in sorted(orphan_ids) if (row := batches.get(b)) is not None),
            released_artifacts=tuple(self._release(released | page_manifests)),
            unpublished_skipped=skipped,
        )

    def _delete_snapshots(self, doomed: list[str]) -> tuple[set[str], set[str]]:
        ids = json.dumps(doomed)
        in_ids = "IN (SELECT value FROM json_each(?))"
        manifests = {r[0] for r in self.db.execute(f"SELECT manifest_sha FROM snapshots WHERE snapshot_id {in_ids}", (ids,))}
        roots = {r[0] for r in self.db.execute(f"SELECT batch_id FROM snapshot_root_batches WHERE snapshot_id {in_ids}", (ids,))}
        for table in ("snapshot_pages", "snapshot_indexes", "snapshot_root_batches", "snapshots"):
            self.db.execute(f"DELETE FROM {table} WHERE snapshot_id {in_ids}", (ids,))
        return manifests, roots

    def _delete_orphan_pages(self) -> tuple[list[str], set[str], set[str]]:
        rows = self.db.execute(
            "SELECT page_id,manifest_sha FROM membership_pages p WHERE NOT EXISTS "
            "(SELECT 1 FROM snapshot_pages s WHERE s.page_id=p.page_id)"
        ).fetchall()
        ids = json.dumps([row[0] for row in rows])
        in_ids = "IN (SELECT value FROM json_each(?))"
        batches = {r[0] for r in self.db.execute(f"SELECT batch_id FROM page_batches WHERE page_id {in_ids}", (ids,))}
        for table in ("page_batches", "page_members", "membership_pages"):
            self.db.execute(f"DELETE FROM {table} WHERE page_id {in_ids}", (ids,))
        return [row[0] for row in rows], {row[1] for row in rows}, batches

    def _jobs_running(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM jobs WHERE state IN ('queued','running','cancelling') LIMIT 1"
        ).fetchone() is not None

    def _orphan_batches(self, candidates: set[str], include_unpublished: bool) -> set[str]:
        unbound = (
            "NOT EXISTS (SELECT 1 FROM page_batches p WHERE p.batch_id=b.batch_id) "
            "AND NOT EXISTS (SELECT 1 FROM snapshot_root_batches r WHERE r.batch_id=b.batch_id)"
        )
        found = {
            r[0]
            for r in self.db.execute(
                f"SELECT batch_id FROM index_batches b WHERE {unbound} AND state='ready' "
                "AND batch_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(candidates)),),
            )
        }
        if include_unpublished:
            found |= {
                r[0]
                for r in self.db.execute(
                    f"SELECT batch_id FROM index_batches b WHERE {unbound} "
                    "AND state IN ('building','ready','failed','removed')"
                )
            }
        return found

    def _release(self, shas: set[str]) -> list[str]:
        """Mark retention documents no longer referenced as pending deletion."""
        released = []
        for sha in sorted(shas):
            if self.db.execute(
                "SELECT 1 FROM snapshots WHERE manifest_sha=? UNION ALL "
                "SELECT 1 FROM membership_pages WHERE manifest_sha=? LIMIT 1",
                (sha, sha),
            ).fetchone():
                continue
            self.db.execute(
                "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=? AND payload_state='present'",
                (sha,),
            )
            released.append(sha)
        return released
```

`table` in the f-strings comes only from the literal tuples above, never from input.

In `SqliteBatchRepository`:

```python
    def receipt(self, batch_id: str):
        row = self.db.execute(
            "SELECT f.channel,b.receipt_json FROM index_batches b JOIN index_families f USING(family_id) "
            "WHERE b.batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None or row[1] is None:
            return None
        kind = {"lexical": LexicalBatchReceipt, "graph": GraphBatchReceipt}.get(row[0], BatchReceipt)
        return kind.model_validate_json(row[1])

    def record_ids(self, batch_id: str) -> tuple[str, ...]:
        return tuple(
            r[0] for r in self.db.execute(
                "SELECT record_id FROM vector_records WHERE batch_id=? ORDER BY record_id", (batch_id,)
            )
        )

    def purge(self, batch_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Delete removed batches and their rows; return artifacts released for deletion."""
        ids = json.dumps(sorted(batch_ids))
        in_ids = "IN (SELECT value FROM json_each(?))"
        shas = {
            sha
            for row in self.db.execute(
                f"SELECT input_manifest_sha,vectors_sha FROM index_batches WHERE state='removed' AND batch_id {in_ids}",
                (ids,),
            )
            for sha in row
            if sha is not None
        }
        lexical = self.db.execute(f"DELETE FROM lexical_records WHERE batch_id {in_ids}", (ids,)).rowcount
        self.db.execute(f"DELETE FROM vector_records WHERE batch_id {in_ids}", (ids,))
        self.db.execute(f"DELETE FROM index_batches WHERE state='removed' AND batch_id {in_ids}", (ids,))
        if lexical:
            SqliteWorkspaceRepository(self.db).bump_lexical_epoch()
        released = []
        for sha in sorted(shas):
            if self.db.execute(
                "SELECT 1 FROM index_batches WHERE input_manifest_sha=? OR vectors_sha=? LIMIT 1", (sha, sha)
            ).fetchone():
                continue
            self.db.execute(
                "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=? AND payload_state='present'",
                (sha,),
            )
            released.append(sha)
        return tuple(released)
```

(`SqliteWorkspaceRepository` is phase 1's workspace repository class in `adapters/sqlite/repositories/workspace.py`.)

- [ ] **Step 5: Orchestrate native drops and payload release**

Add to `src/tychora/application/leases.py`:

```python
    def pinned(self) -> frozenset[str]:
        with self._lock:
            return frozenset(key for key, count in self._counts.items() if count > 0)
```

```python
# src/tychora/application/snapshot_gc.py
"""Collect unretained snapshots: SQL rows, native records and released payloads."""

from dataclasses import dataclass

from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.graph import GraphBatchReceipt
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import BatchRow

from .owner import WorkspaceOwner
from .removal import RemovalService


@dataclass(frozen=True)
class GcSummary:
    snapshots: int
    pages: int
    batches: int
    deferred_batches: int
    released_artifacts: int
    unpublished_skipped: bool


class SnapshotGc:
    def __init__(self, owner: WorkspaceOwner) -> None:
        self.owner = owner

    def collect(
        self, ctx: ExecutionContext, *, limit: int = 64, include_unpublished: bool = False
    ) -> GcSummary:
        uow = SqliteUnitOfWork(self.owner.db)
        with uow.write() as tx:
            report = tx.snapshots.collect(
                limit, leased=self.owner.store.leases.pinned(), include_unpublished=include_unpublished
            )
        dropped, deferred = self._drop_native(report.orphan_batches, ctx)
        with uow.write() as tx:
            released = tx.batches.purge(tuple(dropped))
        jobs = JobRepository(self.owner.db, artifacts=self.owner.artifacts, workspace_id=self.owner.workspace_id)
        candidates = frozenset((*report.released_artifacts, *released))
        if candidates:
            RemovalService(self.owner, jobs).reconcile_pending_deletions(candidates)
        return GcSummary(
            snapshots=len(report.snapshots),
            pages=len(report.pages),
            batches=len(dropped),
            deferred_batches=len(deferred),
            released_artifacts=len(candidates),
            unpublished_skipped=report.unpublished_skipped,
        )

    def _drop_native(self, rows: tuple[BatchRow, ...], ctx: ExecutionContext) -> tuple[list[str], list[str]]:
        """Remove native records first; a batch whose native store is not open waits."""
        store = self.owner.store
        dropped, deferred = [], []
        with SqliteUnitOfWork(self.owner.db).read() as tx:
            plans = [(row, tx.batches.record_ids(row.batch_id), tx.batches.receipt(row.batch_id)) for row in rows]
        for row, record_ids, receipt in plans:
            ctx.checkpoint()
            if row.slot in {"text", "image"}:
                index = store.vector_indexes.get(row.family_id)
                if index is None and record_ids:
                    deferred.append(row.batch_id)
                    continue
                if record_ids:
                    index.drop_records(record_ids, ctx)
            elif row.slot == "graph" and isinstance(receipt, GraphBatchReceipt):
                graph = store.graph_indexes.get(receipt.generation_id)
                if graph is None:
                    deferred.append(row.batch_id)
                    continue
                graph.drop_generation(receipt.generation_id, ctx)
            dropped.append(row.batch_id)
        return dropped, deferred
```

A deferred batch stays `removed` with its rows; `manage gc` (which passes `include_unpublished=True`) selects `removed` rows again and retries them once the owner has attached that family's index.

- [ ] **Step 6: Run the bounded step after each publication**

In `SourceService._publish` (`application/sources.py:841-863`), after `jobs.complete(...)`:

```python
            await self.owner.run_blocking(lambda: self._collect_after_publication(ctx), ctx)
```

```python
    def _collect_after_publication(self, ctx: ExecutionContext) -> None:
        try:
            SnapshotGc(self.owner).collect(ctx, limit=64)
        except TychoraError:
            # Collection is retried by the next publication and by `manage gc`;
            # it never changes the outcome of a succeeded job.
            return
```

In `PublicationReceipts.lookup`, change the binding check to `if row[1] is not None and row[1] != job_id:` (a collected snapshot no longer has a row; the job's `result_snapshot_id` remains authoritative).

- [ ] **Step 7: `manage gc`**

`src/tychora/contracts/operations.py`:

```python
class GcMaintenance(Contract):
    operation: Literal["gc"] = "gc"
    limit: Annotated[int, Field(ge=1, le=100_000)] = 1024
    include_unpublished: bool = True


class GcStatus(Contract):
    snapshots: NonNegativeInt
    pages: NonNegativeInt
    batches: NonNegativeInt
    deferred_batches: NonNegativeInt
    released_artifacts: NonNegativeInt
    unpublished_skipped: bool
```

Add `GcMaintenance` to `MaintenanceVariant` and `gc: GcStatus | None = None` to `MaintenanceResult`. In `MaintenanceService.run` (`application/maintenance.py`), next to the health branch:

```python
        if isinstance(operation, GcMaintenance):
            collections = await self.owner.run_blocking(self._all_collections, ctx)
            if not collections <= grant.collection_ids:
                raise TychoraError("FORBIDDEN_SCOPE", "Garbage collection requires all collection grants")
            async with self.owner.comparison_admission.mutation(ctx):
                summary = await self.owner.run_blocking(
                    lambda: SnapshotGc(self.owner).collect(
                        ctx, limit=operation.limit, include_unpublished=operation.include_unpublished
                    ),
                    ctx,
                )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="gc",
                    target_ids=(self.owner.workspace_id,),
                    actions=("delete unretained snapshots, orphan pages and orphan batches",),
                ),
                gc=GcStatus(**summary.__dict__),
            )
```

with `_all_collections` returning `frozenset(row[0] for row in self.owner.db.execute("SELECT collection_id FROM collections"))` (the same check `_prune_native_on_owner` performs). `gc` requires the `manage` capability through the existing `required` computation (it is not a read-only health call).

`src/tychora/interfaces/commands/manage.py`:

```python
@group.command()
def gc(
    ctx: typer.Context,
    limit: int = typer.Option(1024, min=1, max=100_000, help="Most snapshots deleted in one run."),
    published_only: bool = typer.Option(False, "--published-only", help="Keep unpublished batches."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Delete unretained snapshots, orphan pages and orphan batches."""
    _run(
        ctx,
        {"operation": "gc", "limit": limit, "include_unpublished": not published_only},
        json_output=json_output,
    )
```

Document the command in `docs/cli.md` (one row in the `manage` table: "`manage gc [--limit N] [--published-only]` — delete snapshots outside the retention rule and the pages, batches and payloads only they used") and the retention rule in `docs/operations.md`.

- [ ] **Step 8: Run the tests**

Run: `uv run --locked pytest tests/operations tests/cli tests/indexing tests/jobs -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/tychora tests docs/cli.md docs/operations.md
git commit -m "feat: collect unretained snapshots, orphan pages and orphan batches"
```

---

### Task 11: Whole-corpus verification moves to `manage health`

**Files:**
- Modify: `src/tychora/application/recovery.py:805-900` (`_verify_native_on_owner`), `:330-380` (`verify_native_vector`)
- Test: `tests/indexing/test_publication.py` (append)

**Interfaces:**
- Consumes: `CorpusStore.snapshot_slots(view)`, `vector_batch_records`, `decode_vector_batch`.
- Produces: `RecoveryStatus.native_verified_records` and `native_verified_generations` now count every batch of the active snapshot; the 128-batch ceiling is removed, the job remains cancellable through `ctx.checkpoint()` per batch.

- [ ] **Step 1: Write the failing test**

```python
def test_native_health_covers_more_than_128_batches(projected_corpus):
    from tychora.application.recovery import RecoveryService

    f = projected_corpus
    for index in range(130):
        f.add_revision(f"health outcome {index}", source_key=f"h{index}.md")
    owner = _recovery_owner(f)  # same SimpleNamespace owner as the native GC test builds
    report = RecoveryService(owner)._verify_native_on_owner(ExecutionContext())
    assert report[1] >= 130
```

Factor the `SimpleNamespace(...)` construction in `test_native_gc_prunes_only_unprotected_vector_batch_then_rebuilds` (lines 791-803) into a module-level `_recovery_owner(f)` helper and use it in both tests.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/indexing/test_publication.py::test_native_health_covers_more_than_128_batches -v`
Expected: FAIL with `TychoraError: Native health exceeds 128 batch budget`.

- [ ] **Step 3: Verify per batch without a ceiling**

In `_verify_native_on_owner`, iterate `store.snapshot_slots(snapshot)` instead of `snapshot.slots`, delete the `batches > 128` branch, and call `ctx.checkpoint()` once per batch (already present). For each vector batch, the existing `verify_native_vector` path now reads the batch artifact once (`vector_batch_records`), so `--verify-payloads` hashes every vector exactly once per health run and the add path never does.

- [ ] **Step 4: Run the tests**

Run: `uv run --locked pytest tests/indexing/test_publication.py tests/jobs/test_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tychora/application/recovery.py tests/indexing/test_publication.py
git commit -m "feat: verify every active batch in manage health instead of on each add"
```

---

### Task 12: Export and import carry pages and batch artifacts

**Files:**
- Test: `tests/operations/test_backup.py` (append), `tests/operations/test_restore.py` (append)
- Modify (only if the tests expose a gap): `src/tychora/adapters/files/references.py`, `src/tychora/application/restore.py`

**Interfaces:**
- Consumes: `from_backup` roots of Task 9 (`membership_page` manifests, `vectors_sha` direct payloads).

- [ ] **Step 1: Write the tests**

Append to `tests/operations/test_backup.py`:

```python
def test_backup_inventory_includes_pages_roots_and_vector_batches(tmp_path):
    from tychora.adapters.files.references import from_backup

    from tests.api.test_public_sync import CharacterCounter
    from tychora import SourceInput, Tychora

    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        for index in range(3):
            job = kb.sources.add(
                SourceInput(
                    type="text", source_key=f"m{index}.md", collection_id=kb.default_collection_id,
                    kind="manual@1", format="text",
                    metadata={"title": "T", "publisher": "P", "edition": "1"},
                    text=f"Backup verification outcome {index}.",
                )
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        inventory = from_backup(owner.db, artifacts=owner.artifacts)
        pages = {r[0] for r in owner.db.execute("SELECT manifest_sha FROM membership_pages")}
        roots = {r[0] for r in owner.db.execute("SELECT manifest_sha FROM snapshots")}
        assert pages and roots
        assert pages | roots <= inventory.required_shas
```

Append to `tests/operations/test_restore.py`:

```python
def test_export_import_round_trip_keeps_pages_and_search(tmp_path):
    from tests.api.test_public_sync import CharacterCounter
    from tychora import SourceInput, Tychora

    source = tmp_path / "source"
    with Tychora.open(source, token_counter=CharacterCounter()) as kb:
        job = kb.sources.add(
            SourceInput(
                type="text", source_key="m.md", collection_id=kb.default_collection_id,
                kind="manual@1", format="text",
                metadata={"title": "T", "publisher": "P", "edition": "1"},
                text="Restored verification outcome.",
            )
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        kb.management.run({"operation": "export", "destination": str(tmp_path / "archive")})
        pages = kb._workspace.owner.db.execute("SELECT COUNT(*) FROM snapshot_pages").fetchone()[0]
    with Tychora.restore(tmp_path / "archive", tmp_path / "restored") as restored:
        db = restored._workspace.owner.db
        assert db.execute("SELECT COUNT(*) FROM snapshot_pages").fetchone()[0] == pages
        assert restored.search({"question": "restored", "modalities": ["lexical"]}).segments
```

If `Tychora.restore` needs an explicit token counter for search in this test module, follow the pattern of the existing restore tests in the same file (they open the restored workspace with `Tychora.open(destination, token_counter=...)` after `RestoreService.import_backup`).

- [ ] **Step 2: Run the tests**

Run: `uv run --locked pytest tests/operations/test_backup.py tests/operations/test_restore.py -v`
Expected: PASS. If `test_backup_inventory_includes_pages_roots_and_vector_batches` fails, the Task 9 `from_backup` roots are incomplete: add the missing root to the `add_manifest` list in `adapters/files/references.py` and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/operations src/tychora/adapters/files/references.py src/tychora/application/restore.py
git commit -m "test: round-trip membership pages and vector batches through export and import"
```

---

### Task 13: Publication footprint and the E3 gate

**Files:**
- Modify: `src/tychora/adapters/sqlite/repositories/snapshots.py` (`footprint`)
- Create: `src/tychora/evaluation/bench/footprint.py`
- Modify: the module that implements `--suite scale` in phase 1 (`src/tychora/evaluation/bench/harness.py`, `run_scale`)
- Test: `tests/evaluation/test_bench_footprint.py` (new)

**Interfaces:**
- Consumes (phase 1): the scale suite's per-add loop and result writer (`bench-results/<commit>/scale-offline.json`).
- Produces: `SnapshotRepository.footprint(snapshot_id) -> PublicationFootprint`; scale result keys `publication_bytes_per_add: {"100": n, "1000": n, "10000": n}`, `largest_snapshot_artifact_bytes`, `publication_p90_ms_at_10000`, and verdicts `E3.bytes_ratio`, `E3.artifact_ceiling`, `E3.publication_p90`.

- [ ] **Step 1: Write the failing test**

```python
# tests/evaluation/test_bench_footprint.py
"""Publication writes one page and one root, whose sizes are bounded."""

from tests.api.test_public_sync import CharacterCounter
from tychora import SourceInput, Tychora
from tychora.evaluation.bench.footprint import publication_footprint


def test_footprint_counts_root_and_new_page(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        for index in range(3):
            job = kb.sources.add(
                SourceInput(
                    type="text", source_key=f"f{index}.md", collection_id=kb.default_collection_id,
                    kind="manual@1", format="text",
                    metadata={"title": "T", "publisher": "P", "edition": "1"},
                    text=f"Footprint outcome {index}.",
                )
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        footprint = publication_footprint(owner.db, owner.store.active_snapshot_id())
    assert footprint.new_pages == 1
    assert 0 < footprint.root_bytes < 1024 * 1024
    assert 0 < footprint.new_page_bytes < 1024 * 1024
    assert footprint.largest_artifact_bytes == max(footprint.root_bytes, footprint.new_page_bytes)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/evaluation/test_bench_footprint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.evaluation.bench.footprint'`.

- [ ] **Step 3: Implement**

In `SqliteSnapshotRepository`:

```python
    def footprint(self, snapshot_id: str) -> PublicationFootprint:
        root = self.db.execute(
            "SELECT a.size_bytes,s.parent_snapshot_id FROM snapshots s "
            "JOIN artifacts a ON a.artifact_sha=s.manifest_sha WHERE s.snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if root is None:
            raise TychoraError("NOT_FOUND", "Snapshot is unavailable")
        pages = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(a.size_bytes),0),COALESCE(MAX(a.size_bytes),0) "
            "FROM snapshot_pages sp JOIN membership_pages p ON p.page_id=sp.page_id "
            "JOIN artifacts a ON a.artifact_sha=p.manifest_sha WHERE sp.snapshot_id=? "
            "AND NOT EXISTS (SELECT 1 FROM snapshot_pages b WHERE b.snapshot_id IS ? AND b.page_id=sp.page_id)",
            (snapshot_id, root[1]),
        ).fetchone()
        return PublicationFootprint(
            snapshot_id=snapshot_id,
            root_bytes=root[0],
            new_page_bytes=pages[1],
            new_pages=pages[0],
            largest_artifact_bytes=max(root[0], pages[2]),
        )
```

```python
# src/tychora/evaluation/bench/footprint.py
"""Bytes one publication writes: its root document and its new pages."""

import sqlite3

from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.ports.repositories import PublicationFootprint


def publication_footprint(db: sqlite3.Connection, snapshot_id: str) -> PublicationFootprint:
    with SqliteUnitOfWork(db).read() as tx:
        return tx.snapshots.footprint(snapshot_id)
```

In the scale suite, after each add whose member count is 100, 1,000 or 10,000, record `publication_footprint(...)` (root plus new page bytes) and the publication stage duration (the job's `publish` stage from its events, which `StageSource.ingest_stages` already returns as phase 1 defines). Write the keys listed under Interfaces and these verdicts:

```python
verdicts = {
    "E3.bytes_ratio": bytes_at[10_000] <= 2 * bytes_at[1_000],
    "E3.artifact_ceiling": largest_artifact <= 1024 * 1024,
    "E3.publication_p90": publication_p90_ms_at_10000 <= 500,
}
```

The ratio is measured against 1,000 members, not 100: below about 1,500 documents the number of occupied buckets still grows, so the root grows from about 80 to 256 page references; from 1,000 on, the root is constant (about 60 KB) and only the bucket page grows (about 4 members at 1,000, 40 at 10,000). The 100-member value is recorded for reference. See "Spec gaps".

- [ ] **Step 4: Run the test**

Run: `uv run --locked pytest tests/evaluation/test_bench_footprint.py -v`
Expected: PASS.

- [ ] **Step 5: Run the E3 gate**

Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite scale --profile offline`
Expected: exit code 0; `bench-results/<commit>/scale-offline.json` contains `"E3.bytes_ratio": true`, `"E3.artifact_ceiling": true`, `"E3.publication_p90": true`, and 10,000 members published without a size error. Commit the result file only if the phase-1 harness commits results for gates (the index's phase-1 gate does; follow the same convention).

- [ ] **Step 6: Commit**

```bash
git add src/tychora/adapters/sqlite/repositories/snapshots.py src/tychora/evaluation/bench tests/evaluation/test_bench_footprint.py
git commit -m "feat: measure publication bytes per add and gate E3 on the scale suite"
```

---

### Task 14: Ratchet and phase verification

**Files:**
- Modify: `tests/architecture/test_sql_free_modules.py` (`SQL_FREE_MODULES`; see below)
- Modify: `quality/baseline.json` (via the checker)

- [ ] **Step 1: Confirm the deletions the spec's §7 assigns to this phase**

Run: `rg -n "_manifest_document|publication_manifest|insert_snapshot_bindings|embedding_cache|pin_payloads\(payloads\)|float32-le" src/tychora`
Expected: no match in `adapters/sqlite/publication.py`, `adapters/sqlite/corpus.py`, `indexing/coordinator.py`. (`pin_payloads` may remain in `application/inspection.py`, which pins originals, not snapshot payloads.)

- [ ] **Step 2: SQL-free modules**

The index lists `indexing/coordinator`, `application/removal` and `application/restore` as joining `SQL_FREE_MODULES` in this phase. This plan moves every snapshot, page, batch and family statement of the coordinator into repositories, but the coordinator keeps the extraction-reuse checks (`_commit_or_reuse`), visual input reads (`_visual_inputs`), edition lookups and the transitional graph membership reads, and `removal` and `restore` keep their payload-retention SQL. Moving those needs `RepresentationRepository` (phase 3) and a retention repository nobody has designed yet. Do not add the three modules here; record the per-file counts instead:

Run: `uv run --locked python -m tools.health report`
Expected: G8 (raw SQL outside adapters) lower than the phase-start baseline for `indexing/coordinator.py`; G24 lower for `adapters/sqlite/publication.py`.

- [ ] **Step 3: Full suite and ratchet**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

Run: `uv run --locked python -m tools.health check`
Expected: PASS, or "stale baseline" listing improved metrics. In the latter case:

Run: `uv run --locked python -m tools.health update`
Expected: `quality/baseline.json` rewritten with only improved values.

- [ ] **Step 4: Commit**

```bash
git add quality/baseline.json tests/architecture/test_sql_free_modules.py
git commit -m "chore: lock code-health improvements from snapshot pages"
```

---

## Spec gaps

1. **E3 baseline point.** E3 asks bytes per add at 10,000 members within 2× of 100 members. With 256 buckets the root grows until most buckets are occupied (about 1,500 documents) and the bucket page grows linearly with N/256, so 10,000 versus 100 is about 4–5×. This plan gates 10,000 against 1,000 (about 1.7×) and records the 100-member value. The spec should restate E3 that way.
2. **Corpus-wide graph generation before contributions.** As the index records: `snapshot_root_batches` binds it until phase 6, and its input manifest now carries the generation manifest (corpus-sized, as the old snapshot manifest was).
3. **Family lookup key.** The spec adds `creation_nonce` but finding the existing family for a configuration needs the digest bound into `family_id`; `index_families.identity_sha` is added for that.
4. **Retention of a running job's base.** Not in §4.2's list, but required: a running job's plan reuses its base's pages.
5. **Foreign keys to `snapshots`.** GC must delete snapshots that jobs, expired query runs and child snapshots still name; those foreign keys are dropped (the columns stay).
6. **Withdrawn sources in other buckets.** With page reuse, a withdrawn source's member stays in its bucket page until that page is next rewritten (eligibility already excludes it). The spec should say whether `manage remove` should rewrite the page immediately.
7. **Plan checkpoint.** The old checkpoint was the snapshot manifest, which contained the plan. Format 2 adds a `publication_plan` document (schema "2") for the job checkpoint.

## Self-Review

**Spec coverage.** §4.2 pages, identity, views, retention, GC: Tasks 1, 7, 9, 10. §4.3 publication verifying new batches only, `count_ready` via `json_each`, set-based ownership, lexical rows at batch write with `building → ready`, one epoch bump per batch, `retained_sha`, registry written once, `_lane`/`_role`: Tasks 3, 7, 9. Whole-corpus verification in health: Task 11. §4.4 batch artifact, `row_index`, `vectors_sha`, native rebuild without providers, `vectors.cache_absent`, nonces, `vector_record_id`, Chroma metadata: Tasks 2, 4, 5, 6. §4.11 `native`: Task 5. §5 archives: Task 12. §6 rows: page/snapshot identity and determinism (Task 1), view equivalence and reused-batch spy (Task 9), crash collected by GC (Task 10), scale E3 (Task 13). Q2's manifest half: `test_resolve_snapshot_never_reads_the_root_manifest` (Task 9); the three `resolve_snapshot` calls per query remain for phase 4.

**Placeholder scan.** No TBD or "similar to Task N". Test helpers are pinned to the real fixtures (`tests/storage/conftest.py` `vector_family.batch`, `CliRunner().invoke(app, ...)` in `tests/cli/test_commands.py`).

**Type consistency.** `PageBatch`, `PageMember`, `MembershipPage`, `SlotFamily`, `NewBatch`, `BatchRow`, `PublicationPlan`, `SnapshotView` are used with the same fields across Tasks 1, 7, 8, 9, 10, 13. `snapshot_id_v2(..., root_batch_ids=...)` matches between contract, coordinator and test helper.

**Index changes needed** (report to the index owner): `FamilyRepository.nonce`/`create` take `identity_sha`; `BatchRepository` adds `write_lexical`, `write_vectors`, `find`, `receipt`, `record_ids`, `purge`; `SnapshotRepository` adds `page_manifests`, `page_by_id`, `footprint` and `collect(..., leased=, include_unpublished=)`; `PublicationPlan` adds `member_count` and `graph_watermark`; `snapshot_id_v2` adds keyword `root_batch_ids`; `ResolvedConfig.retained_sha` is a cached property, not a field; `IndexFamily` moves to `contracts/snapshots.py`; `RetainedSlot`/`RetainedBatch` and `CorpusStore.snapshot_slots` are new interim names; phase 1 must name its transaction class `SqliteTransaction` and its workspace repository `SqliteWorkspaceRepository`; the index's phase-2 ratchet row should drop `coordinator`, `removal`, `restore` from `SQL_FREE_MODULES`.
