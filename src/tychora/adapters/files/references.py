"""Bounded traversal of explicit, versioned retention headers.

Task-specific data and provenance never create retention edges. Only the
workspace owner may decide which terminal capture originals became provenance.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tychora.contracts.artifacts import (
    CAPTURE_TYPES,
    ArtifactRef,
    ManifestRef,
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
            if previous is not None and previous != ref:
                raise _fail("Conflicting artifact declarations for one SHA")
            declared_artifacts[ref.sha256] = ref
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
        if previous is not None and previous != ref:
            raise _fail("Conflicting artifact declarations for one SHA")
        artifacts[ref.sha256] = ref

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
