"""Synthetic rows backed by real temporary bytes, with no pipeline services."""

from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import identity
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    ChunkRow,
    CollectionRow,
    ExtractionBundle,
    ExtractionRow,
    LocatorRow,
    RevisionArtifactRow,
    RevisionRow,
    SourceRow,
    WorkspaceRow,
)
from tychora.contracts.evidence import EvidenceUnit, TextLocator

_TEMP = TemporaryDirectory(prefix="synthetic-corpus-")
_ROOT = Path(_TEMP.name).resolve()
CREATED = "2024-01-01T00:00:00Z"
COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


_ARTIFACTS = LocalArtifacts(_ROOT / "artifacts")


def reader() -> LocalArtifacts:
    return _ARTIFACTS


def artifact(data: bytes, media_type="text/plain") -> ArtifactRef:
    return _ARTIFACTS.put_stream(BytesIO(data), media_type)


@lru_cache(maxsize=1)
def registry() -> Registry:
    path = _ROOT / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return Registry.from_config(resolve_config(path, "test", {}))


def seed_bundle(
    text: str = "Record the result.",
    *,
    source_key: str = "manual.md",
    kind: str = "manual@1",
) -> ExtractionBundle:
    import json

    samples = json.loads(
        (
            Path(__file__).parents[1] / "configuration/fixtures/generic-records.json"
        ).read_text()
    )
    metadata = FrozenJsonMap(samples[kind]["metadata"])
    original = artifact(text.encode("utf-8"))
    document = artifact(identity.canonical_bytes({"text": text}), "application/json")
    sid = identity.source_id(UUID(COLLECTION), source_key)
    metadata_sha = identity.digest("metadata-v1", metadata)
    rid = identity.revision_id(sid, original.sha256, "1", metadata_sha)
    eid = identity.extraction_id(rid, "synthetic-parser@1", "synthetic-normalizer@1")
    text_sha = sha256(text.encode()).hexdigest()
    uid = identity.unit_id(eid, "paragraph:1", 0, text_sha)
    locator = TextLocator(
        artifact_ref=original.sha256,
        native_ref=source_key,
        char_start=0,
        char_end=len(text),
    )
    unit = EvidenceUnit(
        unit_id=uid,
        extraction_id=eid,
        parent_id=None,
        ordinal=0,
        unit_type="paragraph",
        native_ref="paragraph:1",
        boundary_key="section:1",
        text=text,
        text_sha=text_sha,
        locators=(locator,),
    )
    return ExtractionBundle(
        workspace=WorkspaceRow(
            workspace_id="83322a35-016b-458b-b941-ae3c4a0dba60", created_at=CREATED
        ),
        collection=CollectionRow(
            collection_id=COLLECTION,
            workspace_id="83322a35-016b-458b-b941-ae3c4a0dba60",
            slug="manuals",
            title="Manuals",
        ),
        source=SourceRow(
            source_id=sid,
            collection_id=COLLECTION,
            source_key=source_key,
            created_at=CREATED,
        ),
        artifacts=(original, document),
        revision=RevisionRow(
            revision_id=rid,
            source_id=sid,
            kind_id=kind,
            original_sha=original.sha256,
            edition_key="1",
            metadata_sha=metadata_sha,
            title=cast(str, metadata["title"]),
            metadata_json=metadata,
            allowed_uses_json=("lookup", "compose", "audit"),
            captured_at=CREATED,
        ),
        revision_artifacts=(
            RevisionArtifactRow(
                revision_id=rid,
                role="original",
                source_ref=source_key,
                artifact_sha=original.sha256,
            ),
        ),
        extraction=ExtractionRow(
            extraction_id=eid,
            revision_id=rid,
            parser_fingerprint="synthetic-parser@1",
            normalizer_fingerprint="synthetic-normalizer@1",
            document_artifact_sha=document.sha256,
            created_at=CREATED,
        ),
        units=(unit,),
        locators=(
            LocatorRow(
                locator_id=identity.digest("locator-v1", uid, 0),
                unit_id=uid,
                fragment_ordinal=0,
                locator=locator,
            ),
        ),
        chunks=(
            ChunkRow(
                chunk_id=identity.chunk_id(uid, 0, len(text), "chunker@1"),
                unit_id=uid,
                extraction_id=eid,
                ordinal_in_unit=0,
                char_start=0,
                char_end=len(text),
                context_group_key="section:1",
                chunker_fingerprint="chunker@1",
                token_count=4,
                text=text,
            ),
        )
        if text
        else (),
    )
