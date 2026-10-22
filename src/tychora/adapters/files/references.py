"""Bounded traversal of explicit, versioned retention headers.

Task-specific data and provenance never create retention edges. Only the
workspace owner may decide which terminal capture originals became provenance.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tychora.adapters.sqlite.repositories import artifact_ref
from tychora.contracts.artifacts import (
    CAPTURE_TYPES,
    ArtifactRef,
    ManifestRef,
    physical_identity,
    registered_manifest,
    validate_manifest_schema,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
# Typed manifest roots whose closures keep artifacts live, as (query, kind).
TYPED_MANIFEST_ROOTS = (
    ("SELECT manifest_sha FROM snapshots", "snapshot"),
    ("SELECT manifest_sha FROM membership_pages", "membership_page"),
    ("SELECT input_manifest_sha FROM index_batches", "index_input"),
    ("SELECT effective_config_sha FROM jobs", "registry"),
    ("SELECT manifest_sha FROM evaluation_datasets", "evaluation"),
    (
        "SELECT retained_payload_sha FROM query_runs WHERE retained_payload_sha IS NOT NULL",
        "retained_run",
    ),
)
_CAPTURE_AND_EXTRACTION_ROOTS = (
    (
        "SELECT captured_inputs_sha FROM jobs WHERE captured_inputs_sha IS NOT NULL",
        "job_capture",
    ),
    ("SELECT document_artifact_sha FROM extractions", "extraction"),
    (
        "SELECT assertions_artifact_sha FROM extractions WHERE assertions_artifact_sha IS NOT NULL",
        "assertion",
    ),
)
MAX_ASSERTION_LINE_BYTES = 1024 * 1024
MAX_REFERENCES = 10000
_HEADER_FIELDS = frozenset(
    {
        "type",
        "schema_version",
        "required_artifacts",
        "required_manifests",
        "original_artifacts",
        "data",
        "provenance",
    }
)


def _fail(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message)


@dataclass(frozen=True)
class _References:
    artifacts: tuple[ArtifactRef, ...]
    manifests: tuple[ManifestRef, ...]
    type: str
    schema_version: str
    declared_count: int


def _array(value: JsonValue) -> list[JsonValue] | tuple[JsonValue, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REFERENCES:
        raise _fail("Invalid or oversized reference array")
    return value


def _artifact(value: JsonValue) -> ArtifactRef:
    ref = ArtifactRef.model_validate_json(canonical_bytes(value))
    if ref.storage_key != ref.sha256:
        raise ValueError("Artifact storage key must match its hash")
    return ref


def _parse(manifest: JsonValue, *, capture_published: bool) -> _References:
    try:
        return _parse_checked(manifest, capture_published=capture_published)
    except (ValueError, TypeError, RecursionError):
        raise _fail("Malformed required manifest or reference") from None


def _parse_checked(manifest: JsonValue, *, capture_published: bool) -> _References:
    if len(canonical_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise _fail("Required manifest exceeds byte limit")
    records = _array(manifest) if isinstance(manifest, (tuple, list)) else [manifest]
    if not records:
        raise _fail("Assertion artifact requires at least one record")
    artifacts: list[ArtifactRef] = []
    manifests: list[ManifestRef] = []
    record_ids: set[str] = set()
    assertion_ids: set[str] = set()
    kind, version = "", ""
    declared_count = 0
    declared_artifacts: dict[str, ArtifactRef] = {}
    declared_schemas: dict[str, tuple[str, str]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise _fail("Manifest must be a typed object")
        raw_kind, raw_version = record.get("type"), record.get("schema_version")
        validate_manifest_schema(raw_kind, raw_version)
        # validate_manifest_schema guarantees these narrowings; keep them explicit.
        if not isinstance(raw_kind, str):
            raise _fail("Manifest type must be a string")
        kind, version = raw_kind, str(raw_version)
        if isinstance(manifest, (list, tuple)) and kind != "assertion":
            raise _fail("Only assertion artifacts use record arrays")
        if kind == "assertion":
            if len(canonical_bytes(record)) + 1 > MAX_ASSERTION_LINE_BYTES:
                raise _fail("Assertion record exceeds line byte limit")
            rid, aid = record.get("record_id"), record.get("assertion_id")
            if (
                not isinstance(rid, str)
                or not rid
                or not isinstance(aid, str)
                or not aid
                or rid in record_ids
                or aid in assertion_ids
            ):
                raise _fail("Assertion record IDs must be unique and nonempty")
            record_ids.add(rid)
            assertion_ids.add(aid)
        elif set(record) - _HEADER_FIELDS:
            raise _fail("Unknown required manifest header field")
        if "original_artifacts" in record and kind not in CAPTURE_TYPES:
            raise _fail("Only capture manifests declare original artifacts")
        required = tuple(
            _artifact(value) for value in _array(record.get("required_artifacts", []))
        )
        linked = tuple(
            ManifestRef.model_validate_json(canonical_bytes(value))
            for value in _array(record.get("required_manifests", []))
        )
        originals = tuple(
            _artifact(value) for value in _array(record.get("original_artifacts", []))
        )
        declared_count += len(required) + len(linked) + len(originals)
        if declared_count > MAX_REFERENCES:
            raise _fail("Required manifest exceeds reference limit")
        for ref in (*required, *originals, *(link.artifact for link in linked)):
            previous = declared_artifacts.get(ref.sha256)
            if previous is not None and physical_identity(
                previous
            ) != physical_identity(ref):
                raise _fail("Conflicting artifact declarations for one SHA")
            declared_artifacts.setdefault(ref.sha256, ref)
        for link in linked:
            schema = (link.type, str(link.schema_version))
            previous_schema = declared_schemas.get(link.artifact.sha256)
            if previous_schema is not None and previous_schema != schema:
                raise _fail("Conflicting manifest schemas for one SHA")
            declared_schemas[link.artifact.sha256] = schema
        artifacts.extend(required)
        manifests.extend(linked)
        if not capture_published:
            artifacts.extend(originals)
    return _References(
        tuple(artifacts), tuple(manifests), kind, version, declared_count
    )


def required_refs(manifest: JsonValue, *, capture_published: bool) -> frozenset[str]:
    """Validate one decoded manifest and return its direct payload dependencies."""
    refs = _parse(manifest, capture_published=capture_published)
    return frozenset(ref.sha256 for ref in refs.artifacts) | frozenset(
        ref.artifact.sha256 for ref in refs.manifests
    )


# Absolute ceilings on one closure walk, far above any real workspace. The
# bound of a walk is derived from the inventory it walks (its artifact rows), so
# an ordinary corpus never meets a fixed constant: one snapshot root reaches
# every retained index input of its workspace.
CLOSURE_MANIFEST_CEILING = 10_000_000
CLOSURE_REFERENCE_CEILING = 160_000_000
# Declared references a walk may count per walked artifact row (repeated edges
# count each time), and floors that keep a small inventory walkable, including
# a job's inputs walked before their rows are registered.
CLOSURE_REFERENCES_PER_ARTIFACT = 16
MIN_CLOSURE_MANIFESTS = 10_000
MIN_CLOSURE_REFERENCES = 100_000


def _limit(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="closure.limit_exceeded")


@dataclass(frozen=True)
class ClosureBudget:
    """Work one closure walk may do: distinct manifests and declared references."""

    manifests: int
    references: int


def closure_budget(artifact_count: int) -> ClosureBudget:
    """Bound a walk by the inventory it walks.

    Every walked manifest is a registered artifact and is loaded at most once,
    so the artifact rows bound the manifests; references scale with them.
    """
    if artifact_count > CLOSURE_MANIFEST_CEILING:
        raise _limit("Artifact inventory exceeds closure ceiling")
    return ClosureBudget(
        manifests=max(MIN_CLOSURE_MANIFESTS, artifact_count),
        references=min(
            CLOSURE_REFERENCE_CEILING,
            max(
                MIN_CLOSURE_REFERENCES,
                CLOSURE_REFERENCES_PER_ARTIFACT * artifact_count,
            ),
        ),
    )


def workspace_closure_budget(db) -> ClosureBudget:
    """Bound a walk of a local workspace by its own artifact rows."""
    (artifact_rows,) = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()
    return closure_budget(artifact_rows)


class ClosureWalk:
    """Walk verified typed links over many roots with one bounded visited set.

    Each manifest is loaded at most once across every ``walk`` call, and a
    repeated SHA (a diamond or a cycle) ends its branch. The budget bounds the
    whole walk, not one root. The loader owns verified decoding; unavailable or
    corrupt required manifests fail through it. ``artifacts`` also contains
    opaque payloads whose bytes the backup/deletion owner must verify under its
    inventory lease.
    """

    def __init__(
        self,
        *,
        load: Callable[[ManifestRef], JsonValue],
        budget: ClosureBudget,
    ) -> None:
        self._load = load
        self._budget = budget
        self._declarations: dict[str, tuple[str, str]] = {}
        self._artifacts: dict[str, ArtifactRef] = {}
        self._work = 0

    @property
    def artifacts(self) -> frozenset[str]:
        """Every SHA any walk so far required."""
        return frozenset(self._artifacts)

    @property
    def loaded(self) -> frozenset[str]:
        """Every manifest SHA any walk so far loaded."""
        return frozenset(self._declarations)

    def _remember(self, ref: ArtifactRef) -> None:
        previous = self._artifacts.get(ref.sha256)
        if previous is not None and physical_identity(previous) != physical_identity(
            ref
        ):
            raise _fail("Conflicting artifact declarations for one SHA")
        self._artifacts.setdefault(ref.sha256, ref)

    def _count(self, work: int) -> None:
        self._work += work
        if self._work > self._budget.references:
            raise _limit("Required closure exceeds reference limit")

    def _visit(
        self,
        descriptor: ManifestRef,
        is_published: Callable[[ArtifactRef, str], bool],
    ) -> tuple[ManifestRef, ...]:
        """Load one manifest unless already walked; return the links it adds."""
        try:
            descriptor = ManifestRef.model_validate(descriptor.model_dump())
        except ValueError:
            raise _fail("Malformed required manifest descriptor") from None
        ref, kind = descriptor.artifact, descriptor.type
        self._remember(ref)
        schema = (kind, str(descriptor.schema_version))
        previous = self._declarations.get(ref.sha256)
        if previous is not None:
            if previous != schema:
                raise _fail("Conflicting manifest schemas for one SHA")
            return ()
        if len(self._declarations) >= self._budget.manifests:
            raise _limit("Required closure exceeds manifest limit")
        published = is_published(ref, kind)
        if published and kind not in CAPTURE_TYPES:
            raise _fail("Only capture manifests may be marked published")
        self._declarations[ref.sha256] = schema
        refs = _parse(self._load(descriptor), capture_published=published)
        if (refs.type, refs.schema_version) != schema:
            raise _fail("Required manifest differs from its declared schema")
        self._count(refs.declared_count)
        for artifact in refs.artifacts:
            self._remember(artifact)
        return refs.manifests

    def walk(
        self,
        roots: tuple[ManifestRef, ...],
        *,
        published_captures: frozenset[str] = frozenset(),
        all_captures_published: bool = False,
    ) -> frozenset[str]:
        """Add what ``roots`` require; return every SHA required so far.

        A manifest an earlier walk loaded is not loaded again: its whole closure
        is already counted. ``all_captures_published`` treats every capture this
        walk loads as published, so their originals are not required.
        """
        self._count(len(roots))
        pending = list(reversed(roots))
        while pending:
            pending.extend(
                reversed(
                    self._visit(
                        pending.pop(),
                        lambda ref, kind: (
                            ref.sha256 in published_captures
                            or (all_captures_published and kind in CAPTURE_TYPES)
                        ),
                    )
                )
            )
        return self.artifacts


def required_closure(
    roots: tuple[ManifestRef, ...],
    *,
    load: Callable[[ManifestRef], JsonValue],
    budget: ClosureBudget,
    published_captures: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Walk verified typed links from ``roots`` in one bounded walk."""
    return ClosureWalk(load=load, budget=budget).walk(
        roots, published_captures=published_captures
    )


@dataclass(frozen=True)
class BackupInventory:
    """Required bytes and copied canonical state at one SQLite backup cut."""

    artifacts: tuple[ArtifactRef, ...]
    required_shas: frozenset[str]
    payload_states: tuple[tuple[str, str], ...]
    workspace_id: str
    policy_epoch: int
    lexical_statistics_epoch: int
    active_snapshot_id: str | None
    snapshot_ids: tuple[str, ...]
    tombstones: tuple[tuple[str, str], ...]
    config_shas: tuple[str, ...]


def _backup_budget(db, archive_entries: int | None) -> ClosureBudget:
    """Bound a backup walk, checking the row ceiling before any row is read.

    An imported archive passes ``archive_entries``, the artifact files it
    physically holds: its rows are its own claim and never widen the budget.
    """
    budget = workspace_closure_budget(db)
    return budget if archive_entries is None else closure_budget(archive_entries)


def _published_captures(
    pending: list[ManifestRef],
    *,
    load: Callable[[ManifestRef], JsonValue],
    budget: ClosureBudget,
) -> frozenset[str]:
    """Return published job roots and every capture they link.

    Published job captures may link repository captures. Their status is
    derived solely from the successful canonical job, never payload metadata.
    """
    published = {root.artifact.sha256 for root in pending}
    seen: set[str] = set()
    while pending:
        link = pending.pop()
        if link.artifact.sha256 in seen:
            continue
        if len(seen) >= budget.manifests:
            raise _limit("Required closure exceeds manifest limit")
        seen.add(link.artifact.sha256)
        content = load(link)
        required_refs(content, capture_published=False)
        if not isinstance(content, dict):
            raise _fail("Capture must be a typed object")
        for item in content.get("required_manifests", []):
            child = ManifestRef.model_validate(item)
            if child.type in CAPTURE_TYPES:
                published.add(child.artifact.sha256)
            pending.append(child)
    return frozenset(published)


def from_backup(db, *, artifacts, archive_entries=None) -> BackupInventory:
    """Enumerate only typed roots from a validated, copied canonical database.

    Hashes in provenance/data JSON and identity columns never create edges.
    The caller holds the owner barrier until it pins ``required_shas``.
    """
    import json

    rows = db.execute(
        "SELECT workspace_id,policy_epoch,lexical_statistics_epoch,active_snapshot_id "
        "FROM workspaces"
    ).fetchall()
    if len(rows) != 1:
        raise _fail("Copied database has ambiguous workspace identity")
    workspace_id, policy_epoch, lexical_epoch, active_snapshot = rows[0]
    budget = _backup_budget(db, archive_entries)
    registered = {}
    states = {}
    for sha, size, media, key, state in db.execute(
        "SELECT artifact_sha,size_bytes,media_type,storage_key,payload_state FROM artifacts"
    ):
        try:
            ref = artifact_ref((sha, size, media, key))
        except ValueError:
            raise _fail("Invalid copied artifact registration") from None
        if ref.storage_key != ref.sha256:
            raise _fail("Invalid copied artifact storage key")
        registered[sha] = ref
        states[sha] = state

    roots: list[ManifestRef] = []
    direct: set[str] = set()
    published_job_roots: set[str] = set()

    def add_direct(query: str) -> None:
        direct.update(sha for (sha,) in db.execute(query) if sha is not None)

    def add_manifest(query: str, kind: str) -> None:
        for (sha,) in db.execute(query):
            if sha is None:
                continue
            ref = registered.get(sha)
            if ref is None:
                raise _fail("Required manifest registration is missing")
            roots.append(registered_manifest(ref, kind))

    for query, kind in (*TYPED_MANIFEST_ROOTS, *_CAPTURE_AND_EXTRACTION_ROOTS):
        add_manifest(query, kind)
    for (sha,) in db.execute(
        "SELECT captured_inputs_sha FROM jobs WHERE captured_inputs_sha IS NOT NULL "
        "AND state='complete' AND result_snapshot_id IS NOT NULL"
    ):
        published_job_roots.add(sha)

    for query in (
        "SELECT artifact_sha FROM revision_artifacts WHERE role NOT IN ('original','source_file')",
        (
            "SELECT artifact_sha FROM revision_artifacts r WHERE role IN ('original','source_file') "
            "AND NOT EXISTS (SELECT 1 FROM original_payload_tombstones t "
            "WHERE t.revision_id=r.revision_id AND t.artifact_sha=r.artifact_sha)"
        ),
        (
            "SELECT original_sha FROM revisions r WHERE NOT EXISTS "
            "(SELECT 1 FROM original_payload_tombstones t WHERE t.revision_id=r.revision_id "
            "AND t.artifact_sha=r.original_sha)"
        ),
        "SELECT diagnostics_artifact_sha FROM extractions WHERE diagnostics_artifact_sha IS NOT NULL",
        "SELECT artifact_sha FROM assets",
        "SELECT derivation_sha FROM asset_proxies",
        "SELECT vectors_sha FROM index_batches WHERE vectors_sha IS NOT NULL",
        "SELECT request_artifact_sha FROM jobs WHERE request_artifact_sha IS NOT NULL",
        "SELECT output_artifact_sha FROM job_steps WHERE output_artifact_sha IS NOT NULL",
        "SELECT reason_artifact_sha FROM review_events WHERE reason_artifact_sha IS NOT NULL",
        "SELECT provenance_artifact_sha FROM assertion_decisions",
        "SELECT reason_artifact_sha FROM assertion_decisions WHERE reason_artifact_sha IS NOT NULL",
        "SELECT payload_sha FROM evaluation_cases",
        "SELECT result_artifact_sha FROM evaluation_results",
    ):
        add_direct(query)
    for revision_id, sha in db.execute(
        "SELECT e.revision_id,l.artifact_sha FROM locators l "
        "JOIN evidence_units u USING(unit_id) JOIN extractions e USING(extraction_id)"
    ):
        if db.execute(
            "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
            (revision_id, sha),
        ).fetchone():
            original = db.execute(
                "SELECT 1 FROM revisions WHERE revision_id=? AND original_sha=? "
                "UNION ALL SELECT 1 FROM revision_artifacts WHERE revision_id=? "
                "AND artifact_sha=? AND role IN ('original','source_file') LIMIT 1",
                (revision_id, sha, revision_id, sha),
            ).fetchone()
            if original:
                continue
        direct.add(sha)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _fail("Duplicate required manifest field")
            result[key] = value
        return result

    def load(link: ManifestRef):
        with artifacts.open_verified(link.artifact) as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise _fail("Required manifest exceeds byte limit")
        try:
            if link.type == "assertion":
                if any(
                    len(line) + 1 > MAX_ASSERTION_LINE_BYTES
                    for line in raw.splitlines()
                ):
                    raise _fail("Assertion record exceeds line byte limit")
                content = [
                    json.loads(line, object_pairs_hook=unique)
                    for line in raw.splitlines()
                ]
            else:
                content = json.loads(raw, object_pairs_hook=unique)
        except (UnicodeError, ValueError):
            raise _fail("Required manifest is malformed") from None
        # The closure returns SHAs, so check complete typed declarations here
        # before the registry descriptor can replace their size/storage key.
        parsed = _parse(content, capture_published=False)
        for declared in (
            *parsed.artifacts,
            *(child.artifact for child in parsed.manifests),
        ):
            stored = registered.get(declared.sha256)
            if stored is None or physical_identity(declared) != physical_identity(
                stored
            ):
                raise _fail("Required artifact declaration differs from registration")
        return content

    published = _published_captures(
        [root for root in roots if root.artifact.sha256 in published_job_roots],
        load=load,
        budget=budget,
    )
    required = set(
        required_closure(
            tuple(roots), load=load, budget=budget, published_captures=published
        )
    )
    required.update(direct)
    required.update(
        sha for sha, state in states.items() if state in {"present", "delete_pending"}
    )
    for sha in required:
        if sha not in registered:
            raise _fail("Required artifact registration is missing")
        if states[sha] == "deleted":
            raise _fail("Required artifact is marked deleted")
    return BackupInventory(
        artifacts=tuple(registered[sha] for sha in sorted(required)),
        required_shas=frozenset(required),
        payload_states=tuple(sorted((sha, states[sha]) for sha in required)),
        workspace_id=workspace_id,
        policy_epoch=policy_epoch,
        lexical_statistics_epoch=lexical_epoch,
        active_snapshot_id=active_snapshot,
        snapshot_ids=tuple(
            row[0]
            for row in db.execute(
                "SELECT snapshot_id FROM snapshots ORDER BY snapshot_id"
            )
        ),
        tombstones=tuple(
            db.execute(
                "SELECT revision_id,artifact_sha FROM original_payload_tombstones ORDER BY revision_id,artifact_sha"
            )
        ),
        config_shas=tuple(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT effective_config_sha FROM jobs ORDER BY effective_config_sha"
            )
        ),
    )
