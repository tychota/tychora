"""Immutable native graph generation behavior."""

import io

import pytest

import tychora.adapters.sqlite.graph as sqlite_graph
from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.graph import read_graph_build
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit, TextLocator
from tychora.contracts.graph import (
    GraphAssertionBinding,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphManifest,
    GraphUnitBinding,
)
from tychora.contracts.identity import digest
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
    encode_assertions,
)
from tychora.ports.execution import ExecutionContext


def _assertion(method="native", status="recorded") -> RelationshipAssertion:
    origin = TextLocator(artifact_ref="text", native_ref="a", char_start=0, char_end=1)
    observation = AssertionObservation(
        observation_key="sample",
        revision_id="revision",
        extraction_id="extraction",
        predicate="references@1",
        subject=RelationshipEndpoint(entity_id="subject", unit_id="a", kind="symbol@1"),
        object=RelationshipEndpoint(entity_id="object", unit_id="b", kind="symbol@1"),
        origin_unit_id="a",
        origin_locator=origin,
        required_support_ids=frozenset({"a", "b"}),
        method=method,
        status=status,
        confidence=1.0,
        derivation_fingerprint=digest("fixture"),
    )
    return RelationshipAssertion.from_observation(observation)


def test_graph_cannot_seal_ungrounded_assertion(tmp_path):
    graph = LadybugGraphStore(tmp_path / "graph")
    generation = graph.begin_generation(
        GraphGenerationSpec(
            generation_id="generation",
            family_id=digest("family"),
            config_sha=digest("config"),
            registry_digest=digest("registry"),
            workspace_id="workspace",
            decision_watermark=0,
        )
    )
    bad = _assertion().model_copy(update={"required_support_ids": frozenset()})
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(generation, (bad,), (), ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"


def _unit(uid: str) -> EvidenceUnit:
    text = uid
    return EvidenceUnit(
        unit_id=uid,
        extraction_id="extraction",
        parent_id=None,
        ordinal=0,
        unit_type="symbol",
        native_ref=uid,
        boundary_key=uid,
        text=text,
        text_sha=digest("text", text),
        locators=(
            TextLocator(artifact_ref="text", native_ref=uid, char_start=0, char_end=1),
        ),
    )


def _build(tmp_path, *, method="native", status="recorded", allow_model=False):
    assertion = _assertion(method=method, status=status)
    store = LocalArtifacts(tmp_path / "artifacts")
    root = store.put_stream(
        io.BytesIO(encode_assertions((assertion,))), "application/x-ndjson"
    )
    units = (_unit("a"), _unit("b"))
    spec = GraphGenerationSpec(
        generation_id="generation",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=digest("registry"),
        workspace_id="workspace",
        decision_watermark=0,
        memberships=(
            GraphExtraction(
                extraction_id="extraction",
                revision_id="revision",
                assertion_root=root,
                unit_ids=("a", "b"),
            ),
        ),
    )
    build = GraphBuildInput(
        spec=spec,
        units=tuple(
            GraphUnitBinding(unit=unit, revision_id="revision") for unit in units
        ),
        assertions=(
            GraphAssertionBinding(
                assertion=assertion, artifact=root, record_id=assertion.assertion_id
            ),
        ),
        relations=FrozenJsonMap(
            {
                "references@1": {
                    "subject_unit_types": ["symbol"],
                    "object_unit_types": ["symbol"],
                    "subject_kinds": ["symbol@1"],
                    "object_kinds": ["symbol@1"],
                    "methods": ["native", "model"] if allow_model else ["native"],
                    "revision_binding": "same_revision",
                    "cardinality": "many_to_many",
                    "self_links": False,
                }
            }
        ),
    )
    return spec, build, units, assertion


def test_generation_seals_native_grounding_and_reopens(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    expected = GraphManifest.from_build(build)
    receipt = graph.seal_generation(generation, expected, ExecutionContext())
    assert receipt.config_sha == spec.config_sha
    assert receipt.assertion_count == 1
    assert receipt.evidence_count == 2
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion)-[:SupportedBy]->(e:Evidence) WHERE a.generation_id='generation' RETURN e.evidence_id ORDER BY e.evidence_id"
    ).get_all() == [["a"], ["b"]]
    graph.close()
    reopened = LadybugGraphStore(tmp_path / "graph", build=build)
    assert reopened.seal_generation(generation, expected, ExecutionContext()) == receipt
    with pytest.raises(TychoraError) as err:
        reopened.upsert_batch(generation, (assertion,), (), ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"


def test_graph_rejects_unresolved_support_before_native_write(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(generation, (assertion,), (units[0],), ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion) RETURN count(a)"
    ).get_all() == [[0]]


def test_sqlite_graph_build_checks_registered_membership(tmp_path):
    bundle = seed_bundle("One source unit.")
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=registry(), artifacts=reader())
    corpus.commit_extraction(bundle)
    spec = GraphGenerationSpec(
        generation_id="generation",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=registry().registry_digest,
        workspace_id=bundle.workspace.workspace_id,
        decision_watermark=0,
        memberships=(
            GraphExtraction(
                extraction_id=bundle.extraction.extraction_id,
                revision_id=bundle.revision.revision_id,
                unit_ids=(bundle.units[0].unit_id,),
            ),
        ),
    )
    build = read_graph_build(corpus, spec, ExecutionContext())
    assert build.units[0].unit == bundle.units[0]
    bad = spec.model_copy(
        update={
            "memberships": (
                spec.memberships[0].model_copy(update={"unit_ids": ("missing",)}),
            )
        }
    )
    with pytest.raises(TychoraError) as err:
        read_graph_build(corpus, bad, ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"
    db.close()


def test_sqlite_graph_build_reads_pinned_assertion_decision(tmp_path):
    bundle = seed_bundle("One source unit.")
    unit = bundle.units[0]
    observation = AssertionObservation(
        observation_key="record",
        revision_id=bundle.revision.revision_id,
        extraction_id=bundle.extraction.extraction_id,
        predicate="references@1",
        subject=RelationshipEndpoint(
            entity_id="a", unit_id=unit.unit_id, kind="symbol@1"
        ),
        object=RelationshipEndpoint(
            entity_id="b", unit_id=unit.unit_id, kind="symbol@1"
        ),
        origin_unit_id=unit.unit_id,
        origin_locator=unit.locators[0],
        required_support_ids=frozenset({unit.unit_id}),
        method="native",
        status="recorded",
        confidence=1.0,
        derivation_fingerprint=digest("fixture"),
    )
    assertion = RelationshipAssertion.from_observation(observation)
    root = artifact(encode_assertions((assertion,)), "application/x-ndjson")
    bundle = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, root),
            "extraction": bundle.extraction.model_copy(
                update={"assertions_artifact_sha": root.sha256}
            ),
        }
    )
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=registry(), artifacts=reader())
    corpus.commit_extraction(bundle)
    spec = GraphGenerationSpec(
        generation_id="generation",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=registry().registry_digest,
        workspace_id=bundle.workspace.workspace_id,
        decision_watermark=0,
        memberships=(
            GraphExtraction(
                extraction_id=bundle.extraction.extraction_id,
                revision_id=bundle.revision.revision_id,
                assertion_root=root,
                unit_ids=(unit.unit_id,),
            ),
        ),
    )
    assert (
        read_graph_build(corpus, spec, ExecutionContext())
        .assertions[0]
        .status_at_watermark
        == "recorded"
    )
    watermark = corpus.record_assertion_decision(
        assertion.assertion_id,
        root.sha256,
        assertion.assertion_id,
        "rejected",
        "reviewer",
    )
    assert (
        read_graph_build(corpus, spec, ExecutionContext())
        .assertions[0]
        .status_at_watermark
        == "recorded"
    )
    advanced = spec.model_copy(update={"decision_watermark": watermark})
    assert (
        read_graph_build(corpus, advanced, ExecutionContext())
        .assertions[0]
        .status_at_watermark
        == "rejected"
    )
    db.close()


def test_model_promotion_uses_applicable_pinned_canonical_human_decision(tmp_path):
    workspace = tmp_path / "workspace.toml"
    workspace.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(workspace, "test", {})
    data = config.model_dump(mode="json")["registry"]
    data["relations"]["supports@1"]["methods"].append("model")
    config = config.model_copy(
        update={
            "registry": FrozenJsonMap(data),
            "registry_digest": digest("registry-v1", data),
        }
    )
    model_registry = Registry.from_config(config)
    bundle = seed_bundle("One source unit.")
    unit = bundle.units[0]
    assertion = RelationshipAssertion.from_observation(
        AssertionObservation(
            observation_key="model-observation",
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
            predicate="supports@1",
            subject=RelationshipEndpoint(
                entity_id="subject", unit_id=unit.unit_id, kind="manual@1"
            ),
            object=RelationshipEndpoint(
                entity_id="object", unit_id=unit.unit_id, kind="manual@1"
            ),
            origin_unit_id=unit.unit_id,
            origin_locator=unit.locators[0],
            required_support_ids=frozenset({unit.unit_id}),
            method="model",
            status="proposed",
            confidence=0.5,
            derivation_fingerprint=digest("model-fixture"),
        )
    )
    root = artifact(encode_assertions((assertion,)), "application/x-ndjson")
    bundle = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, root),
            "extraction": bundle.extraction.model_copy(
                update={"assertions_artifact_sha": root.sha256}
            ),
        }
    )
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=model_registry, artifacts=reader())
    corpus.commit_extraction(bundle)
    watermark = corpus.record_assertion_decision(
        assertion.assertion_id,
        root.sha256,
        assertion.assertion_id,
        "confirmed",
        "reviewer",
    )
    spec = GraphGenerationSpec(
        generation_id="generation",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=model_registry.registry_digest,
        workspace_id=bundle.workspace.workspace_id,
        decision_watermark=watermark,
        memberships=(
            GraphExtraction(
                extraction_id=bundle.extraction.extraction_id,
                revision_id=bundle.revision.revision_id,
                assertion_root=root,
                unit_ids=(unit.unit_id,),
            ),
        ),
    )
    build = read_graph_build(corpus, spec, ExecutionContext())
    assert build.assertions[0].decision_id == watermark
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(
        generation, (assertion,), (build.units[0].unit,), ExecutionContext()
    )
    graph.seal_generation(
        generation, GraphManifest.from_build(build), ExecutionContext()
    )
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion) RETURN a.method,a.status"
    ).get_all() == [["model", "confirmed"]]
    db.close()


def test_seal_detects_native_evidence_content_tamper(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph._connection.execute(
        "MATCH (e:Evidence) WHERE e.evidence_id='a' SET e.content_hash='wrong'"
    )
    with pytest.raises(TychoraError) as err:
        graph.seal_generation(
            generation, GraphManifest.from_build(build), ExecutionContext()
        )
    assert err.value.code == "INTEGRITY_ERROR"


def test_sealed_reopen_rechecks_native_graph_before_receipt(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    expected = GraphManifest.from_build(build)
    graph.seal_generation(generation, expected, ExecutionContext())
    graph._connection.execute(
        "MATCH (e:Evidence) WHERE e.evidence_id='a' SET e.content_hash='wrong'"
    )
    graph.close()
    reopened = LadybugGraphStore(tmp_path / "graph", build=build)
    with pytest.raises(TychoraError) as err:
        reopened.seal_generation(generation, expected, ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"


def test_seal_detects_native_assertion_status_tamper(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph._connection.execute("MATCH (a:RelationAssertion) SET a.status='confirmed'")
    with pytest.raises(TychoraError) as err:
        graph.seal_generation(
            generation, GraphManifest.from_build(build), ExecutionContext()
        )
    assert err.value.code == "INTEGRITY_ERROR"


def test_native_batch_rejects_contradictory_entity_grounding(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    bad = assertion.model_copy(
        update={"object": assertion.object.model_copy(update={"entity_id": "subject"})}
    )
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(generation, (bad,), units, ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion) RETURN count(a)"
    ).get_all() == [[0]]


def test_retry_restores_missing_native_edge_after_interruption(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph._connection.execute(
        "MATCH (a:RelationAssertion)-[r:SupportedBy]->(e:Evidence) WHERE e.evidence_id='b' DELETE r"
    )
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph.seal_generation(
        generation, GraphManifest.from_build(build), ExecutionContext()
    )


def test_predicate_many_to_one_rejects_second_object(tmp_path):
    spec, build, units, first = _build(tmp_path)
    data = first.model_dump(mode="json", exclude={"assertion_id"})
    data["observation_key"] = "second"
    data["object"] = {"entity_id": "other", "unit_id": "c", "kind": "symbol@1"}
    data["required_support_ids"] = ["a", "c"]
    second = RelationshipAssertion.from_observation(
        AssertionObservation.from_mapping(data)
    )
    store = LocalArtifacts(tmp_path / "artifacts")
    root = store.put_stream(
        io.BytesIO(encode_assertions((first, second))), "application/x-ndjson"
    )
    spec = spec.model_copy(
        update={
            "memberships": (
                spec.memberships[0].model_copy(
                    update={"assertion_root": root, "unit_ids": ("a", "b", "c")}
                ),
            )
        }
    )
    build = GraphBuildInput(
        spec=spec,
        units=(*build.units, GraphUnitBinding(unit=_unit("c"), revision_id="revision")),
        assertions=tuple(
            GraphAssertionBinding(
                assertion=item, artifact=root, record_id=item.assertion_id
            )
            for item in (first, second)
        ),
        relations=FrozenJsonMap(
            {
                "references@1": {
                    **dict(build.relations["references@1"]),
                    "cardinality": "many_to_one",
                }
            }
        ),
    )
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(
            generation, (first, second), (*units, _unit("c")), ExecutionContext()
        )
    assert err.value.code == "INTEGRITY_ERROR"
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion) RETURN count(a)"
    ).get_all() == [[0]]


def test_explicit_model_predicate_projects_proposed_status(tmp_path):
    spec, build, units, assertion = _build(
        tmp_path, method="model", status="proposed", allow_model=True
    )
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph.seal_generation(
        generation, GraphManifest.from_build(build), ExecutionContext()
    )
    assert graph._connection.execute(
        "MATCH (a:RelationAssertion) RETURN a.method,a.status"
    ).get_all() == [["model", "proposed"]]


def test_unlisted_or_self_promoting_model_method_is_rejected(tmp_path):
    for allow_model, status in ((False, "proposed"), (True, "recorded")):
        case = tmp_path / ("allowed" if allow_model else "unlisted")
        spec, build, units, assertion = _build(
            case, method="model", status=status, allow_model=allow_model
        )
        graph = LadybugGraphStore(case / "graph", build=build)
        generation = graph.begin_generation(spec)
        with pytest.raises(TychoraError) as err:
            graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
        assert err.value.code == "INTEGRITY_ERROR"


def test_drop_generation_removes_native_nodes_and_allows_rebuild(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph.seal_generation(
        generation, GraphManifest.from_build(build), ExecutionContext()
    )
    graph.drop_generation(generation, ExecutionContext())
    assert graph._connection.execute("MATCH (n:Entity) RETURN count(n)").get_all() == [
        [0]
    ]
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    graph.seal_generation(
        generation, GraphManifest.from_build(build), ExecutionContext()
    )


def test_same_revision_predicate_rejects_cross_revision_endpoint(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    foreign = units[1].model_copy(update={"extraction_id": "foreign"})
    spec = spec.model_copy(
        update={
            "memberships": (
                spec.memberships[0].model_copy(update={"unit_ids": ("a",)}),
                GraphExtraction(
                    extraction_id="foreign",
                    revision_id="other-revision",
                    unit_ids=("b",),
                ),
            )
        }
    )
    build = GraphBuildInput(
        spec=spec,
        units=(
            build.units[0],
            GraphUnitBinding(unit=foreign, revision_id="other-revision"),
        ),
        assertions=build.assertions,
        relations=build.relations,
    )
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(
            generation, (assertion,), (units[0], foreign), ExecutionContext()
        )
    assert err.value.code == "INTEGRITY_ERROR"


def test_cancelled_batch_leaves_native_graph_empty(tmp_path):
    from threading import Event

    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    cancel = Event()
    cancel.set()
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(
            generation, (assertion,), units, ExecutionContext(cancelled=cancel)
        )
    assert err.value.code == "CANCELLED"
    assert graph._connection.execute(
        "MATCH (e:Evidence) RETURN count(e)"
    ).get_all() == [[0]]


def test_immutable_unit_retry_rejects_changed_canonical_content(tmp_path):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    changed = units[0].model_copy(update={"text": "changed"})
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(generation, (), (changed,), ExecutionContext())
    assert err.value.code == "INTEGRITY_ERROR"


def test_failed_native_transaction_rolls_back_partial_generation_write(tmp_path):
    graph = LadybugGraphStore(tmp_path / "graph")
    with pytest.raises(RuntimeError):
        graph._transaction(
            [
                (
                    "CREATE (:Evidence {pk:'partial',generation_id:'g',evidence_id:'u'})",
                    {},
                ),
                (
                    "CREATE (:Evidence {pk:'partial',generation_id:'g',evidence_id:'duplicate'})",
                    {},
                ),
            ],
            ExecutionContext(),
        )
    assert graph._connection.execute(
        "MATCH (e:Evidence) RETURN count(e)"
    ).get_all() == [[0]]


def test_evidence_upsert_chunks_more_than_128_native_operations(tmp_path):
    units = tuple(_unit(f"unit-{index}") for index in range(260))
    spec = GraphGenerationSpec(
        generation_id="large",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=digest("registry"),
        workspace_id="workspace",
        decision_watermark=0,
        memberships=(
            GraphExtraction(
                extraction_id="extraction",
                revision_id="revision",
                unit_ids=tuple(unit.unit_id for unit in units),
            ),
        ),
    )
    build = GraphBuildInput(
        spec=spec,
        units=tuple(
            GraphUnitBinding(unit=unit, revision_id="revision") for unit in units
        ),
        assertions=(),
        relations=FrozenJsonMap(),
    )
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    lengths = []
    original = graph._transaction

    def capture(operations, ctx):
        lengths.append(len(operations))
        return original(operations, ctx)

    graph._transaction = capture
    graph.upsert_batch(generation, (), units, ExecutionContext())
    assert lengths == [128, 128, 4]
    assert graph._connection.execute(
        "MATCH (e:Evidence) RETURN count(e)"
    ).get_all() == [[260]]


def test_interrupted_drop_keeps_dropping_state_and_resumes_in_bounded_chunks(tmp_path):
    from threading import Event

    graph = LadybugGraphStore(tmp_path / "graph")
    spec = GraphGenerationSpec(
        generation_id="large",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=digest("registry"),
        workspace_id="workspace",
        decision_watermark=0,
    )
    generation = graph.begin_generation(spec)
    for index in range(260):
        graph._connection.execute(
            "CREATE (:Evidence {pk:$pk,generation_id:$gid,evidence_id:$id})",
            {
                "pk": graph._pk(generation, f"unit-{index}"),
                "gid": generation,
                "id": f"unit-{index}",
            },
        )
    cancel = Event()
    ctx = ExecutionContext(cancelled=cancel)
    lengths = []
    original = graph._transaction

    def capture(operations, execution):
        lengths.append(len(operations))
        result = original(operations, execution)
        cancel.set()
        return result

    graph._transaction = capture
    with pytest.raises(TychoraError) as err:
        graph.drop_generation(generation, ctx)
    assert err.value.code == "CANCELLED"
    assert lengths == [128]
    assert graph._read_state(generation)["dropping"] is True
    assert graph._connection.execute(
        "MATCH (e:Evidence) RETURN count(e)"
    ).get_all() == [[132]]
    with pytest.raises(TychoraError, match="removal is in progress"):
        graph.begin_generation(spec)
    with pytest.raises(TychoraError, match="removal is in progress"):
        graph.upsert_batch(generation, (), (), ExecutionContext())
    graph.close()
    graph = LadybugGraphStore(tmp_path / "graph")
    graph.drop_generation(generation, ExecutionContext())
    assert graph._read_state(generation) is None
    assert graph._connection.execute(
        "MATCH (e:Evidence) RETURN count(e)"
    ).get_all() == [[0]]


@pytest.mark.parametrize("table", ("GroundedIn", "Subject", "Object", "SupportedBy"))
def test_cross_generation_target_is_rejected_on_retry_and_seal(tmp_path, table):
    spec, build, units, assertion = _build(tmp_path)
    graph = LadybugGraphStore(tmp_path / "graph", build=build)
    generation = graph.begin_generation(spec)
    graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    if table == "GroundedIn":
        source_label, source_id, target_label, target_id = (
            "Entity",
            "subject",
            "Evidence",
            "a",
        )
    elif table == "SupportedBy":
        source_label, source_id, target_label, target_id = (
            "RelationAssertion",
            assertion.assertion_id,
            "Evidence",
            "a",
        )
    else:
        source_label, source_id, target_label, target_id = (
            "RelationAssertion",
            assertion.assertion_id,
            "Entity",
            ("subject" if table == "Subject" else "object"),
        )
    external_field = "evidence_id" if target_label == "Evidence" else "entity_id"
    graph._connection.execute(
        f"CREATE (:{target_label} {{pk:$pk,generation_id:'other',{external_field}:$id}})",
        {"pk": graph._pk("other", target_id), "id": target_id},
    )
    source_pk = graph._pk(generation, source_id)
    graph._connection.execute(
        f"MATCH (s:{source_label})-[r:{table}]->(t:{target_label}) WHERE s.pk=$pk AND t.pk=$target DELETE r",
        {"pk": source_pk, "target": graph._pk(generation, target_id)},
    )
    graph._connection.execute(
        f"MATCH (s:{source_label}),(t:{target_label}) WHERE s.pk=$source AND t.pk=$target CREATE (s)-[:{table}]->(t)",
        {"source": source_pk, "target": graph._pk("other", target_id)},
    )
    with pytest.raises(TychoraError) as retry:
        graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
    assert retry.value.code == "INTEGRITY_ERROR"
    with pytest.raises(TychoraError) as seal:
        graph.seal_generation(
            generation, GraphManifest.from_build(build), ExecutionContext()
        )
    assert seal.value.code == "INTEGRITY_ERROR"


def test_decision_materialization_cap_counts_rows_across_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_graph, "_MAX_DECISIONS", 3)
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=registry(), artifacts=reader())
    members = []
    workspace_id = ""
    for index in range(2):
        bundle = seed_bundle(f"Source {index}.", source_key=f"source-{index}.md")
        workspace_id = bundle.workspace.workspace_id
        unit = bundle.units[0]
        assertion = RelationshipAssertion.from_observation(
            AssertionObservation(
                observation_key=f"observation-{index}",
                revision_id=bundle.revision.revision_id,
                extraction_id=bundle.extraction.extraction_id,
                predicate="supports@1",
                subject=RelationshipEndpoint(
                    entity_id=f"subject-{index}", unit_id=unit.unit_id, kind="manual@1"
                ),
                object=RelationshipEndpoint(
                    entity_id=f"object-{index}", unit_id=unit.unit_id, kind="manual@1"
                ),
                origin_unit_id=unit.unit_id,
                origin_locator=unit.locators[0],
                required_support_ids=frozenset({unit.unit_id}),
                method="native",
                status="recorded",
                confidence=1.0,
                derivation_fingerprint=digest("fixture", index),
            )
        )
        root = artifact(encode_assertions((assertion,)), "application/x-ndjson")
        bundle = bundle.model_copy(
            update={
                "artifacts": (*bundle.artifacts, root),
                "extraction": bundle.extraction.model_copy(
                    update={"assertions_artifact_sha": root.sha256}
                ),
            }
        )
        corpus.commit_extraction(bundle)
        for _ in range(2):
            corpus.record_assertion_decision(
                assertion.assertion_id,
                root.sha256,
                assertion.assertion_id,
                "confirmed",
                "reviewer",
            )
        members.append(
            GraphExtraction(
                extraction_id=bundle.extraction.extraction_id,
                revision_id=bundle.revision.revision_id,
                assertion_root=root,
                unit_ids=(unit.unit_id,),
            )
        )
    spec = GraphGenerationSpec(
        generation_id="multi",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=registry().registry_digest,
        workspace_id=workspace_id,
        decision_watermark=4,
        memberships=tuple(members),
    )
    with pytest.raises(TychoraError) as err:
        read_graph_build(corpus, spec, ExecutionContext())
    assert err.value.code == "BUDGET_TOO_SMALL"
    assert not db.in_transaction
    db.close()
