"""Versioned identities over canonical, byte-preserving JSON."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID, uuid4, uuid5

type JsonValue = (
    None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]
)


def _json_primitive(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_primitive(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("Only JSON values are supported")


def canonical_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        _json_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(tag: str, *parts: JsonValue) -> str:
    return hashlib.sha256(canonical_bytes([tag, *parts])).hexdigest()


def normalize_source_key(source_key: str) -> str:
    """Normalize a registered POSIX key without touching the filesystem."""
    if (
        not source_key
        or "\\" in source_key
        or "\x00" in source_key
        or PurePosixPath(source_key).is_absolute()
        or PureWindowsPath(source_key).drive
        or ".." in source_key.split("/")
    ):
        raise ValueError(
            "Source key must be a relative POSIX path without parent traversal"
        )
    normalized = str(PurePosixPath(source_key))
    if normalized == ".":
        raise ValueError("Source key must name a source")
    return normalized


def source_id(collection_id: UUID, source_key: str) -> str:
    return str(uuid5(collection_id, normalize_source_key(source_key)))


def revision_id(
    source_id: str,
    original_sha: str,
    edition_key: str | None,
    immutable_metadata_sha: str,
) -> str:
    return digest(
        "revision-v1", source_id, original_sha, edition_key, immutable_metadata_sha
    )


def extraction_id(
    revision_id: str, parser_fingerprint: str, normalizer_fingerprint: str
) -> str:
    return digest(
        "extraction-v1", revision_id, parser_fingerprint, normalizer_fingerprint
    )


def unit_id(extraction_id: str, native_ref: str, ordinal: int, text_sha: str) -> str:
    return digest("unit-v1", extraction_id, native_ref, ordinal, text_sha)


def chunk_id(
    unit_id: str, char_start: int, char_end: int, chunker_fingerprint: str
) -> str:
    return digest("chunk-v1", unit_id, char_start, char_end, chunker_fingerprint)


def space_id(
    modality: str,
    provider: str,
    model: str,
    dimensions: int,
    metric: str,
    representation_fingerprint: str,
) -> str:
    return digest(
        "space-v1",
        modality,
        provider,
        model,
        dimensions,
        metric,
        representation_fingerprint,
    )


def vector_record_id(
    family_id: str, chunk_or_asset_id: str, exact_input_sha: str
) -> str:
    return digest("vector-v1", family_id, chunk_or_asset_id, exact_input_sha)


def family_id(workspace_id: str, config_sha: str, creation_uuid: str) -> str:
    """Bind one physical family to its persisted creation-job nonce."""
    return digest("family-v1", workspace_id, config_sha, creation_uuid)


def new_family_nonce() -> str:
    """Persist this fresh nonce once per backend family creation."""
    return str(uuid4())


def repository_manifest(
    files: Sequence[tuple[str, str, str, str]], *, commit: str | None, dirty: bool
) -> dict[str, JsonValue]:
    entries = [
        {
            "relative_path": normalize_source_key(path),
            "mode": mode,
            "blob_sha": blob_sha,
            "artifact_ref": artifact_ref,
        }
        for path, mode, blob_sha, artifact_ref in files
    ]
    entries.sort(key=lambda entry: entry["relative_path"])
    if len({entry["relative_path"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate repository path")
    return {"files": entries, "commit": commit, "dirty": dirty}
