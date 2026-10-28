"""Policy eligibility on sealed native graph traversal."""

import io

import pytest

from tests.helpers.eligibility import synthetic_graph_scope
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.configuration.graph_policy import resolve_graph_traversal_policy
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit, TextLocator
from tychora.contracts.graph import (
    GraphAssertionBinding,
    GraphBudget,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphManifest,
    GraphPredicatePolicy,
    GraphTraversalPolicy,
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


def _unit(uid: str) -> EvidenceUnit:
    return EvidenceUnit(
        unit_id=uid,
        extraction_id="extraction",
        parent_id=None,
        ordinal=0,
        unit_type="symbol",
        native_ref=uid,
        boundary_key="file",
        text=uid,
        text_sha=digest("text", uid),
        locators=(
            TextLocator(artifact_ref="text", native_ref=uid, char_start=0, char_end=1),
        ),
    )


def _assertion(subject: str, target: str, supports: set[str]) -> RelationshipAssertion:
    return RelationshipAssertion.from_observation(
        AssertionObservation(
            observation_key=f"{subject}-{target}",
            revision_id="revision",
            extraction_id="extraction",
            predicate="references@1",
            subject=RelationshipEndpoint(
                entity_id=subject, unit_id=subject, kind="symbol@1"
            ),
            object=RelationshipEndpoint(
                entity_id=target, unit_id=target, kind="symbol@1"
            ),
            origin_unit_id=subject,
            origin_locator=_unit(subject).locators[0],
            required_support_ids=frozenset(supports),
            method="native",
            status="recorded",
            confidence=1.0,
            derivation_fingerprint=digest("fixture"),
        )
    )


@pytest.fixture
def graph_fixture(tmp_path):
    units = tuple(_unit(uid) for uid in ("a", "b", "hidden"))
    assertion = _assertion("a", "b", {"a", "b", "hidden"})
    root = LocalArtifacts(tmp_path / "artifacts").put_stream(
        io.BytesIO(encode_assertions((assertion,))), "application/x-ndjson"
    )
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
                unit_ids=tuple(u.unit_id for u in units),
            ),
        ),
    )
    build = GraphBuildInput(
        spec=spec,
        units=tuple(GraphUnitBinding(unit=u, revision_id="revision") for u in units),
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
                    "methods": ["native"],
                    "revision_binding": "same_revision",
                    "cardinality": "many_to_many",
                    "self_links": False,
                }
            }
        ),
    )
    graph = LadybugGraphStore(
        tmp_path / "graph",
        build=build,
        traversal_policy=GraphTraversalPolicy(
            predicates=(
                GraphPredicatePolicy(
                    predicate="references@1", directions=("outgoing", "incoming")
                ),
            ),
            semantic=True,
        ),
    )
    graph.begin_generation(spec)
    graph.upsert_batch(spec.generation_id, (assertion,), units, ExecutionContext())
    receipt = graph.seal_generation(
        spec.generation_id, GraphManifest.from_build(build), ExecutionContext()
    )

    class Fixture:
        ctx = ExecutionContext()

        def __init__(self, graph, assertion):
            self.graph = graph
            self.assertion = assertion

        @staticmethod
        def budget():
            return GraphBudget()

        @staticmethod
        def view(*, context, candidates, structural, blocked=frozenset()):
            return synthetic_graph_scope(
                source=candidates,
                context=context,
                structural=structural,
                blocked=blocked,
                generation_id="generation",
                family_id=spec.family_id,
                config_sha=spec.config_sha,
                decision_watermark=spec.decision_watermark,
                manifest_digest=receipt.manifest_digest,
            )

    fixture = Fixture(graph, assertion)
    yield fixture
    graph.close()


def test_hidden_support_blocks_visible_endpoints(graph_fixture):
    f = graph_fixture
    view = f.view(context={"a", "b"}, candidates={"a", "b"}, structural=set())
    page = f.graph.expand(view, ("a",), f.budget(), f.ctx)
    assert "b" not in {c.unit_id for c in page.candidates}


def test_full_support_allows_visible_target(graph_fixture):
    f = graph_fixture
    view = f.view(context={"a", "b", "hidden"}, candidates={"a", "b"}, structural=set())
    page = f.graph.expand(view, ("a",), f.budget(), f.ctx)
    assert [c.unit_id for c in page.candidates] == ["b"]


def test_resolved_table_continuation_policy_only_narrows_status(tmp_path):
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.retrieval]\ntable_continuations="confirmed_only"\n'
        "[configuration.retrieval.graph]\nsemantic=true\n"
        'predicates=["continues_table@1"]\n'
    )
    config = resolve_config(workspace, "test", {})
    policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
    assert policy.predicates[0].statuses == ("confirmed",)
    workspace.write_text(workspace.read_text().replace("confirmed_only", "none"))
    config = resolve_config(workspace, "test", {})
    policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
    assert policy.predicates == ()


@pytest.mark.parametrize(
    "limit",
    (
        "max_seeds",
        "max_new_units",
        "max_rows",
        "max_inspection_edges",
        "max_inspection_hops",
    ),
)
def test_graph_budget_rejects_values_above_hard_caps(limit):
    caps = {
        "max_seeds": 5,
        "max_new_units": 32,
        "max_rows": 2000,
        "max_inspection_edges": 100,
        "max_inspection_hops": 2,
    }
    with pytest.raises(ValueError):
        type.__call__(GraphBudget, **{limit: caps[limit] + 1})


def test_disabled_semantic_still_rejects_unknown_configured_predicate(tmp_path):
    workspace = tmp_path / "workspace.toml"
    workspace.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    config = resolve_config(workspace, "test", {})
    registry = Registry.from_config(config)
    graph = config.values.retrieval.graph.model_copy(
        update={"predicates": ("missing@1",)}
    )
    retrieval = config.values.retrieval.model_copy(update={"graph": graph})
    changed = config.model_copy(
        update={"values": config.values.model_copy(update={"retrieval": retrieval})}
    )
    with pytest.raises(TychoraError) as error:
        resolve_graph_traversal_policy(changed, registry)
    assert error.value.code == "INVALID_REQUEST"
