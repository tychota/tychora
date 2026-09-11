import json
from pathlib import Path
from uuid import UUID

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def native_case(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    artifacts = LocalArtifacts((tmp_path / "artifacts").resolve())
    collection = CollectionRow(
        collection_id="00000000-0000-4000-8000-000000000010",
        workspace_id="workspace",
        slug="documents",
        title="Synthetic documents",
    )
    workspace = WorkspaceRow(
        workspace_id="workspace", created_at="2024-01-01T00:00:00Z"
    )

    def make(
        text, *, kind="manual@1", format="markdown", metadata=None, config_override=None
    ):
        from tychora.contracts.extraction import NativeRegistration
        from tychora.ingestion.native import NativeParser

        samples = json.loads(
            (
                Path(__file__).parents[1]
                / "configuration/fixtures/generic-records.json"
            ).read_text()
        )
        metadata = FrozenJsonMap(
            samples[kind]["metadata"] if metadata is None else metadata
        )
        source_key = "synthetic." + format
        captured = Acquirer(artifacts, config).capture(
            SourceInput.model_validate(
                {
                    "type": "text",
                    "text": text,
                    "format": format,
                    "encoding": "utf-8",
                    "kind": kind,
                    "metadata": metadata,
                    "collection_id": UUID(collection.collection_id),
                    "source_key": source_key,
                }
            ),
            ExecutionContext(),
        )
        sid = identity.source_id(UUID(collection.collection_id), source_key)
        msha = identity.digest("metadata-v1", metadata)
        revision = RevisionRow(
            revision_id=identity.revision_id(sid, captured.original_sha, "1", msha),
            source_id=sid,
            kind_id=kind,
            original_sha=captured.original_sha,
            edition_key="1",
            metadata_sha=msha,
            title=str(metadata["title"]),
            metadata_json=metadata,
            captured_at=captured.captured_at.isoformat(),
            allowed_uses_json=("lookup",),
        )
        registration = NativeRegistration(
            capture=captured,
            workspace=workspace,
            collection=collection,
            source=SourceRow(
                source_id=sid,
                collection_id=collection.collection_id,
                source_key=source_key,
                created_at=captured.captured_at.isoformat(),
            ),
            revision=revision,
            format=format,
        )
        parsed = NativeParser(artifacts, registration).extract(
            captured.original_ref, config_override or config, ExecutionContext()
        )
        return parsed, artifacts, Registry.from_config(config_override or config)

    return make
