import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.code.normalize import (
    normalize_code,
    replay_code_extraction,
    retain_code_extraction,
)
from tychora.adapters.code.python import PythonAdapter
from tychora.adapters.code.syntax import base_syntax_result
from tychora.adapters.code.typescript import TypeScriptAdapter
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import MAX_MANIFEST_BYTES, required_refs
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.acquisition import CapturedFile, CaptureManifest
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.code import CapturedSyntax, CodeRegistration
from tychora.contracts.corpus import (
    CollectionRow,
    RevisionRow,
    SourceRow,
    WorkspaceRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, revision_id, source_id


def _registration(tmp_path: Path):
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    payloads = {
        "a.py": (
            b"def first(): return 1\n\ndef second(): return first()\n\n"
            b"class Parent:\n    def child(self): return 3\n"
        ),
        "nested/b.ts": b"export function build(): number { return 2; }\n",
        "notes.bin": b"\xffsynthetic\rnote",
    }
    entries = []
    parsed = []
    adapters = {
        "a.py": PythonAdapter(),
        "nested/b.ts": TypeScriptAdapter("typescript"),
    }
    for path, payload in sorted(payloads.items()):
        ref = artifacts.put_stream(BytesIO(payload), "text/plain")
        captured = CapturedFile(
            relative_path=path,
            source_key=f"code/{path}",
            mode=0o644,
            blob_sha=ref.sha256,
            artifact_ref=ref,
        )
        entries.append(captured)
        if path in adapters:
            syntax = adapters[path].parse(payload, {})
        else:
            syntax = base_syntax_result(
                language="unknown",
                grammar="unsupported",
                adapter_fingerprint=digest("unsupported-adapter-v1"),
                file_bytes=payload,
                text=payload.decode("latin-1"),
                encoding="latin-1",
                coverage="text_fallback",
                options={},
                root_type="unsupported_file",
            )
        parsed.append(CapturedSyntax(captured=captured, syntax=syntax))

    metadata = FrozenJsonMap({"title": "Synthetic Repository"})
    source_input = FrozenJsonMap(
        {
            "type": "repository",
            "source_key": "code",
            "collection_id": "00000000-0000-4000-8000-000000000015",
            "kind": "code_file@1",
            "metadata": metadata,
        }
    )
    capture_ref = artifacts.put_stream(BytesIO(b"{}"), "application/json")
    capture = CaptureManifest(
        source_key="code",
        original_ref=capture_ref,
        original_sha=capture_ref.sha256,
        entries=tuple(entries),
        captured_at=datetime(2024, 1, 1, tzinfo=UTC),
        acquire_fingerprint=digest("capture-v1"),
        config_sha=digest("config-v1"),
        manifest_ref=capture_ref,
        source=source_input,
    )
    collection_id = "00000000-0000-4000-8000-000000000015"
    sid = source_id(UUID(collection_id), "code")
    rid = revision_id(sid, capture_ref.sha256, "1", digest("metadata-v1", metadata))
    registration = CodeRegistration(
        capture=capture,
        workspace=WorkspaceRow(
            workspace_id="workspace", created_at="2024-01-01T00:00:00Z"
        ),
        collection=CollectionRow(
            collection_id=collection_id,
            workspace_id="workspace",
            slug="code",
            title="Code",
        ),
        source=SourceRow(
            source_id=sid,
            collection_id=collection_id,
            source_key="code",
            source_type="repository",
            created_at="2024-01-01T00:00:00Z",
        ),
        revision=RevisionRow(
            revision_id=rid,
            source_id=sid,
            kind_id="code_file@1",
            original_sha=capture_ref.sha256,
            edition_key="1",
            metadata_sha=digest("metadata-v1", metadata),
            title="Synthetic Repository",
            metadata_json=metadata,
            captured_at="2024-01-01T00:00:00Z",
        ),
    )
    return artifacts, registration, tuple(parsed)


def test_mixed_repository_retains_replays_and_registers_exact_file_facts(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    replayed = replay_code_extraction(
        retained.raw_document_ref, registration, artifacts
    )
    bundle = normalize_code(replayed, registry())

    assert [item.relative_path for item in bundle.code_files] == [
        "a.py",
        "nested/b.ts",
        "notes.bin",
    ]
    assert [item.language for item in bundle.code_files] == [
        "python",
        "typescript",
        "unknown",
    ]
    containers = [unit for unit in bundle.units if unit.unit_type == "code_file"]
    assert len(containers) == 3
    assert all(not unit.citable and not unit.text for unit in containers)
    assert {unit.attributes["relative_path"] for unit in containers} == {
        "a.py",
        "nested/b.ts",
        "notes.bin",
    }

    assert retained.assertions_ref is not None
    with artifacts.open_verified(retained.assertions_ref) as stream:
        assertions = [json.loads(line) for line in stream]
    predicates = [item["data"]["predicate"] for item in assertions]
    assert predicates.count("contains@1") == 5
    assert predicates.count("precedes@1") == 2
    assert predicates.count("calls@1") == 1
    call = next(item["data"] for item in assertions if item["data"]["predicate"] == "calls@1")
    assert set(call["required_support_ids"]) == {
        call["origin_unit_id"],
        call["object"]["unit_id"],
    }
    assert call["method"] == "parser_inferred"
    assert len(assertions) == 8

    child = next(row for row in bundle.records if row.data_json["name"] == "child")
    child_data = json.loads(child.model_dump_json())["data_json"]
    parent_context = next(
        span
        for span in child_data["context_spans"]
        if span["reason"] == "parent_declaration"
    )
    assert parent_context["dependency_unit_ids"] == [child_data["parent_unit_id"]]
    parent = next(
        unit
        for unit in bundle.units
        if unit.unit_id == child_data["parent_unit_id"]
    )
    parent_attributes = json.loads(parent.model_dump_json())["attributes"]
    assert child.unit_id in parent_attributes["range_dependency_ids"]

    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=artifacts)
    assert store.commit_extraction(bundle) == bundle.extraction.extraction_id
    symbols = [unit for unit in bundle.units if unit.unit_type == "symbol"]
    assert [unit.text for unit in store.hydrate(tuple(u.unit_id for u in symbols))]


def test_same_bytes_at_two_paths_keep_distinct_file_and_definition_identity(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    duplicate = parsed[0].captured.artifact_ref
    copied = parsed[0].captured.model_copy(
        update={"relative_path": "copy.py", "source_key": "code/copy.py"}
    )
    capture = registration.capture.model_copy(
        update={
            "entries": tuple(
                sorted(
                    (*registration.capture.entries, copied),
                    key=lambda x: x.relative_path,
                )
            )
        }
    )
    registration = registration.model_copy(update={"capture": capture})
    parsed = tuple(
        sorted(
            (*parsed, CapturedSyntax(captured=copied, syntax=parsed[0].syntax)),
            key=lambda x: x.captured.relative_path,
        )
    )
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    keys = {
        row.data_json["definition_key"]
        for row in bundle.records
        if row.data_json["name"] == "first"
    }
    assert duplicate.sha256 == copied.artifact_ref.sha256
    assert len(keys) == 2


def test_large_repository_uses_verified_bounded_extraction_partitions(tmp_path):
    artifacts, registration, _ = _registration(tmp_path)
    empty_ref = artifacts.put_stream(BytesIO(b""), "text/plain")
    syntax = base_syntax_result(
        language="unknown",
        grammar="unsupported",
        adapter_fingerprint=digest("unsupported-adapter-v1"),
        file_bytes=b"",
        text="",
        encoding="utf-8",
        coverage="empty",
        options={},
        root_type="empty",
    )
    entries = tuple(
        CapturedFile(
            relative_path=f"generated/{index:04}.txt",
            source_key=f"code/generated/{index:04}.txt",
            mode=0o644,
            blob_sha=empty_ref.sha256,
            artifact_ref=empty_ref,
        )
        for index in range(4001)
    )
    registration = registration.model_copy(
        update={"capture": registration.capture.model_copy(update={"entries": entries})}
    )
    files = tuple(CapturedSyntax(captured=entry, syntax=syntax) for entry in entries)
    retained = retain_code_extraction(registration, files, registry(), artifacts)
    assert [link.type for link in retained.partition_refs] == [
        "extraction",
        "extraction",
    ]
    replayed = replay_code_extraction(
        retained.raw_document_ref, registration, artifacts
    )
    assert len(replayed.files) == 4001
    assert replayed.files[-1].captured.relative_path == "generated/4000.txt"
    empty_bundle = normalize_code(replayed, registry())
    assert len(empty_bundle.units) == 4001
    assert retained.assertions_ref is None

    missing = retained.partition_refs[0].artifact
    (artifacts.root / missing.storage_key).unlink()
    with pytest.raises(TychoraError, match="payload is missing"):
        replay_code_extraction(retained.raw_document_ref, registration, artifacts)
    (artifacts.root / missing.storage_key).write_bytes(b"{}")
    with pytest.raises(TychoraError, match="failed verification"):
        replay_code_extraction(retained.raw_document_ref, registration, artifacts)


def test_repository_capture_file_cap_is_enforced_before_retention(tmp_path):
    _, registration, parsed = _registration(tmp_path)
    with pytest.raises(ValueError):
        CodeRegistration.model_validate(
            registration.model_copy(
                update={
                    "capture": registration.capture.model_copy(
                        update={"entries": (parsed[0].captured,) * 90001}
                    )
                }
            ).model_dump()
        )


def test_retained_syntax_partitions_before_manifest_byte_limit(tmp_path):
    artifacts, registration, _ = _registration(tmp_path)
    empty_ref = artifacts.put_stream(BytesIO(b""), "text/plain")
    padding = "x" * (8 * 1024 * 1024 + 1024)
    syntax = base_syntax_result(
        language="unknown",
        grammar="unsupported",
        adapter_fingerprint=digest("unsupported-adapter-v1"),
        file_bytes=b"",
        text="",
        encoding="utf-8",
        coverage="empty",
        options={"padding": padding},
        root_type="empty",
    )
    entries = tuple(
        CapturedFile(
            relative_path=f"large/{index}.txt",
            source_key=f"code/large/{index}.txt",
            mode=0o644,
            blob_sha=empty_ref.sha256,
            artifact_ref=empty_ref,
        )
        for index in range(2)
    )
    registration = registration.model_copy(
        update={"capture": registration.capture.model_copy(update={"entries": entries})}
    )
    retained = retain_code_extraction(
        registration,
        tuple(CapturedSyntax(captured=entry, syntax=syntax) for entry in entries),
        registry(),
        artifacts,
    )
    assert len(retained.partition_refs) == 2
    for link in retained.partition_refs:
        with artifacts.open_verified(link.artifact) as stream:
            payload = stream.read()
        assert len(payload) <= MAX_MANIFEST_BYTES
        assert required_refs(json.loads(payload), capture_published=True) == frozenset()
    assert (
        len(
            replay_code_extraction(
                retained.raw_document_ref, registration, artifacts
            ).files
        )
        == 2
    )


@pytest.mark.parametrize("field", ["relative_path", "language"])
def test_symbol_record_cannot_swap_to_another_captured_file(tmp_path, field):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    row = bundle.records[0]
    value = dict(row.data_json)
    value[field] = "nested/b.ts" if field == "relative_path" else "typescript"
    forged = row.model_copy(update={"data_json": FrozenJsonMap(value)})
    bundle = bundle.model_copy(update={"records": (forged, *bundle.records[1:])})
    store = CorpusStore(
        open_corpus(tmp_path / f"forged-{field}.sqlite3", initialize=True),
        registry=registry(),
        artifacts=artifacts,
    )
    with pytest.raises(TychoraError):
        store.commit_extraction(bundle)


def test_descriptor_cannot_swap_to_another_captured_artifact(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    forged = bundle.code_files[0].model_copy(
        update={"artifact_ref": bundle.code_files[1].artifact_ref}
    )
    bundle = bundle.model_copy(update={"code_files": (forged, *bundle.code_files[1:])})
    store = CorpusStore(
        open_corpus(tmp_path / "forged-artifact.sqlite3", initialize=True),
        registry=registry(),
        artifacts=artifacts,
    )
    with pytest.raises(TychoraError, match="retained file provenance"):
        store.commit_extraction(bundle)


def test_empty_standalone_code_source_still_requires_metadata_pair(tmp_path):
    bundle = seed_bundle("", source_key="empty.py", kind="code_file@1")
    metadata = FrozenJsonMap({"title": "Empty Source"})
    bundle = bundle.model_copy(
        update={
            "revision": bundle.revision.model_copy(
                update={
                    "metadata_json": metadata,
                    "metadata_sha": digest("metadata-v1", metadata),
                    "title": "Empty Source",
                }
            )
        }
    )
    store = CorpusStore(
        open_corpus(tmp_path / "standalone.sqlite3", initialize=True),
        registry=registry(),
        artifacts=reader(),
    )
    with pytest.raises(TychoraError, match="requires relative_path and language"):
        store.commit_extraction(bundle)
