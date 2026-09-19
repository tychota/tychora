"""Bounded verification for direct assertion JSONL and partition indexes."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256

from tychora.contracts.artifacts import ArtifactRef, ManifestRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.artifacts import ArtifactReader

from .references import MAX_ASSERTION_LINE_BYTES, MAX_MANIFEST_BYTES, required_refs


@dataclass(frozen=True)
class VerifiedAssertion:
    root_ref: ArtifactRef
    leaf_ref: ArtifactRef
    record_id: str
    assertion_id: str
    assertion: RelationshipAssertion | None


@dataclass(frozen=True)
class VerifiedAssertionLeaf:
    artifact: ArtifactRef
    record_count: int
    assertion_ids_digest: str


@dataclass(frozen=True)
class VerifiedAssertionClosure:
    root_ref: ArtifactRef
    leaves: tuple[VerifiedAssertionLeaf, ...]
    assertion_count: int
    assertion_ids_digest: str
    extraction_id: str | None
    require_typed: bool


class _OrderedIdentityDigest:
    def __init__(self) -> None:
        self._hash = sha256(b'["assertion-index-ids-v1",[')
        self._count = 0

    def add(self, assertion_id: str) -> None:
        if self._count:
            self._hash.update(b",")
        self._hash.update(canonical_bytes(assertion_id))
        self._count += 1

    def hexdigest(self) -> str:
        value = self._hash.copy()
        value.update(b"]]")
        return value.hexdigest()


def _fail(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate assertion JSON field")
        value[key] = item
    return value


def _read_bounded(ref: ArtifactRef, artifacts: ArtifactReader) -> bytes:
    if ref.size_bytes > MAX_MANIFEST_BYTES:
        raise _fail("Assertion artifact exceeds verification bound")
    try:
        with artifacts.open_verified(ref) as stream:
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise _fail("Assertion artifact is unavailable") from None
    if len(payload) > MAX_MANIFEST_BYTES or len(payload) != ref.size_bytes:
        raise _fail("Assertion artifact exceeds verification bound")
    return payload


def _records(payload: bytes) -> Iterator[Mapping]:
    if not payload or not payload.endswith(b"\n"):
        raise _fail("Assertion JSONL requires complete newline-delimited records")
    lines = payload.splitlines(keepends=True)
    if len(lines) > 10_000:
        raise _fail("Assertion JSONL exceeds record capacity")
    for line in lines:
        if len(line) > MAX_ASSERTION_LINE_BYTES:
            raise _fail("Assertion JSONL record exceeds verification bound")
        try:
            record = json.loads(line, object_pairs_hook=_unique)
        except (UnicodeError, ValueError):
            raise _fail("Assertion JSONL is malformed") from None
        if not isinstance(record, Mapping):
            raise _fail("Assertion JSONL record is not an object")
        yield record


def _assertion(
    record: Mapping, *, require_typed: bool
) -> tuple[str, str, RelationshipAssertion | None]:
    record_id, assertion_id = record.get("record_id"), record.get("assertion_id")
    if (
        record.get("type") != "assertion"
        or record.get("schema_version") != "1"
        or not isinstance(record_id, str)
        or not record_id
        or not isinstance(assertion_id, str)
        or not assertion_id
    ):
        raise _fail("Assertion record identity or framing is invalid")
    if "data" not in record and not require_typed:
        return record_id, assertion_id, None
    try:
        value = RelationshipAssertion.model_validate_json(
            canonical_bytes(record["data"])
        )
    except (KeyError, TypeError, ValueError):
        raise _fail("Assertion record payload is invalid") from None
    if (
        record_id != value.assertion_id
        or assertion_id != value.assertion_id
        or record.get("required_artifacts")
        != [item.model_dump(mode="json") for item in value.required_artifacts]
        or record.get("required_manifests")
        != [item.model_dump(mode="json") for item in value.required_manifests]
    ):
        raise _fail("Assertion record header differs from its typed payload")
    return record_id, assertion_id, value


def verify_assertion_closure(
    root_ref: ArtifactRef,
    artifacts: ArtifactReader,
    *,
    expected_extraction_id: str | None = None,
) -> VerifiedAssertionClosure:
    """Verify a complete immutable assertion closure before exposing records."""
    root_payload = _read_bounded(root_ref, artifacts)
    links: tuple[ManifestRef, ...]
    expected_count: int | None = None
    expected_digest: str | None = None
    indexed = root_ref.media_type != "application/x-ndjson"
    if root_ref.media_type == "application/x-ndjson":
        links = (
            ManifestRef(artifact=root_ref, type="assertion", schema_version="1"),
        )
    else:
        try:
            root = json.loads(root_payload, object_pairs_hook=_unique)
            required_refs(root, capture_published=True)
            data = root["data"]
            links = tuple(
                ManifestRef.model_validate_json(canonical_bytes(value))
                for value in root["required_manifests"]
            )
            expected_count = data["assertion_count"]
            expected_digest = data["assertion_ids_digest"]
            if (
                root.get("type") != "assertion_index"
                or root.get("schema_version") != "1"
                or root.get("required_artifacts") != []
                or len(links) < 2
                or len(links) > 10_000
                or any(
                    link.type != "assertion" or link.schema_version != "1"
                    for link in links
                )
                or data["leaf_shas"]
                != [link.artifact.sha256 for link in links]
                or len({link.artifact.sha256 for link in links}) != len(links)
                or not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or expected_count <= 0
            ):
                raise ValueError("Invalid assertion index")
            index_extraction_id = data["extraction_id"]
        except (KeyError, TypeError, ValueError):
            raise _fail("Assertion index is malformed") from None
        if (
            expected_extraction_id is not None
            and index_extraction_id != expected_extraction_id
        ):
            raise _fail("Assertion index belongs to a foreign extraction")
        expected_extraction_id = index_extraction_id

    assertion_ids: set[str] = set()
    record_ids: set[str] = set()
    ordered_ids = _OrderedIdentityDigest()
    verified_leaves = []
    count = 0
    for link in links:
        payload = (
            root_payload
            if link.artifact.sha256 == root_ref.sha256
            else _read_bounded(link.artifact, artifacts)
        )
        leaf_references = 0
        leaf_count = 0
        leaf_ids = _OrderedIdentityDigest()
        for record in _records(payload):
            artifact_refs = record.get("required_artifacts", [])
            manifest_refs = record.get("required_manifests", [])
            if not isinstance(artifact_refs, list) or not isinstance(
                manifest_refs, list
            ):
                raise _fail("Assertion record references are malformed")
            leaf_references += len(artifact_refs) + len(manifest_refs)
            if leaf_references > 10_000:
                raise _fail("Assertion JSONL exceeds reference capacity")
            record_id, assertion_id, assertion = _assertion(
                record, require_typed=indexed or expected_extraction_id is not None
            )
            if (
                assertion_id in assertion_ids
                or record_id in record_ids
                or expected_extraction_id is not None
                and (
                    assertion is None
                    or assertion.extraction_id != expected_extraction_id
                )
            ):
                raise _fail("Assertion index contains duplicate or foreign records")
            assertion_ids.add(assertion_id)
            record_ids.add(record_id)
            ordered_ids.add(assertion_id)
            leaf_ids.add(assertion_id)
            count += 1
            leaf_count += 1
        verified_leaves.append(
            VerifiedAssertionLeaf(
                artifact=link.artifact,
                record_count=leaf_count,
                assertion_ids_digest=leaf_ids.hexdigest(),
            )
        )
    actual_digest = ordered_ids.hexdigest()
    if expected_count is not None and (
        count != expected_count or actual_digest != expected_digest
    ):
        raise _fail("Assertion index count or ordered identity digest differs")
    return VerifiedAssertionClosure(
        root_ref=root_ref,
        leaves=tuple(verified_leaves),
        assertion_count=count,
        assertion_ids_digest=actual_digest,
        extraction_id=expected_extraction_id,
        require_typed=indexed or expected_extraction_id is not None,
    )


def iter_verified_assertions(
    root_ref: ArtifactRef,
    artifacts: ArtifactReader,
    *,
    expected_extraction_id: str | None = None,
) -> Iterator[VerifiedAssertion]:
    """Yield records only after their complete root closure has been verified."""
    closure = verify_assertion_closure(
        root_ref,
        artifacts,
        expected_extraction_id=expected_extraction_id,
    )
    for leaf in closure.leaves:
        payload = _read_bounded(leaf.artifact, artifacts)
        leaf_count = 0
        leaf_ids = _OrderedIdentityDigest()
        for record in _records(payload):
            record_id, assertion_id, assertion = _assertion(
                record, require_typed=closure.require_typed
            )
            if closure.extraction_id is not None and (
                assertion is None or assertion.extraction_id != closure.extraction_id
            ):
                raise _fail("Assertion closure changed after verification")
            leaf_count += 1
            leaf_ids.add(assertion_id)
            yield VerifiedAssertion(
                closure.root_ref,
                leaf.artifact,
                record_id,
                assertion_id,
                assertion,
            )
        if (
            leaf_count != leaf.record_count
            or leaf_ids.hexdigest() != leaf.assertion_ids_digest
        ):
            raise _fail("Assertion closure changed after verification")
