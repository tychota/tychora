import json
from functools import lru_cache
from io import BytesIO

import pytest

from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tychora.adapters.code.syntax import source_maps
from tychora.adapters.files.assertions import (
    iter_verified_assertions,
    verify_assertion_closure,
)
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import required_refs
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.validation import verify_artifacts
from tychora.contracts.artifacts import ManifestRef
from tychora.contracts.code_storage import CodeFileDescriptor
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import TextLocator
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
    assertion_index_document,
    encode_assertions,
    partition_assertions,
)


@lru_cache(maxsize=4)
def _assertions(count: int, extraction_id: str = "extraction"):
    result = []
    for index in range(count):
        target = f"target-{index}"
        observation = AssertionObservation(
            observation_key=f"contains:{index}",
            revision_id="revision",
            extraction_id=extraction_id,
            predicate="contains@1",
            subject=RelationshipEndpoint(
                entity_id="source-unit", unit_id="source-unit", kind="code_file"
            ),
            object=RelationshipEndpoint(
                entity_id=target, unit_id=target, kind="symbol"
            ),
            origin_unit_id="source-unit",
            origin_locator=TextLocator(
                artifact_ref="a" * 64,
                native_ref="source.py",
                char_start=0,
                char_end=1,
            ),
            required_support_ids=frozenset({"source-unit", target}),
            method="native",
            status="recorded",
            confidence=1.0,
            derivation_fingerprint=digest("assertion-fixture-v1", index),
        )
        result.append(RelationshipAssertion.from_observation(observation))
    return tuple(result)


def _retained_index(artifacts, assertions):
    parts = partition_assertions(assertions)
    assert len(parts) > 1
    leaves = tuple(
        ManifestRef(
            artifact=artifacts.put_stream(BytesIO(part), "application/x-ndjson"),
            type="assertion",
            schema_version="1",
        )
        for part in parts
    )
    payload = assertion_index_document(assertions[0].extraction_id, leaves, assertions)
    root = artifacts.put_stream(BytesIO(payload), "application/json")
    return root, leaves, payload


def test_more_than_ten_thousand_sparse_assertions_roundtrip_and_bind_root_decision(
    tmp_path,
):
    bundle = seed_bundle()
    assertions = _assertions(10_001, bundle.extraction.extraction_id)
    parts = partition_assertions(assertions)
    leaves = tuple(
        ManifestRef(
            artifact=artifact(part, "application/x-ndjson"),
            type="assertion",
            schema_version="1",
        )
        for part in parts
    )
    payload = assertion_index_document(
        bundle.extraction.extraction_id, leaves, assertions
    )
    root = artifact(payload, "application/json")
    assert required_refs(json.loads(payload), capture_published=True) == frozenset(
        link.artifact.sha256 for link in leaves
    )
    closure = verify_assertion_closure(
        root,
        reader(),
        expected_extraction_id=bundle.extraction.extraction_id,
    )
    assert closure.assertion_count == 10_001
    assert sum(leaf.record_count for leaf in closure.leaves) == 10_001
    assert {leaf.artifact.sha256 for leaf in closure.leaves} == {
        link.artifact.sha256 for link in leaves
    }
    assert not hasattr(closure, "records")
    verified_count = 0
    child = None
    for child in iter_verified_assertions(
        root,
        reader(),
        expected_extraction_id=bundle.extraction.extraction_id,
    ):
        verified_count += 1
    assert verified_count == 10_001
    assert child is not None

    bundle = bundle.model_copy(
        update={
            "artifacts": (
                *bundle.artifacts,
                root,
                *(link.artifact for link in leaves),
            ),
            "extraction": bundle.extraction.model_copy(
                update={"assertions_artifact_sha": root.sha256}
            ),
        }
    )
    corpus = CorpusStore(
        open_corpus(tmp_path / "partitioned-assertions.sqlite3", initialize=True),
        registry=registry(),
        artifacts=reader(),
    )
    corpus.commit_extraction(bundle)
    corpus.record_assertion_decision(
        child.assertion_id,
        root.sha256,
        child.record_id,
        "confirmed",
        "reviewer",
    )
    assert (
        corpus.db.execute(
            "SELECT provenance_artifact_sha FROM assertion_decisions"
        ).fetchone()[0]
        == root.sha256
    )
    with pytest.raises(TychoraError, match="not registered to an extraction"):
        corpus.record_assertion_decision(
            child.assertion_id,
            child.leaf_ref.sha256,
            child.record_id,
            "rejected",
            "reviewer",
        )


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_assertion_index_rejects_missing_or_tampered_child(tmp_path, damage):
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    root, leaves, _ = _retained_index(artifacts, _assertions(10_001))
    path = artifacts.root / leaves[0].artifact.storage_key
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"{}")
    with pytest.raises(TychoraError, match="missing|verification"):
        tuple(iter_verified_assertions(root, artifacts))


def test_assertion_index_rejects_duplicate_child_and_cross_child_record(tmp_path):
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    first, second = _assertions(2)
    first_ref = artifacts.put_stream(
        BytesIO(encode_assertions((first,))), "application/x-ndjson"
    )
    second_ref = artifacts.put_stream(
        BytesIO(encode_assertions((first, second))), "application/x-ndjson"
    )
    first_link = ManifestRef(artifact=first_ref, type="assertion", schema_version="1")
    second_link = ManifestRef(artifact=second_ref, type="assertion", schema_version="1")

    duplicate_child = canonical_bytes(
        {
            "type": "assertion_index",
            "schema_version": "1",
            "required_artifacts": [],
            "required_manifests": [
                first_link.model_dump(mode="json"),
                first_link.model_dump(mode="json"),
            ],
            "data": {
                "extraction_id": "extraction",
                "assertion_count": 2,
                "leaf_shas": [first_ref.sha256, first_ref.sha256],
                "assertion_ids_digest": digest(
                    "assertion-index-ids-v1",
                    [first.assertion_id, first.assertion_id],
                ),
            },
        }
    )
    duplicate_child_ref = artifacts.put_stream(
        BytesIO(duplicate_child), "application/json"
    )
    with pytest.raises(TychoraError, match="malformed"):
        tuple(iter_verified_assertions(duplicate_child_ref, artifacts))

    duplicate_record = canonical_bytes(
        {
            "type": "assertion_index",
            "schema_version": "1",
            "required_artifacts": [],
            "required_manifests": [
                first_link.model_dump(mode="json"),
                second_link.model_dump(mode="json"),
            ],
            "data": {
                "extraction_id": "extraction",
                "assertion_count": 3,
                "leaf_shas": [first_ref.sha256, second_ref.sha256],
                "assertion_ids_digest": digest(
                    "assertion-index-ids-v1",
                    [first.assertion_id, first.assertion_id, second.assertion_id],
                ),
            },
        }
    )
    duplicate_record_ref = artifacts.put_stream(
        BytesIO(duplicate_record), "application/json"
    )
    with pytest.raises(TychoraError, match="duplicate"):
        tuple(iter_verified_assertions(duplicate_record_ref, artifacts))


def test_assertion_index_verifies_terminal_identity_before_first_record(tmp_path):
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    first, second = _assertions(2)
    leaves = tuple(
        ManifestRef(
            artifact=artifacts.put_stream(
                BytesIO(encode_assertions((assertion,))),
                "application/x-ndjson",
            ),
            type="assertion",
            schema_version="1",
        )
        for assertion in (first, second)
    )
    malformed = json.loads(
        assertion_index_document("extraction", leaves, (first, second))
    )
    malformed["data"]["assertion_ids_digest"] = "0" * 64
    root = artifacts.put_stream(BytesIO(canonical_bytes(malformed)), "application/json")

    with pytest.raises(TychoraError, match="count or ordered identity digest"):
        next(iter_verified_assertions(root, artifacts))


def test_non_code_commit_rejects_self_consistent_foreign_assertion_index(tmp_path):
    artifacts = reader()
    bundle = seed_bundle()
    assertions = _assertions(2, "foreign-extraction")
    leaves = tuple(
        ManifestRef(
            artifact=artifacts.put_stream(
                BytesIO(encode_assertions((assertion,))),
                "application/x-ndjson",
            ),
            type="assertion",
            schema_version="1",
        )
        for assertion in assertions
    )
    root = artifacts.put_stream(
        BytesIO(assertion_index_document("foreign-extraction", leaves, assertions)),
        "application/json",
    )
    bundle = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, root, *(link.artifact for link in leaves)),
            "extraction": bundle.extraction.model_copy(
                update={"assertions_artifact_sha": root.sha256}
            ),
        }
    )
    corpus = CorpusStore(
        open_corpus(tmp_path / "foreign-index.sqlite3", initialize=True),
        registry=registry(),
        artifacts=artifacts,
    )

    with pytest.raises(TychoraError, match="foreign"):
        corpus.commit_extraction(bundle)


def test_assertion_leaves_are_not_retained_in_general_validation_payload_cache():
    artifacts = reader()
    bundle = seed_bundle()
    original = next(
        ref for ref in bundle.artifacts if ref.sha256 == bundle.revision.original_sha
    )
    original_bytes = bundle.units[0].text.encode()
    maps = source_maps(original_bytes, bundle.units[0].text, "utf-8")
    descriptor = CodeFileDescriptor(
        relative_path=bundle.source.source_key,
        artifact_ref=original.sha256,
        capture_entry_digest="0" * 64,
        language="python",
        encoding="utf-8",
        coverage="parsed",
        adapter_fingerprint="1" * 64,
        byte_length=len(original_bytes),
        char_length=len(bundle.units[0].text),
        codepoint_to_byte=maps[0],
        byte_to_codepoint=maps[1],
        line_starts=maps[2],
        byte_to_line=maps[3],
    )
    bundle = bundle.model_copy(update={"code_files": (descriptor,)})
    assertions = _assertions(2, bundle.extraction.extraction_id)
    leaves = tuple(
        ManifestRef(
            artifact=artifacts.put_stream(
                BytesIO(encode_assertions((assertion,))), "application/json"
            ),
            type="assertion",
            schema_version="1",
        )
        for assertion in assertions
    )
    root = artifacts.put_stream(
        BytesIO(
            assertion_index_document(
                bundle.extraction.extraction_id, leaves, assertions
            )
        ),
        "application/json",
    )
    bundle = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, root, *(link.artifact for link in leaves)),
            "extraction": bundle.extraction.model_copy(
                update={"assertions_artifact_sha": root.sha256}
            ),
        }
    )

    payloads = verify_artifacts(bundle, artifacts)

    assert root.sha256 not in payloads
    assert not {link.artifact.sha256 for link in leaves} & payloads.keys()
