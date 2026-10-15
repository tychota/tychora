"""Declared graph decisions and hidden support traverse a published generation."""

from hashlib import sha256
from io import BytesIO

import pytest

from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.corpus import reader, seed_bundle
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.ladybug.traversal import resolve_graph_traversal_policy
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import QueryService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.corpus import LocatorRow, ReviewTarget
from tychora.contracts.evidence import EvidenceUnit, TextLocator
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import canonical_bytes, digest, unit_id
from tychora.contracts.operations import ConfigurationVariant, EvaluationRequest
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
    encode_assertions,
)
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.evaluation.runner import EvaluationService
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


def _declared_bundle(owner):
    lines = ("SeedAlpha", "AllowedBeta", "HiddenParent", "BlockedGamma")
    source = "\n".join(lines)
    bundle = seed_bundle(source, source_key="relations.txt")
    for ref in bundle.artifacts:
        with reader().open_verified(ref) as stream:
            owner.artifacts.put_stream(stream, ref.media_type)
    units = []
    locators = []
    offset = 0
    for ordinal, value in enumerate(lines):
        uid = unit_id(
            bundle.extraction.extraction_id,
            f"line:{ordinal}",
            ordinal,
            sha256(value.encode()).hexdigest(),
        )
        locator = TextLocator(
            artifact_ref=bundle.revision.original_sha,
            native_ref="relations.txt",
            char_start=offset,
            char_end=offset + len(value),
        )
        units.append(
            EvidenceUnit(
                unit_id=uid,
                extraction_id=bundle.extraction.extraction_id,
                parent_id=None,
                ordinal=ordinal,
                unit_type="paragraph",
                native_ref=f"line:{ordinal}",
                boundary_key=f"line:{ordinal}",
                text=value,
                text_sha=sha256(value.encode()).hexdigest(),
                locators=(locator,),
            )
        )
        locators.append(
            LocatorRow(
                locator_id=digest("locator-v1", uid, 0),
                unit_id=uid,
                fragment_ordinal=0,
                locator=locator,
            )
        )
        offset += len(value) + 1
    # The last target's declared parent is the hidden source unit.
    units[3] = units[3].model_copy(update={"parent_id": units[2].unit_id})
    a, b, hidden, c = units

    def relation(key, target, predicate, supports):
        return RelationshipAssertion.from_observation(
            AssertionObservation(
                observation_key=key,
                revision_id=bundle.revision.revision_id,
                extraction_id=bundle.extraction.extraction_id,
                predicate=predicate,
                subject=RelationshipEndpoint(
                    entity_id=a.unit_id, unit_id=a.unit_id, kind="manual@1"
                ),
                object=RelationshipEndpoint(
                    entity_id=target.unit_id, unit_id=target.unit_id, kind="manual@1"
                ),
                origin_unit_id=a.unit_id,
                origin_locator=a.locators[0],
                required_support_ids=frozenset(supports),
                method="native",
                status="recorded",
                confidence=1.0,
                derivation_fingerprint=digest("synthetic-graph-fixture-v1"),
            )
        )

    allowed = relation("allowed", b, "supports@1", {a.unit_id, b.unit_id})
    rejected = relation("rejected", c, "contradicts@1", {a.unit_id, c.unit_id})
    needs_parent = relation(
        "requires-parent",
        c,
        "supports@1",
        {a.unit_id, c.unit_id, hidden.unit_id},
    )
    assertion_ref = owner.artifacts.put_stream(
        BytesIO(encode_assertions((allowed, rejected, needs_parent))),
        "application/x-ndjson",
    )
    created_at = owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0]
    bundle = bundle.model_copy(
        update={
            "workspace": bundle.workspace.model_copy(
                update={
                    "workspace_id": owner.workspace_id,
                    "created_at": created_at,
                }
            ),
            "collection": bundle.collection.model_copy(
                update={
                    "workspace_id": owner.workspace_id,
                }
            ),
            "artifacts": (*bundle.artifacts, assertion_ref),
            "extraction": bundle.extraction.model_copy(
                update={
                    "assertions_artifact_sha": assertion_ref.sha256,
                }
            ),
            "units": tuple(units),
            "locators": tuple(locators),
            "chunks": (),
        }
    )
    return bundle, (a, b, hidden, c), (allowed, rejected, needs_parent), assertion_ref


@pytest.mark.asyncio
async def test_published_decisions_and_hidden_parent_support_are_excluded(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
        "[configuration.limits]\nmax_tokens=30000\n"
        "[configuration.retrieval.graph]\nsemantic=true\n"
        'predicates=["supports@1","contradicts@1"]\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="synthetic-reader",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        ctx = ExecutionContext()
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                'VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (COLLECTION, owner.workspace_id, "manuals", "Manuals"),
            ),
            ctx,
        )
        (
            bundle,
            (a, b, hidden, c),
            (allowed, rejected, needs_parent),
            root,
        ) = await owner.run_blocking(lambda: _declared_bundle(owner), ctx)
        graphs = {}

        def graph_factory(build):
            graph = LadybugGraphStore(
                tmp_path / "graphs" / build.spec.generation_id,
                build=build,
                traversal_policy=resolve_graph_traversal_policy(
                    config, owner.store.registry
                ),
            )
            owner.register_close(graph.close)
            graphs[build.spec.generation_id] = graph
            return graph

        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=SyntheticCounter(),
            graph_factory=graph_factory,
        )
        config_ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(
                BytesIO(
                    canonical_bytes(
                        {
                            "type": "registry",
                            "schema_version": "1",
                            "data": config.model_dump(mode="json"),
                        }
                    )
                ),
                "application/json",
            ),
            ctx,
        )

        def prepare():
            jobs = JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            )
            job = jobs.accept(
                "add", scope, None, {"source": "relations.txt"}, config_ref
            )
            jobs.start(job.id)
            prepared = coordinator.prepare(
                bundle, owner.store.active_snapshot_id(), job, ctx
            )
            owner.store.record_assertion_decision(
                rejected.assertion_id,
                root.sha256,
                rejected.assertion_id,
                "rejected",
                "synthetic-reviewer",
            )
            return prepared

        prepared = await owner.run_blocking(prepare, ctx)
        plan = await owner.run_blocking(
            lambda: coordinator.finalize(prepared, (), ctx), ctx
        )
        snapshot_id = await owner.run_blocking(
            lambda: owner.store.publish_snapshot(plan), ctx
        )
        snapshot = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ctx
        )
        slot = next(slot for slot in snapshot.slots if slot.slot == "graph")
        graph_batch = slot.batches[0]
        assert isinstance(graph_batch.receipt, GraphBatchReceipt)
        assert graph_batch.receipt.assertion_count == 3
        assert graph_batch.graph_manifest is not None
        graph = graphs[graph_batch.graph_manifest.spec.generation_id]
        query = QueryService(owner, output_counter=SyntheticCounter(), graph=graph)
        request = QueryRequest.from_mapping(
            {
                "question": "SeedAlpha",
                "modalities": ["lexical"],
                "mode": "lookup",
                "budgets": {"max_tokens": 30000},
            }
        )
        control = (await query.search(request, scope, ctx)).result
        assert control.checks.status == "not_evaluated"
        control_ids = {key for path in control.relations for key in path.assertion_ids}
        assert allowed.assertion_id in control_ids
        assert needs_parent.assertion_id in control_ids
        assert rejected.assertion_id not in control_ids
        allowed_path = next(
            path
            for path in control.relations
            if path.assertion_ids == (allowed.assertion_id,)
        )
        assert allowed_path.unit_ids == (a.unit_id, b.unit_id)
        assert allowed_path.predicates == ("supports@1",)
        assert allowed_path.provenance["status"] == "recorded"
        assert allowed_path.provenance["confidence"] == 1.0
        tight = (
            await query.search(
                QueryRequest.from_mapping(
                    {
                        "question": "SeedAlpha",
                        "modalities": ["lexical"],
                        "mode": "lookup",
                        "budgets": {"max_tokens": 5500},
                    }
                ),
                scope,
                ctx,
            )
        ).result
        retained = {
            uid for segment in tight.segments for uid in segment.matched_unit_ids
        }
        assert len(tight.segments) == 1
        assert "output_omitted:owner" in tight.warnings
        assert len(tight.relations) == 1
        assert all(set(path.unit_ids) & retained for path in tight.relations)
        assert {
            aid for path in tight.relations for aid in path.assertion_ids
        } < control_ids
        no_graph = QueryService(owner, output_counter=SyntheticCounter())
        assert not (await no_graph.search(request, scope, ctx)).result.relations
        exact = QueryRequest.from_mapping(
            {
                "question": "SeedAlpha",
                "modalities": ["lexical"],
                "mode": "lookup",
                "budgets": {"max_tokens": 30000},
                "exact_only": True,
            }
        )
        assert not (await query.search(exact, scope, ctx)).result.relations

        await owner.run_blocking(
            lambda: owner.store.record_review(
                ReviewTarget(unit_id=hidden.unit_id), "rejected", "synthetic-reviewer"
            ),
            ctx,
        )
        hidden_result = (await query.search(request, scope, ctx)).result
        hidden_ids = {
            key for path in hidden_result.relations for key in path.assertion_ids
        }
        assert allowed.assertion_id in hidden_ids
        assert needs_parent.assertion_id not in hidden_ids
        assert rejected.assertion_id not in hidden_ids
        assert all(
            hidden.unit_id not in (*path.unit_ids, *path.support_unit_ids)
            for path in hidden_result.relations
        )
        assert all(
            hidden.unit_id
            not in (*segment.matched_unit_ids, *segment.context_only_unit_ids)
            for segment in hidden_result.segments
        )

        cases = tuple(
            {
                "case_id": case_id,
                "family_key": case_id,
                "split": "test",
                "benchmark": "integrity",
                "label_provenance": "declared source lines, assertion observations and canonical decision events",
                "query": {
                    "question": "SeedAlpha",
                    "modalities": ["lexical"],
                    "mode": "lookup",
                    "budgets": {"max_tokens": 30000},
                },
                "expected_source_ids": [bundle.source.source_id],
                "expected_revision_ids": [bundle.revision.revision_id],
                "expected_unit_ids": [b.unit_id],
                "forbidden_unit_ids": [c.unit_id]
                if case_id == "conflicting-assertion"
                else [hidden.unit_id, c.unit_id],
                "expected_paths": [[allowed.assertion_id]],
                "expected_edge_ids": [allowed.assertion_id],
            }
            for case_id in ("conflicting-assertion", "hidden-parent-support")
        )
        case_bytes = b"".join(canonical_bytes(case) + b"\n" for case in cases)
        manifest = canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": "synthetic-graph-v1",
                "name": "Synthetic decision and support graph",
                "version_sha": sha256(case_bytes).hexdigest(),
                "case_count": 2,
            }
        )
        jobs = await owner.run_blocking(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ctx,
        )
        evaluation = EvaluationService(owner, query, jobs)
        await evaluation.register_dataset_bytes(manifest, case_bytes, scope, ctx)
        handle = await evaluation.run(
            EvaluationRequest(
                dataset_id="synthetic-graph-v1",
                split="test",
                snapshot_id=snapshot_id,
                variants=(
                    ConfigurationVariant.from_mapping(
                        {
                            "name": "lexical",
                            "profile": "test",
                            "overrides": {"channels": ["lexical"]},
                        }
                    ),
                    ConfigurationVariant.from_mapping(
                        {
                            "name": "semantic",
                            "profile": "test",
                            "overrides": {"channels": ["lexical", "semantic_graph"]},
                        }
                    ),
                ),
            ),
            scope,
            ctx,
        )
        assert handle.state == "succeeded"
        report = await evaluation.get(handle.id, scope, ctx)
        for case_id in ("conflicting-assertion", "hidden-parent-support"):
            lexical, semantic = report["cases"][case_id]["runs"]
            assert lexical["result"]["relations"] == []
            assert any(
                path["assertion_ids"] == [allowed.assertion_id]
                for path in semantic["result"]["relations"]
            )
            values = {
                item["name"]: item["value"] for item in semantic["metrics"]["metrics"]
            }
            assert values["path_recall"] == 1.0
            assert values["unit_recall"] == 1.0
            assert values["forbidden_leakage"] == 0.0
            assert values["locator_validity"] == 1.0
