import io
import json

import pytest

from tychora.adapters.files import references
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import required_refs
from tychora.contracts.artifacts import ArtifactRef, ManifestRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes


def test_required_refs_reads_only_explicit_payload_header():
    manifest = {
        "type": "extraction",
        "schema_version": "1",
        "required_artifacts": [
            {
                "sha256": "a" * 64,
                "size_bytes": 3,
                "media_type": "text/plain",
                "storage_key": "a" * 64,
            }
        ],
        "data": {"sha256": "b" * 64},
        "provenance": {"sha256": "c" * 64},
    }
    assert required_refs(manifest, capture_published=False) == frozenset({"a" * 64})


TYPES = (
    "repository_capture",
    "extraction",
    "assertion",
    "registry",
    "index_input",
    "vector_cache",
    "job_capture",
    "retained_run",
    "evaluation",
    "backup",
)


def artifact(letter="a"):
    return {
        "sha256": letter * 64,
        "size_bytes": 3,
        "media_type": "application/json",
        "storage_key": letter * 64,
    }


def manifest(kind="extraction", **extra):
    result = {"type": kind, "schema_version": "1", **extra}
    if kind == "assertion":
        result.update(record_id="record-1", assertion_id="assertion-1")
    return result


@pytest.mark.parametrize("kind", TYPES)
def test_registered_types_reject_unknown_version(kind):
    with pytest.raises(TychoraError):
        required_refs(manifest(kind, schema_version="2"), capture_published=False)


@pytest.mark.parametrize("kind", TYPES)
def test_registered_types_accept_explicit_required_manifest_links(kind):
    value = manifest(
        kind,
        required_manifests=[
            {"artifact": artifact(), "type": "registry", "schema_version": "1"}
        ],
    )
    assert required_refs(value, capture_published=False) == frozenset({"a" * 64})


@pytest.mark.parametrize("kind", ["repository_capture", "job_capture"])
def test_published_capture_originals_retained_only_by_revision_bindings(kind):
    value = manifest(
        kind, original_artifacts=[artifact()], required_artifacts=[artifact("b")]
    )
    assert required_refs(value, capture_published=False) == frozenset(
        {"a" * 64, "b" * 64}
    )
    assert required_refs(value, capture_published=True) == frozenset({"b" * 64})


@pytest.mark.parametrize(
    "change",
    [
        {"type": "unknown"},
        {"schema_version": 1},
        {"unexpected": []},
        {"required_artifacts": ["a" * 64]},
        {"required_artifacts": {}},
        {
            "required_manifests": [
                {"artifact": artifact(), "type": "registry", "schema_version": "7"}
            ]
        },
        {"required_artifacts": [{**artifact(), "storage_key": "../escape"}]},
        {"required_artifacts": [{**artifact(), "size_bytes": True}]},
        {"original_artifacts": [artifact()]},
        {"capture_published": True},
    ],
)
def test_malformed_or_unknown_required_headers_fail_closed(change):
    with pytest.raises(TychoraError):
        required_refs(manifest(**change), capture_published=False)


def test_backup_accepts_integer_version_but_not_boolean():
    assert (
        required_refs(manifest("backup", schema_version=1), capture_published=False)
        == frozenset()
    )
    with pytest.raises(TychoraError):
        required_refs(manifest("backup", schema_version=True), capture_published=False)


def test_assertion_jsonl_records_union_headers_and_enforce_unique_ids():
    first = manifest("assertion", required_artifacts=[artifact()])
    second = {
        **manifest("assertion", required_artifacts=[artifact("b")]),
        "record_id": "record-2",
        "assertion_id": "assertion-2",
    }
    assert required_refs([first, second], capture_published=False) == frozenset(
        {"a" * 64, "b" * 64}
    )
    for key in ("record_id", "assertion_id"):
        with pytest.raises(TychoraError):
            required_refs([first, {**second, key: first[key]}], capture_published=False)


@pytest.mark.parametrize(
    "value",
    [
        [],
        [manifest()],
        manifest("assertion", original_artifacts=[]),
        {"type": "assertion", "schema_version": "1"},
    ],
)
def test_invalid_assertion_framing_is_rejected(value):
    with pytest.raises(TychoraError):
        required_refs(value, capture_published=False)


def test_manifest_and_assertion_work_is_bounded():
    with pytest.raises(TychoraError):
        required_refs(
            manifest(required_artifacts=[artifact()] * 10001), capture_published=False
        )
    with pytest.raises(TychoraError):
        required_refs(
            manifest("assertion", text="x" * (1024 * 1024)), capture_published=False
        )


def store_manifest(store, value):
    ref = store.put_stream(io.BytesIO(canonical_bytes(value)), "application/json")
    return ManifestRef(
        artifact=ref, type=value["type"], schema_version=value["schema_version"]
    )


def verified_loader(store):
    def load(ref):
        with store.open_verified(ref.artifact) as stream:
            if ref.type == "assertion":
                return [json.loads(line) for line in stream]
            return json.load(stream)

    return load


def test_transitive_closure_reads_verified_nested_manifests(tmp_path):
    store = LocalArtifacts(tmp_path)
    payload = store.put_stream(io.BytesIO(b"original bytes"), "text/plain")
    child = store_manifest(
        store, manifest("repository_capture", original_artifacts=[payload.model_dump()])
    )
    root = store_manifest(
        store, manifest("backup", required_manifests=[child.model_dump()])
    )
    assert references.required_closure(
        (root,), load=verified_loader(store)
    ) == frozenset({root.artifact.sha256, child.artifact.sha256, payload.sha256})
    assert references.required_closure(
        (root,),
        load=verified_loader(store),
        published_captures=frozenset({child.artifact.sha256}),
    ) == frozenset({root.artifact.sha256, child.artifact.sha256})


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_required_manifest_fails_closure(tmp_path, corrupt):
    store = LocalArtifacts(tmp_path)
    child = store_manifest(store, manifest("registry"))
    root = store_manifest(
        store, manifest("backup", required_manifests=[child.model_dump()])
    )
    path = tmp_path / child.artifact.storage_key
    if corrupt:
        path.write_bytes(b"corrupt")
    else:
        path.unlink()
    with pytest.raises(TychoraError):
        references.required_closure((root,), load=verified_loader(store))


def test_closure_rejects_descriptor_content_mismatch(tmp_path):
    store = LocalArtifacts(tmp_path)
    root = store_manifest(store, manifest("registry"))
    wrong = root.model_copy(update={"type": "extraction"})
    with pytest.raises(TychoraError):
        references.required_closure((wrong,), load=verified_loader(store))


@pytest.mark.parametrize(
    "change",
    [
        {"type": "extraction"},
        {"artifact": ArtifactRef.model_validate({**artifact(), "size_bytes": 4})},
    ],
)
def test_closure_rejects_conflicting_declarations_for_one_sha(change):
    root = ManifestRef(
        artifact=ArtifactRef(**artifact()), type="registry", schema_version="1"
    )
    conflicting = root.model_copy(update=change)
    with pytest.raises(TychoraError):
        references.required_closure(
            (root, conflicting), load=lambda ref: manifest(ref.type)
        )


def test_cycle_terminates_and_diamond_visits_shared_manifest_once():
    # Real CAS hash cycles require hash fixed points. Synthetic decoded loader
    # exercises traversal termination independently of byte verification.
    first = ManifestRef(
        artifact=ArtifactRef(**artifact()), type="registry", schema_version="1"
    )
    second = ManifestRef(
        artifact=ArtifactRef(**artifact("b")), type="registry", schema_version="1"
    )
    loaded = []

    def load(ref):
        loaded.append(ref.artifact.sha256)
        other = second if ref == first else first
        return manifest(
            "registry", required_manifests=[other.model_dump(), other.model_dump()]
        )

    assert references.required_closure((first,), load=load) == frozenset(
        {"a" * 64, "b" * 64}
    )
    assert loaded == ["a" * 64, "b" * 64]


def test_only_capture_roots_may_be_marked_published(tmp_path):
    store = LocalArtifacts(tmp_path)
    root = store_manifest(store, manifest("registry"))
    with pytest.raises(TychoraError):
        references.required_closure(
            (root,),
            load=verified_loader(store),
            published_captures=frozenset({root.artifact.sha256}),
        )


def test_closure_reads_assertion_jsonl_without_wrapper(tmp_path):
    store = LocalArtifacts(tmp_path)
    payload = store.put_stream(io.BytesIO(b"assertion support"), "text/plain")
    row = manifest("assertion", required_artifacts=[payload.model_dump()])
    ref = store.put_stream(
        io.BytesIO(canonical_bytes(row) + b"\n"), "application/x-ndjson"
    )
    root = ManifestRef(artifact=ref, type="assertion", schema_version="1")
    assert references.required_closure(
        (root,), load=verified_loader(store)
    ) == frozenset({ref.sha256, payload.sha256})


def test_closure_work_limit_applies_to_repeated_edges(monkeypatch):
    root = ManifestRef(
        artifact=ArtifactRef(**artifact()), type="registry", schema_version="1"
    )
    monkeypatch.setattr(references, "MAX_CLOSURE_REFERENCES", 3, raising=False)
    with pytest.raises(TychoraError, match="limit"):
        references.required_closure(
            (root,),
            load=lambda ref: manifest(
                "registry", required_manifests=[root.model_dump()] * 4
            ),
        )


def test_direct_headers_reject_conflicting_payload_metadata():
    value = manifest(required_artifacts=[artifact(), {**artifact(), "size_bytes": 7}])
    with pytest.raises(TychoraError):
        required_refs(value, capture_published=False)


def test_direct_headers_reject_conflicting_manifest_schemas():
    value = manifest(
        required_manifests=[
            {"artifact": artifact(), "type": "registry", "schema_version": "1"},
            {"artifact": artifact(), "type": "extraction", "schema_version": "1"},
        ]
    )
    with pytest.raises(TychoraError):
        required_refs(value, capture_published=False)


def test_immutable_json_manifests_use_the_same_reference_grammar():
    from tychora.contracts.base import FrozenJsonMap

    value = FrozenJsonMap(
        manifest(
            required_manifests=[
                {"artifact": artifact(), "type": "registry", "schema_version": "1"}
            ]
        )
    )
    assert required_refs(value, capture_published=False) == frozenset({"a" * 64})


def test_original_reference_limit_counts_each_declared_edge_once(monkeypatch):
    monkeypatch.setattr(references, "MAX_REFERENCES", 3)
    value = manifest("job_capture", original_artifacts=[artifact(), artifact("b")])
    assert required_refs(value, capture_published=False) == frozenset(
        {"a" * 64, "b" * 64}
    )


def test_published_originals_still_count_toward_total_declared_work(monkeypatch):
    monkeypatch.setattr(references, "MAX_REFERENCES", 3)
    value = manifest(
        "job_capture",
        original_artifacts=[artifact(), artifact("b")],
        required_artifacts=[artifact("c"), artifact("d")],
    )
    with pytest.raises(TychoraError):
        required_refs(value, capture_published=True)
