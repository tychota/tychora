import json
from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast
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
from tychora.adapters.files.assertions import iter_verified_assertions
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import MAX_MANIFEST_BYTES, required_refs
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.acquisition import CapturedFile, CaptureManifest
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.code import (
    CODE_ASSERTION_PROJECTION,
    CODE_RESOLVER_FINGERPRINT,
    CapturedSyntax,
    CodeRegistration,
)
from tychora.contracts.corpus import (
    CollectionRow,
    RevisionRow,
    SourceRow,
    WorkspaceRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, extraction_id, revision_id, source_id
from tychora.contracts.sections import (
    ROOT_SECTION_KEY,
    normalizer_definition,
    section_path_of,
)
from tychora.ports.language import Definition, SourceSpan


def _registration(tmp_path: Path, extra_payloads: dict[str, bytes] | None = None):
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    payloads = {
        "a.py": (
            b"def first(): return 1\n\ndef second(): return first()\n\n"
            b"class Parent:\n    def child(self): return 3\n"
        ),
        "nested/b.ts": b"export function build(): number { return 2; }\n",
        "notes.bin": b"\xffsynthetic\rnote",
        **(extra_payloads or {}),
    }
    entries = []
    parsed = []
    adapters = {
        **{path: PythonAdapter() for path in extra_payloads or {}},
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
    call = next(
        item["data"] for item in assertions if item["data"]["predicate"] == "calls@1"
    )
    assert set(call["required_support_ids"]) == {
        call["origin_unit_id"],
        call["object"]["unit_id"],
    }
    assert call["method"] == "parser_inferred"
    assert len(assertions) == 8
    assert retained.diagnostics_ref is not None
    with artifacts.open_verified(retained.diagnostics_ref) as stream:
        diagnostics = json.load(stream)
    unit_ids = {unit.unit_id for unit in bundle.units}
    assert diagnostics["data"]["resolutions"]
    assert all(
        item["source_unit_id"] in unit_ids
        for item in diagnostics["data"]["resolutions"]
    )

    child = next(row for row in bundle.records if row.data_json["name"] == "child")
    child_data = json.loads(child.model_dump_json())["data_json"]
    parent_context = next(
        span
        for span in child_data["context_spans"]
        if span["reason"] == "parent_declaration"
    )
    assert parent_context["dependency_unit_ids"] == [child_data["parent_unit_id"]]
    parent = next(
        unit for unit in bundle.units if unit.unit_id == child_data["parent_unit_id"]
    )
    parent_attributes = json.loads(parent.model_dump_json())["attributes"]
    assert child.unit_id in parent_attributes["range_dependency_ids"]

    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=artifacts)
    assert store.commit_extraction(bundle) == bundle.extraction.extraction_id
    symbols = [unit for unit in bundle.units if unit.unit_type == "symbol"]
    assert [unit.text for unit in store.hydrate(tuple(u.unit_id for u in symbols))]


def test_recursive_function_commits_a_calls_self_link(tmp_path):
    artifacts, registration, parsed = _registration(
        tmp_path,
        {"rec.py": b"def countdown(n):\n    return countdown(n - 1) if n else 0\n"},
    )
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    assert retained.assertions_ref is not None
    with artifacts.open_verified(retained.assertions_ref) as stream:
        calls = [
            item["data"]
            for item in map(json.loads, stream)
            if item["data"]["predicate"] == "calls@1"
        ]
    units = {unit.unit_id: unit for unit in bundle.units}
    selfs = [c for c in calls if c["subject"]["unit_id"] == c["object"]["unit_id"]]
    assert len(selfs) == 1
    assert selfs[0]["subject"]["entity_id"] == selfs[0]["object"]["entity_id"]
    assert units[selfs[0]["subject"]["unit_id"]].text.startswith("def countdown")

    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=artifacts)
    assert store.commit_extraction(bundle) == bundle.extraction.extraction_id


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


def test_unsupported_unique_target_is_diagnostic_without_semantic_assertion(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    syntax = parsed[0].syntax.model_copy(
        update={
            "references_found": tuple(
                reference.model_copy(update={"supported": False})
                for reference in parsed[0].syntax.references_found
            )
        }
    )
    parsed = (parsed[0].model_copy(update={"syntax": syntax}), *parsed[1:])
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    assert retained.assertions_ref is not None
    with artifacts.open_verified(retained.assertions_ref) as stream:
        assertions = [json.loads(line) for line in stream]
    assert "calls@1" not in {item["data"]["predicate"] for item in assertions}
    assert retained.diagnostics_ref is not None
    with artifacts.open_verified(retained.diagnostics_ref) as stream:
        resolutions = json.load(stream)["data"]["resolutions"]
    unsupported = [item for item in resolutions if not item["supported"]]
    assert unsupported
    assert all(
        item["state"] == "unresolved"
        and item["selected_target"] is None
        and item["rule"] == "unsupported_reference"
        for item in unsupported
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (("source_key", "code/forged.py"), ("mode", 0o600)),
)
def test_retention_and_direct_normalize_require_exact_capture_entry(
    tmp_path, field, value
):
    artifacts, registration, parsed = _registration(tmp_path)
    captured = parsed[0].captured.model_copy(update={field: value})
    forged_file = parsed[0].model_copy(update={"captured": captured})
    forged_files = (forged_file, *parsed[1:])
    with pytest.raises(ValueError, match="repository capture entries"):
        retain_code_extraction(registration, forged_files, registry(), artifacts)

    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    forged = retained.model_copy(update={"files": forged_files})
    with pytest.raises(ValueError, match="captured files"):
        normalize_code(forged, registry())


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


def test_code_retention_roundtrips_more_than_ten_thousand_sparse_assertions(
    tmp_path,
):
    artifacts, registration, _ = _registration(tmp_path)
    count = 5001
    payload = b"x\n" * count
    ref = artifacts.put_stream(BytesIO(payload), "text/plain")
    captured = CapturedFile(
        relative_path="large.py",
        source_key="code/large.py",
        mode=0o644,
        blob_sha=ref.sha256,
        artifact_ref=ref,
    )
    registration = registration.model_copy(
        update={
            "capture": registration.capture.model_copy(update={"entries": (captured,)})
        }
    )
    base = base_syntax_result(
        language="python",
        grammar="synthetic",
        adapter_fingerprint=digest("synthetic-adapter-v1"),
        file_bytes=payload,
        text=payload.decode(),
        encoding="utf-8",
        coverage="parsed",
        options={},
        root_type="module",
    )
    definitions = tuple(
        Definition(
            kind="function",
            name=f"item_{index}",
            qualified_name=f"item_{index}",
            scope=(),
            native_ref=f"definition:{index}",
            byte_start=index * 2,
            byte_end=index * 2 + 1,
            char_start=index * 2,
            char_end=index * 2 + 1,
            line_start=index + 1,
            line_end=index + 1,
            declaration_span=SourceSpan(
                native_ref=f"declaration:{index}",
                kind="declaration",
                byte_start=index * 2,
                byte_end=index * 2 + 1,
                char_start=index * 2,
                char_end=index * 2 + 1,
                line_start=index + 1,
                line_end=index + 1,
            ),
        )
        for index in range(count)
    )
    parsed = (
        CapturedSyntax(
            captured=captured,
            syntax=base.model_copy(update={"definitions_found": definitions}),
        ),
    )
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    assert retained.assertions_ref is not None
    assert len(retained.assertion_partition_refs) >= 2
    replayed = replay_code_extraction(
        retained.raw_document_ref, registration, artifacts
    )
    assert replayed.assertion_partition_refs == retained.assertion_partition_refs
    verified = tuple(
        iter_verified_assertions(
            retained.assertions_ref,
            artifacts,
            expected_extraction_id=extraction_id(
                registration.revision.revision_id,
                retained.parser_fingerprint,
                retained.normalizer_fingerprint,
            ),
        )
    )
    assert len(verified) == count * 2 - 1


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


def test_replay_and_registration_reject_hash_valid_oversized_partition(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    with artifacts.open_verified(retained.raw_document_ref) as stream:
        root = json.load(stream)
    child = {
        "type": "extraction",
        "schema_version": "1",
        "required_artifacts": [],
        "required_manifests": [],
        "data": {
            "files": root["data"]["files"],
            "padding": "x" * MAX_MANIFEST_BYTES,
        },
    }
    child_ref = artifacts.put_stream(
        BytesIO(json.dumps(child, separators=(",", ":")).encode()),
        "application/json",
    )
    root["data"]["files"] = []
    root["data"]["files_partitioned"] = True
    root["required_manifests"].append(
        {
            "artifact": child_ref.model_dump(mode="json"),
            "type": "extraction",
            "schema_version": "1",
        }
    )
    root_ref = artifacts.put_stream(
        BytesIO(json.dumps(root, sort_keys=True, separators=(",", ":")).encode()),
        "application/json",
    )
    with pytest.raises(ValueError, match="partition differs"):
        replay_code_extraction(root_ref, registration, artifacts)

    bundle = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, root_ref, child_ref),
            "extraction": bundle.extraction.model_copy(
                update={"document_artifact_sha": root_ref.sha256}
            ),
        }
    )
    store = CorpusStore(
        open_corpus(tmp_path / "oversized-part.sqlite3", initialize=True),
        registry=registry(),
        artifacts=artifacts,
    )
    with pytest.raises(TychoraError, match="partition exceeds byte capacity"):
        store.commit_extraction(bundle)


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


def test_descriptor_cannot_change_full_capture_entry_identity(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(retained, registry())
    forged = bundle.code_files[0].model_copy(update={"capture_entry_digest": "f" * 64})
    bundle = bundle.model_copy(update={"code_files": (forged, *bundle.code_files[1:])})
    store = CorpusStore(
        open_corpus(tmp_path / "forged-capture-entry.sqlite3", initialize=True),
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


def test_code_units_use_file_scheme(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(
        replay_code_extraction(retained.raw_document_ref, registration, artifacts),
        registry(),
    )
    for unit in bundle.units:
        path = unit.attributes["relative_path"]
        assert unit.boundary_key == f"file:{path}"
        assert [
            (entry.key, entry.level, entry.depth)
            for entry in section_path_of(unit.attributes, unit.boundary_key)
        ] == [(ROOT_SECTION_KEY, None, 0), (f"file:{path}", "file", 1)]
    kind = cast(Mapping[str, Any], registry().kinds["code_file@1"])
    assert bundle.extraction.normalizer_fingerprint == digest(
        "code-normalizer-v2",
        normalizer_definition(kind),
        registry().schema_digests,
        CODE_RESOLVER_FINGERPRINT,
        CODE_ASSERTION_PROJECTION,
    )
