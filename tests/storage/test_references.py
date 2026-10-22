import io
import json

import pytest

from tychora.adapters.files import references
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import required_refs
from tychora.contracts.artifacts import MANIFEST_VERSIONS, ArtifactRef, ManifestRef
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
    "assertion_index",
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
    result = {"type": kind, "schema_version": MANIFEST_VERSIONS[kind], **extra}
    if kind == "assertion":
        result.update(record_id="record-1", assertion_id="assertion-1")
    return result


@pytest.mark.parametrize("kind", TYPES)
def test_registered_types_reject_unknown_version(kind):
    with pytest.raises(TychoraError):
        required_refs(manifest(kind, schema_version="99"), capture_published=False)


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


BUDGET = references.closure_budget(1000)


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
        (root,), load=verified_loader(store), budget=BUDGET
    ) == frozenset({root.artifact.sha256, child.artifact.sha256, payload.sha256})
    assert references.required_closure(
        (root,),
        load=verified_loader(store),
        budget=BUDGET,
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
        references.required_closure((root,), load=verified_loader(store), budget=BUDGET)


def test_closure_rejects_descriptor_content_mismatch(tmp_path):
    store = LocalArtifacts(tmp_path)
    root = store_manifest(store, manifest("registry"))
    wrong = root.model_copy(update={"type": "extraction"})
    with pytest.raises(TychoraError):
        references.required_closure(
            (wrong,), load=verified_loader(store), budget=BUDGET
        )


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
            (root, conflicting), load=lambda ref: manifest(ref.type), budget=BUDGET
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

    assert references.required_closure((first,), load=load, budget=BUDGET) == frozenset(
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
            budget=BUDGET,
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
        (root,), load=verified_loader(store), budget=BUDGET
    ) == frozenset({ref.sha256, payload.sha256})


def test_closure_work_limit_applies_to_repeated_edges(monkeypatch):
    root = ManifestRef(
        artifact=ArtifactRef(**artifact()), type="registry", schema_version="1"
    )
    with pytest.raises(TychoraError, match="limit") as error:
        references.required_closure(
            (root,),
            load=lambda ref: manifest(
                "registry", required_manifests=[root.model_dump()] * 4
            ),
            budget=references.ClosureBudget(manifests=10, references=3),
        )
    assert error.value.reason == "closure.limit_exceeded"


def _ref(index: int, kind: str = "registry") -> ManifestRef:
    sha = f"{index:064x}"
    return ManifestRef(
        artifact=ArtifactRef(
            sha256=sha, size_bytes=3, media_type="application/json", storage_key=sha
        ),
        type=kind,
        schema_version=MANIFEST_VERSIONS[kind],
    )


def _tree(leaves: int, fanout: int = 100):
    """One root over pages over ``leaves`` manifests, as a snapshot is built."""
    children = [_ref(index + 1, "index_input") for index in range(leaves)]
    loaded: dict[str, object] = {
        child.artifact.sha256: manifest("index_input") for child in children
    }
    pages = []
    for start in range(0, leaves, fanout):
        page = _ref(leaves + 1 + len(pages))
        loaded[page.artifact.sha256] = manifest(
            "registry",
            required_manifests=[
                child.model_dump() for child in children[start : start + fanout]
            ],
        )
        pages.append(page)
    root = _ref(10**9)
    loaded[root.artifact.sha256] = manifest(
        "registry", required_manifests=[page.model_dump() for page in pages]
    )
    return root, loaded, 1 + len(pages) + leaves


def test_one_root_may_reach_more_manifests_than_ten_thousand_documents():
    # A snapshot root reaches every retained index input of the workspace.
    root, loaded, size = _tree(10_050)
    closure = references.required_closure(
        (root,),
        load=lambda ref: loaded[ref.artifact.sha256],
        budget=references.closure_budget(size),
    )
    assert len(closure) == size


def test_closure_budget_scales_with_the_walked_inventory():
    budget = references.closure_budget(70_002)
    assert budget.manifests == 70_002
    assert budget.references >= 70_002
    small = references.closure_budget(0)
    assert small.manifests == references.MIN_CLOSURE_MANIFESTS
    assert small.references == references.MIN_CLOSURE_REFERENCES
    with pytest.raises(TychoraError) as error:
        references.closure_budget(references.CLOSURE_MANIFEST_CEILING + 1)
    assert error.value.reason == "closure.limit_exceeded"


def test_closure_above_its_budget_fails_with_a_registered_reason():
    root, loaded, size = _tree(120, fanout=10)
    with pytest.raises(TychoraError) as error:
        references.required_closure(
            (root,),
            load=lambda ref: loaded[ref.artifact.sha256],
            budget=references.ClosureBudget(manifests=size - 1, references=10**6),
        )
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason == "closure.limit_exceeded"


def test_one_walk_loads_a_manifest_shared_by_several_roots_once():
    shared = _ref(1)
    first, second, published = _ref(2), _ref(3), _ref(4, "job_capture")
    original = artifact("c")
    loaded = {
        shared.artifact.sha256: manifest("registry"),
        first.artifact.sha256: manifest(
            "registry", required_manifests=[shared.model_dump()]
        ),
        second.artifact.sha256: manifest(
            "registry", required_manifests=[shared.model_dump()]
        ),
        published.artifact.sha256: manifest(
            "job_capture",
            required_manifests=[shared.model_dump()],
            original_artifacts=[original],
        ),
    }
    reads = []

    def load(ref):
        reads.append(ref.artifact.sha256)
        return loaded[ref.artifact.sha256]

    walk = references.ClosureWalk(load=load, budget=references.closure_budget(4))
    walk.walk((first,))
    walk.walk((second,))
    walk.walk((published,), all_captures_published=True)
    assert sorted(reads) == sorted(loaded)
    assert walk.artifacts == frozenset(loaded)
    assert walk.loaded == frozenset(loaded)


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


def test_closure_deduplicates_physical_payloads_with_binding_mime_hints(tmp_path):
    store = LocalArtifacts(tmp_path.resolve())
    payload = store.put_stream(io.BytesIO(b"same bytes"), "text/plain")
    other = payload.model_copy(update={"media_type": "application/custom"})
    child = store_manifest(
        store,
        manifest("registry", required_artifacts=[payload.model_dump(mode="json")]),
    )
    root = store_manifest(
        store,
        manifest(
            "job_capture",
            original_artifacts=[
                other.model_dump(mode="json"),
                payload.model_dump(mode="json"),
            ],
            required_manifests=[
                child.model_dump(mode="json"),
                child.model_copy(
                    update={
                        "artifact": child.artifact.model_copy(
                            update={"media_type": "application/octet-stream"}
                        )
                    }
                ).model_dump(mode="json"),
            ],
        ),
    )

    def load(ref):
        with store.open_verified(ref.artifact) as stream:
            return json.load(stream)

    assert references.required_closure((root,), load=load, budget=BUDGET) == {
        root.artifact.sha256,
        child.artifact.sha256,
        payload.sha256,
    }
