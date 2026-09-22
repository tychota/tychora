"""Short canonical read for detached native graph construction."""

from __future__ import annotations

from typing import Literal, NoReturn, cast

from tychora.adapters.files.assertions import iter_verified_assertions
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    GraphAssertionBinding,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphUnitBinding,
)
from tychora.ports.execution import ExecutionContext

_MAX_UNITS = 20_000
_MAX_ASSERTIONS = 20_000
_MAX_DECISIONS = 20_000


def _fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


def read_graph_build(
    corpus: CorpusStore, spec: GraphGenerationSpec, ctx: ExecutionContext
) -> GraphBuildInput:
    """Detach one pinned canonical view on the corpus owner lane.

    The short SQL read contains only row identities and decisions. Artifact
    verification and Ladybug projection happen after the transaction closes.
    """
    db = corpus.db
    if db.in_transaction:
        _fail("Graph build requires its own short canonical read")
    if spec.registry_digest != corpus.registry.registry_digest:
        _fail("Graph registry differs from canonical configuration")
    selected: list[tuple[GraphExtraction, ArtifactRef | None]] = []
    unit_bindings: list[GraphUnitBinding] = []
    decisions: dict[str, tuple[str, str, str, int, str]] = {}
    try:
        db.execute("BEGIN")
        maximum = db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]
        if spec.decision_watermark > maximum:
            _fail("Graph decision watermark exceeds canonical history")
        unit_count = 0
        for member in spec.memberships:
            ctx.checkpoint()
            row = db.execute(
                "SELECT e.revision_id,e.assertions_artifact_sha,c.workspace_id "
                "FROM extractions e JOIN revisions r USING(revision_id) "
                "JOIN sources s USING(source_id) JOIN collections c USING(collection_id) "
                "WHERE e.extraction_id=?",
                (member.extraction_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != member.revision_id
                or row[2] != spec.workspace_id
            ):
                _fail("Graph extraction revision is not registered")
            root = None
            if row[1] is not None:
                artifact_row = db.execute(
                    "SELECT artifact_sha,size_bytes,media_type,storage_key,payload_state "
                    "FROM artifacts WHERE artifact_sha=?",
                    (row[1],),
                ).fetchone()
                if artifact_row is None or artifact_row[4] != "present":
                    _fail("Graph assertion root is unavailable")
                root = ArtifactRef(
                    sha256=artifact_row[0],
                    size_bytes=artifact_row[1],
                    media_type=artifact_row[2],
                    storage_key=artifact_row[3],
                )
            if root != member.assertion_root:
                _fail("Graph assertion root differs from registered extraction")
            count = db.execute(
                "SELECT COUNT(*) FROM evidence_units WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchone()[0]
            unit_count += count
            if unit_count > _MAX_UNITS:
                raise TychoraError(
                    "BUDGET_TOO_SMALL", "Graph unit materialization limit exceeded"
                )
            rows = db.execute(
                "SELECT unit_id,declared_review_state FROM evidence_units "
                "WHERE extraction_id=? ORDER BY unit_id",
                (member.extraction_id,),
            ).fetchall()
            if tuple(row[0] for row in rows) != tuple(sorted(member.unit_ids)):
                _fail("Graph unit membership differs from canonical extraction")
            units = corpus._hydrate(tuple(row[0] for row in rows))
            for unit, (_, declared) in zip(units, rows, strict=True):
                ctx.checkpoint()
                # Review events are current policy overlays, not immutable content.
                original = unit.model_copy(update={"review_state": declared})
                unit_bindings.append(
                    GraphUnitBinding(unit=original, revision_id=member.revision_id)
                )
            selected.append((member, root))
            if root is not None:
                count = db.execute(
                    "SELECT COUNT(*) FROM assertion_decisions "
                    "WHERE provenance_artifact_sha=? AND decision_id<=?",
                    (root.sha256, spec.decision_watermark),
                ).fetchone()[0]
                if count + len(decisions) > _MAX_DECISIONS:
                    raise TychoraError(
                        "BUDGET_TOO_SMALL",
                        "Graph decision materialization limit exceeded",
                    )
                rows = db.execute(
                    "SELECT assertion_id,provenance_artifact_sha,provenance_record_id,state,decision_id,actor_id "
                    "FROM assertion_decisions WHERE provenance_artifact_sha=? "
                    "AND decision_id<=? ORDER BY decision_id",
                    (root.sha256, spec.decision_watermark),
                ).fetchall()
                for (
                    assertion_id,
                    artifact_sha,
                    record_id,
                    state,
                    decision_id,
                    actor_id,
                ) in rows:
                    decisions[assertion_id] = (
                        artifact_sha,
                        record_id,
                        state,
                        decision_id,
                        actor_id,
                    )
        db.commit()
    except BaseException:
        db.rollback()
        raise

    assertions: list[GraphAssertionBinding] = []
    for member, root in selected:
        if root is None:
            continue
        for item in iter_verified_assertions(
            root, corpus.artifacts, expected_extraction_id=member.extraction_id
        ):
            ctx.checkpoint()
            if item.assertion is None or item.record_id != item.assertion_id:
                _fail("Graph assertion record is not a canonical typed observation")
            assertion = item.assertion
            assert assertion is not None
            if assertion.revision_id != member.revision_id:
                _fail("Graph assertion revision differs from canonical extraction")
            decision = decisions.get(assertion.assertion_id)
            if decision is not None and decision[:2] != (root.sha256, item.record_id):
                _fail("Graph assertion decision provenance differs")
            assertions.append(
                GraphAssertionBinding(
                    assertion=assertion,
                    artifact=root,
                    record_id=item.record_id,
                    status_at_watermark=cast(
                        Literal["recorded", "proposed", "confirmed", "rejected"],
                        decision[2],
                    )
                    if decision
                    else assertion.status,
                    decision_id=decision[3] if decision else None,
                    decision_actor_id=decision[4] if decision else None,
                )
            )
            if len(assertions) > _MAX_ASSERTIONS:
                raise TychoraError(
                    "BUDGET_TOO_SMALL", "Graph assertion materialization limit exceeded"
                )
    return GraphBuildInput(
        spec=spec,
        units=tuple(unit_bindings),
        assertions=tuple(assertions),
        relations=corpus.registry.relations,
    )
