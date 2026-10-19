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


MAX_CLOSURE_REFERENCES = 100000
MAX_CLOSURE_MANIFESTS = 10000


def required_closure(
    roots: tuple[ManifestRef, ...],
    *,
    load: Callable[[ManifestRef], JsonValue],
    published_captures: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Walk verified typed links with bounded work and cycle termination.

    The loader owns verified decoding; unavailable/corrupt required manifests
    fail through it. The returned inventory also contains opaque payloads whose
    bytes the backup/deletion owner must verify under its inventory lease.
    """
    if len(roots) > MAX_CLOSURE_REFERENCES:
        raise _fail("Required closure exceeds reference limit")
    pending = list(reversed(roots))
    declarations: dict[str, tuple[str, str]] = {}
    artifacts: dict[str, ArtifactRef] = {}
    work = len(roots)

    def remember(ref: ArtifactRef) -> None:
        previous = artifacts.get(ref.sha256)
        if previous is not None and physical_identity(previous) != physical_identity(
            ref
        ):
            raise _fail("Conflicting artifact declarations for one SHA")
        artifacts.setdefault(ref.sha256, ref)

    while pending:
        descriptor = pending.pop()
        try:
            descriptor = ManifestRef.model_validate(descriptor.model_dump())
        except ValueError:
            raise _fail("Malformed required manifest descriptor") from None
        ref, kind = descriptor.artifact, descriptor.type
        remember(ref)
        schema = (kind, str(descriptor.schema_version))
        previous = declarations.get(ref.sha256)
        if previous is not None:
            if previous != schema:
                raise _fail("Conflicting manifest schemas for one SHA")
            continue
        if len(declarations) >= MAX_CLOSURE_MANIFESTS:
            raise _fail("Required closure exceeds manifest limit")
        published = ref.sha256 in published_captures
        if published and kind not in CAPTURE_TYPES:
            raise _fail("Only capture manifests may be marked published")
        declarations[ref.sha256] = schema
        refs = _parse(load(descriptor), capture_published=published)
        if (refs.type, refs.schema_version) != schema:
            raise _fail("Required manifest differs from its declared schema")
        work += refs.declared_count
        if work > MAX_CLOSURE_REFERENCES:
            raise _fail("Required closure exceeds reference limit")
        for artifact in refs.artifacts:
            remember(artifact)
        pending.extend(reversed(refs.manifests))
    return frozenset(artifacts)


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


def from_backup(db, *, artifacts) -> BackupInventory:
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

    for query, kind in (
        ("SELECT manifest_sha FROM snapshots", "snapshot"),
        ("SELECT input_manifest_sha FROM index_batches", "index_input"),
        ("SELECT effective_config_sha FROM jobs", "registry"),
        (
            "SELECT captured_inputs_sha FROM jobs WHERE captured_inputs_sha IS NOT NULL",
            "job_capture",
        ),
        ("SELECT document_artifact_sha FROM extractions", "extraction"),
        (
            "SELECT assertions_artifact_sha FROM extractions WHERE assertions_artifact_sha IS NOT NULL",
            "assertion",
        ),
        (
            "SELECT retained_payload_sha FROM query_runs WHERE retained_payload_sha IS NOT NULL",
            "retained_run",
        ),
        ("SELECT manifest_sha FROM evaluation_datasets", "evaluation"),
    ):
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
        "SELECT embedding_cache_sha FROM vector_records WHERE embedding_cache_sha IS NOT NULL",
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

    # Published job captures may link repository captures. Their status is
    # derived solely from the successful canonical job, never payload metadata.
    published = set(published_job_roots)
    pending = [root for root in roots if root.artifact.sha256 in published_job_roots]
    seen: set[str] = set()
    while pending:
        link = pending.pop()
        if link.artifact.sha256 in seen:
            continue
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
        if len(seen) > MAX_CLOSURE_MANIFESTS:
            raise _fail("Required closure exceeds manifest limit")

    required = set(
        required_closure(
            tuple(roots), load=load, published_captures=frozenset(published)
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
