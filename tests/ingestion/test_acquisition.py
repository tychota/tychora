import hashlib
import json
from uuid import UUID

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def acquisition(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    artifacts = LocalArtifacts((tmp_path / "artifacts").resolve())
    return Acquirer(artifacts, config), artifacts


def source(**fields):
    return SourceInput.model_validate(
        {
            "source_key": "notes",
            "collection_id": UUID("00000000-0000-4000-8000-000000000010"),
            "kind": "manual@1",
            **fields,
        }
    )


def test_inline_capture_needs_no_caller_temporary_file(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    artifacts = LocalArtifacts((tmp_path / "artifacts").resolve())
    source = SourceInput.model_validate(
        {
            "type": "text",
            "source_key": "notes",
            "collection_id": UUID("00000000-0000-4000-8000-000000000010"),
            "kind": "manual@1",
            "text": "Résumé",
        }
    )
    captured = Acquirer(artifacts, config).capture(source, ExecutionContext())
    with artifacts.open_verified(captured.original_ref) as data:
        assert data.read().decode() == "Résumé"
    assert captured.source_key == "notes"


def test_capture_retains_exact_bytes_and_serialized_identity(acquisition):
    from tychora.adapters.files.references import required_refs

    acquirer, artifacts = acquisition
    payload = b"\x00\xffsynthetic\r\n"
    captured = acquirer.capture(source(type="bytes", data=payload), ExecutionContext())
    with artifacts.open_verified(captured.original_ref) as stream:
        assert stream.read() == payload
    assert captured.original_sha == hashlib.sha256(payload).hexdigest()
    assert captured.config_sha == acquirer.config.config_sha
    assert captured.captured_at.tzinfo is not None
    assert captured.commit is None
    assert captured.dirty is False
    with artifacts.open_verified(captured.manifest_ref) as stream:
        manifest = json.load(stream)
    assert manifest["type"] == "job_capture"
    assert manifest["data"]["source_key"] == "notes"
    assert required_refs(manifest, capture_published=False) == {captured.original_sha}
    assert required_refs(manifest, capture_published=True) == set()


def test_capture_binds_source_metadata_and_validates_manifest(acquisition):
    from pydantic import ValidationError

    from tychora.contracts.acquisition import CaptureManifest

    acquirer, artifacts = acquisition
    captured = acquirer.capture(
        source(type="text", text="one", metadata={"edition": 2}), ExecutionContext()
    )
    with artifacts.open_verified(captured.manifest_ref) as stream:
        stored = json.load(stream)
    assert (
        stored["data"]["source"]["collection_id"]
        == "00000000-0000-4000-8000-000000000010"
    )
    assert stored["data"]["source"]["kind"] == "manual@1"
    assert stored["data"]["source"]["metadata"] == {"edition": 2}
    assert "text" not in stored["data"]["source"]
    values = captured.model_dump(mode="json")
    values["original_sha"] = "0" * 64
    with pytest.raises(ValidationError):
        CaptureManifest.model_validate_json(json.dumps(values))


def test_binary_source_intent_hashes_without_utf8_serialization():
    from tychora.application.jobs import source_intent

    intent = source_intent(source(type="bytes", data=b"\xff\x00"))
    assert intent["content_sha256"] == hashlib.sha256(b"\xff\x00").hexdigest()
    assert "data" not in intent


@pytest.mark.slow
def test_default_ten_thousand_file_capture_has_complete_retention(tmp_path):
    from tests.security.test_acquisition import item, local_acquirer
    from tychora.adapters.files.references import required_closure
    from tychora.contracts.artifacts import ManifestRef

    root, acquirer, artifacts = local_acquirer(tmp_path)
    for index in range(10000):
        (root / f"{index:05}.txt").write_bytes(str(index).encode())
    capture = acquirer.capture(item(type="folder", path=str(root)), ExecutionContext())
    assert len(capture.entries) == 10000

    def load(ref):
        with artifacts.open_verified(ref.artifact) as stream:
            return json.load(stream)

    closure = required_closure(
        (
            ManifestRef(
                artifact=capture.manifest_ref, type="job_capture", schema_version="1"
            ),
        ),
        load=load,
    )
    expected = {
        hashlib.sha256(str(index).encode()).hexdigest() for index in range(10000)
    }
    assert expected <= closure
    assert capture.original_ref.sha256 in closure
    with artifacts.open_verified(capture.manifest_ref) as stream:
        manifest = json.load(stream)
    assert manifest["required_manifests"]


def test_capture_is_durable_job_input_and_replay_does_not_read_live_path(tmp_path):
    from io import BytesIO

    from tests.security.test_acquisition import item, local_acquirer
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.jobs import source_intent
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.requests import RequestScope

    root, acquirer, artifacts = local_acquirer(tmp_path)
    (root / "main").write_bytes(b"original")
    submitted = item(type="file", path="main")
    captured = acquirer.capture(submitted, ExecutionContext())
    effective = artifacts.put_stream(
        BytesIO(
            canonical_bytes(
                {
                    "type": "registry",
                    "schema_version": "1",
                    "data": acquirer.config.model_dump(mode="json"),
                }
            )
        ),
        "application/json",
    )
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        db.execute(
            "INSERT INTO workspaces VALUES ('workspace',1,NULL,0,0,'2026-01-01')"
        )
        db.execute(
            "INSERT INTO collections VALUES ('00000000-0000-4000-8000-000000000010','workspace','manuals','Manuals',0,'{}')"
        )
        scope = RequestScope(
            principal_id="writer",
            collection_ids=frozenset({"00000000-0000-4000-8000-000000000010"}),
            capabilities=frozenset({"read", "write"}),
        )
        repo = JobRepository(db, artifacts=artifacts, workspace_id="workspace")
        job = repo.accept(
            "add", scope, "capture-once", source_intent(submitted), effective
        )
        repo.capture(job.id, captured.manifest_ref)
        (root / "main").unlink()
        replay = repo.accept(
            "add", scope, "capture-once", source_intent(submitted), effective
        )
        assert replay.id == job.id
        assert repo.get(job.id).captured_inputs == captured.manifest_ref
        with artifacts.open_verified(captured.original_ref) as stream:
            assert stream.read() == b"original"
    finally:
        db.close()


def test_folder_aggregate_streams_past_typed_manifest_limit(acquisition):
    from io import BytesIO

    from tychora.contracts.acquisition import CapturedFile

    acquirer, artifacts = acquisition
    leaf = artifacts.put_stream(BytesIO(b"same actual bytes"), "text/plain")
    entries = tuple(
        CapturedFile(
            relative_path=f"part-{index:06}.txt",
            source_key=f"notes/part-{index:06}.txt",
            mode=0o644,
            blob_sha=leaf.sha256,
            artifact_ref=leaf,
        )
        for index in range(50000)
    )
    aggregate = acquirer._tree(entries, (), ExecutionContext())
    assert aggregate.size_bytes > 16 * 1024 * 1024
    with artifacts.open_verified(aggregate) as stream:
        tree = json.load(stream)
    assert len(tree["entries"]) == 50000
    assert tree["entries"][-1]["source_key"] == "notes/part-049999.txt"


def test_capture_contract_rejects_duplicate_entries_and_wrong_blob_identity(
    acquisition,
):
    from pydantic import ValidationError

    from tychora.contracts.acquisition import CapturedFile, CaptureManifest

    acquirer, _ = acquisition
    capture = acquirer.capture(source(type="text", text="one"), ExecutionContext())
    fields = {
        "relative_path": "a",
        "source_key": "notes/a",
        "mode": 0o644,
        "blob_sha": "0" * 64,
        "artifact_ref": capture.original_ref,
    }
    with pytest.raises(ValidationError):
        CapturedFile.model_validate(fields)
    fields["blob_sha"] = capture.original_sha
    entry = CapturedFile.model_validate(fields)
    values = capture.model_dump()
    values["entries"] = (entry, entry)
    with pytest.raises(ValidationError):
        CaptureManifest.model_validate(values)
