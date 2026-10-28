"""Authorized response attachment and finite exact delivery fitting."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from tychora.configuration.compatibility import validate_historical_record
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap, thaw_json
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    Citation,
    Coverage,
    CoverageGap,
    DecisionRequirement,
    Omission,
    QueryResult,
    RelationChecks,
    RelationPath,
    Segment,
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.graph import AssertionQuery, GraphBudget, GraphScope
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.requests import (
    BudgetContract,
    QueryRequest,
    RenderTarget,
    RequestScope,
)
from tychora.contracts.requirements import ResponseRequirement, active_requirements
from tychora.formatting.budget import measure_payloads
from tychora.formatting.render import OutputProfile, render_unchecked
from tychora.ports.tokens import TokenCounter
from tychora.retrieval.segments import Selection

if TYPE_CHECKING:
    from tychora.contracts.eligibility import EligibilityView
    from tychora.ports.execution import ExecutionContext
    from tychora.ports.graph import GraphStore
    from tychora.ports.repositories import ReadTransaction
    from tychora.retrieval.attempt import EligibilityAttempt
    from tychora.retrieval.rerank import RankedEvidence, RerankFacts


STUB_IDENTITY_FIELDS = ("definition_key", "qualified_name", "relative_path")
_STUB_FIELD_CHARS = 512


def budget_stub(record: StructuredRecord) -> StructuredRecord:
    """The record trimmed by the output budget; a code symbol keeps what names it."""
    value: dict[str, object] = {"omitted_budget": True}
    if record.record_type == "code_symbol":
        for name in STUB_IDENTITY_FIELDS:
            field = record.value.get(name)
            if isinstance(field, str) and len(field) <= _STUB_FIELD_CHARS:
                value[name] = field
    return record.model_copy(update={"value": FrozenJsonMap(value)})


@dataclass(frozen=True)
class ResponseContext:
    """Pinned, already authorized producer inputs; no stores enter rendering."""

    run_id: str
    snapshot_id: str
    scope: RequestScope
    counter: TokenCounter
    profiles: tuple[OutputProfile, ...] = (OutputProfile(),)
    eligible_unit_ids: frozenset[str] = frozenset()
    eligible_asset_ids: frozenset[str] = frozenset()
    relations: tuple[RelationPath, ...] = ()
    visuals: tuple[VisualEvidence, ...] = ()
    coverage: Coverage = field(default_factory=lambda: Coverage(status="complete"))
    warnings: tuple[str, ...] = ()
    decision_requirements: tuple[DecisionRequirement, ...] = ()
    trace: FrozenJsonMap | None = None
    required_lanes: tuple[str, ...] = ()
    required_kinds: tuple[str, ...] = ()
    required_record_schemas: tuple[str, ...] = ()
    requirements: tuple[ResponseRequirement, ...] = ()
    checks_required: bool = False
    max_tokens: int = 6000
    max_response_bytes: int = 1048576
    delivery_serializer: Callable[[QueryResult], bytes] | None = None
    delivery_serialization: str | None = None


@dataclass(frozen=True)
class DeliveredResult:
    result: QueryResult
    payloads: tuple[bytes, ...]
    token_count: int
    byte_count: int


def _target(profile: OutputProfile, serialization: str | None = None) -> RenderTarget:
    return RenderTarget(
        renderer=profile.renderer,
        profile_digest=digest("output-profile-v1", profile.model_dump(mode="json")),
        schema_digest=digest("output-schema-v1", profile.renderer),
        serialization=serialization
        or (
            "canonical-json-utf8"
            if profile.renderer.endswith("json@1")
            or profile.renderer == "hierarchical-evidence@1"
            else "utf8-text"
        ),
    )


def _unique_citations(
    segments: tuple[Segment, ...], visuals: tuple[VisualEvidence, ...]
) -> tuple[Citation, ...]:
    seen: set[bytes] = set()
    result: list[Citation] = []
    for citation in (item for segment in segments for item in segment.citations):
        key = citation.model_dump_json().encode()
        if key not in seen:
            result.append(citation)
            seen.add(key)
    for citation in (item for visual in visuals for item in visual.citations):
        key = citation.model_dump_json().encode()
        if key not in seen:
            result.append(citation)
            seen.add(key)
    return tuple(result)


class ResponseAssembler:
    def assemble(
        self,
        selection: Selection,
        records: tuple[StructuredRecord, ...],
        checks: RelationChecks,
        request: QueryRequest,
        ctx: ResponseContext,
    ) -> DeliveredResult:
        if ctx.counter.tokenizer_id != "cl100k_base" or not ctx.profiles:
            raise TychoraError(
                "INVALID_REQUEST", "Output counter or target selection is invalid."
            )
        if (ctx.delivery_serializer is None) != (ctx.delivery_serialization is None):
            raise TychoraError(
                "INVALID_REQUEST", "Delivery serializer and target differ."
            )
        if (
            ctx.delivery_serializer is not None
            and ctx.profiles[0].renderer != "canonical-json@1"
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Transport delivery requires canonical first profile.",
            )
        targets = tuple(
            _target(profile, ctx.delivery_serialization if index == 0 else None)
            for index, profile in enumerate(ctx.profiles)
        )
        if request.render_targets and request.render_targets != targets:
            raise TychoraError("INVALID_REQUEST", "Output target fingerprints differ.")
        cap_tokens = min(request.budgets.max_tokens, ctx.max_tokens)
        cap_bytes = min(request.budgets.max_response_bytes, ctx.max_response_bytes)
        if cap_tokens < 1 or cap_bytes < 1:
            raise TychoraError("INVALID_REQUEST", "Output cap must be positive.")
        budget = BudgetContract(
            max_tokens=cap_tokens,
            tokenizer_id=ctx.counter.tokenizer_id,
            render_targets=targets,
            max_response_bytes=cap_bytes,
        )
        segments = list(selection.segments)
        visuals = list(ctx.visuals)
        active = active_requirements(ctx.requirements, request)
        required_schemas = set(ctx.required_record_schemas) | {
            item.value for item in active if item.target == "record_schema"
        }
        initial_lanes = {segment.lane for segment in segments}
        initial_kinds = {segment.kind for segment in segments}
        initial_schemas = {record.schema_id for record in records}
        owner_ids = {uid for segment in segments for uid in segment.matched_unit_ids}
        for record in records:
            if (
                record.unit_id not in owner_ids
                or not record.dependencies <= ctx.eligible_unit_ids
                or record.unit_id not in record.dependencies
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Structured record ownership or dependencies differ.",
                )
        for visual in visuals:
            if any(
                visual.asset_id not in ctx.eligible_asset_ids
                or visual.asset_id not in proxy.dependencies
                or not (proxy.dependencies - {visual.asset_id}) <= ctx.eligible_unit_ids
                for proxy in visual.proxies
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Visual proxy dependencies differ."
                )
        for path in ctx.relations:
            if (
                not set(path.unit_ids) <= ctx.eligible_unit_ids
                or not path.support_unit_ids <= ctx.eligible_unit_ids
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Relation path eligibility differs."
                )
        for index, segment in enumerate(segments):
            attached = tuple(
                record
                for record in records
                if record.unit_id in segment.matched_unit_ids
            )
            segments[index] = segment.model_copy(
                update={"structured_records": attached}
            )
        trace = ctx.trace if request.trace else None
        omitted: set[str] = set()

        def materialize() -> QueryResult:
            kept = tuple(segments)
            kept_visuals = tuple(visuals)
            retained_owners = {uid for item in kept for uid in item.matched_unit_ids}
            kept_relations = tuple(
                path for path in ctx.relations if set(path.unit_ids) & retained_owners
            )
            gaps: list[CoverageGap] = []
            for lane in ctx.required_lanes:
                if not any(item.lane == lane for item in kept):
                    gaps.append(
                        CoverageGap(
                            code="required_lane",
                            channel=lane,
                            message="output_budget_omission"
                            if lane in initial_lanes
                            else "not_selected_in_bounded_search",
                        )
                    )
            for kind in ctx.required_kinds:
                if not any(item.kind == kind for item in kept):
                    gaps.append(
                        CoverageGap(
                            code="required_kind",
                            channel=kind,
                            message="output_budget_omission"
                            if kind in initial_kinds
                            else "not_selected_in_bounded_search",
                        )
                    )
            for schema in ctx.required_record_schemas:
                if not any(
                    record.schema_id == schema and "omitted_budget" not in record.value
                    for item in kept
                    for record in item.structured_records
                ):
                    gaps.append(
                        CoverageGap(
                            code="required_record_schema",
                            channel=schema,
                            message="output_budget_omission"
                            if schema in initial_schemas
                            else "not_selected_in_bounded_search",
                        )
                    )
            for requirement in active:
                if requirement.target == "lane":
                    before = sum(
                        item.lane == requirement.value for item in selection.segments
                    )
                    after = sum(item.lane == requirement.value for item in kept)
                elif requirement.target == "kind":
                    before = sum(
                        item.kind == requirement.value for item in selection.segments
                    )
                    after = sum(item.kind == requirement.value for item in kept)
                else:
                    before = sum(
                        record.schema_id == requirement.value for record in records
                    )
                    after = sum(
                        record.schema_id == requirement.value
                        and "omitted_budget" not in record.value
                        for item in kept
                        for record in item.structured_records
                    )
                if after < requirement.minimum:
                    gaps.append(
                        CoverageGap(
                            code=requirement.requirement_id,
                            channel=requirement.value,
                            message="output_budget_omission"
                            if before >= requirement.minimum
                            else "not_selected_in_bounded_search",
                        )
                    )
            if "owner" in omitted or "visual" in omitted:
                gaps.append(
                    CoverageGap(
                        code="output_budget_omission",
                        channel="delivery",
                        message="selected_evidence_omitted",
                    )
                )
            coverage = _delivered_coverage(
                ctx.coverage,
                gaps,
                ctx.checks_required and checks.status != "complete",
            )
            kept_assertions = tuple(
                path
                for path in checks.assertions
                if set(path.unit_ids) & retained_owners
            )
            kept_checks = checks.model_copy(
                update={
                    "assertions": kept_assertions,
                    "assertion_ids": tuple(
                        dict.fromkeys(
                            aid
                            for path in kept_assertions
                            for aid in path.assertion_ids
                        )
                    )
                    if checks.assertions
                    else checks.assertion_ids,
                    "decisions": tuple(
                        item
                        for item in checks.decisions
                        if not item.unit_ids or set(item.unit_ids) & retained_owners
                    ),
                }
            )
            decisions = tuple(
                item
                for item in (*ctx.decision_requirements, *kept_checks.decisions)
                if not item.unit_ids or set(item.unit_ids) & retained_owners
            )
            warnings = (
                *ctx.warnings,
                *(("relation_checks_partial",) if checks.status == "partial" else ()),
                *(f"output_omitted:{code}" for code in sorted(omitted)),
            )
            return QueryResult(
                run_id=ctx.run_id,
                snapshot_id=ctx.snapshot_id,
                applied_scope=ctx.scope,
                segments=kept,
                visuals=kept_visuals,
                relations=kept_relations,
                citations=_unique_citations(kept, kept_visuals),
                coverage=coverage,
                checks=kept_checks,
                coverage_gaps=tuple(gaps),
                decision_requirements=decisions,
                warnings=warnings,
                budget_contract=budget,
                trace=trace,
            )

        def fit() -> DeliveredResult | None:
            result = materialize()
            payloads = tuple(
                ctx.delivery_serializer(result)
                if index == 0 and ctx.delivery_serializer is not None
                else render_unchecked(result, profile)
                for index, profile in enumerate(ctx.profiles)
            )
            measured = measure_payloads(payloads, ctx.counter)
            token_count, byte_count = measured.tokens, measured.bytes
            if token_count <= cap_tokens and byte_count <= cap_bytes:
                return DeliveredResult(result, payloads, token_count, byte_count)
            return None

        if (delivered := fit()) is not None:
            return delivered
        if trace is not None:
            trace = None
            omitted.add("trace")
            if (delivered := fit()) is not None:
                return delivered
        for index in range(len(segments) - 1, -1, -1):
            segment = segments[index]
            for record in sorted(
                segment.structured_records, key=lambda row: row.record_id, reverse=True
            ):
                if record.schema_id in required_schemas:
                    continue
                stub = budget_stub(record)
                segments[index] = segments[index].model_copy(
                    update={
                        "structured_records": tuple(
                            stub if row.record_id == record.record_id else row
                            for row in segments[index].structured_records
                        )
                    }
                )
                omitted.add("optional_record")
                if (delivered := fit()) is not None:
                    return delivered
        for index in range(len(segments) - 1, -1, -1):
            segment = segments[index]
            if segment.context_excerpts:
                segments[index] = segment.model_copy(
                    update={
                        "context_excerpts": (),
                        "context_only_unit_ids": (),
                        "citations": tuple(
                            c
                            for c in segment.citations
                            if c.unit_id in segment.matched_unit_ids
                        ),
                        "omissions": (
                            *segment.omissions,
                            Omission(
                                reason="output_budget_context",
                                unit_ids=segment.context_only_unit_ids,
                                count=len(segment.context_excerpts),
                            ),
                        ),
                    }
                )
                omitted.add("context")
                if (delivered := fit()) is not None:
                    return delivered
        for index in range(len(segments) - 1, -1, -1):
            segment = segments[index]
            if len(segment.excerpts) > 1:
                winner = segment.excerpts[0]
                segments[index] = segment.model_copy(
                    update={
                        "excerpts": (winner,),
                        "text": winner.text,
                        "matched_unit_ids": (winner.unit_id,),
                        "citations": tuple(
                            c for c in segment.citations if c.unit_id == winner.unit_id
                        ),
                        "structured_records": tuple(
                            r
                            for r in segment.structured_records
                            if r.unit_id == winner.unit_id
                        ),
                        "complete": False,
                        "truncated": True,
                        "omissions": (
                            *segment.omissions,
                            Omission(
                                reason="output_budget_match",
                                unit_ids=tuple(
                                    item.unit_id for item in segment.excerpts[1:]
                                ),
                                count=len(segment.excerpts) - 1,
                            ),
                        ),
                    }
                )
                omitted.add("additional_match")
                if (delivered := fit()) is not None:
                    return delivered
        for index in range(len(visuals) - 1, -1, -1):
            visual = visuals[index]
            if visual.proxies:
                visuals[index] = visual.model_copy(update={"proxies": ()})
                omitted.add("visual_proxy")
                if (delivered := fit()) is not None:
                    return delivered
        for index in range(len(visuals) - 1, -1, -1):
            visual = visuals[index]
            if not any(
                reason.startswith("reserved_") for reason in visual.selection_reasons
            ):
                if len(visuals) == 1 and not segments:
                    continue
                visuals.pop(index)
                omitted.add("visual")
                if (delivered := fit()) is not None:
                    return delivered
        reserved = set(selection.reservation_priority)
        for is_reserved in (False, True):
            for index in range(len(segments) - 1, -1, -1):
                if len(segments) <= 1:
                    break
                if (segments[index].segment_id in reserved) != is_reserved:
                    continue
                segments.pop(index)
                omitted.add("owner")
                if (delivered := fit()) is not None:
                    return delivered
        for index in range(len(visuals) - 1, -1, -1):
            if len(visuals) + len(segments) <= 1:
                break
            visuals.pop(index)
            omitted.add("visual")
            if (delivered := fit()) is not None:
                return delivered
        raise TychoraError(
            "BUDGET_TOO_SMALL",
            "Mandatory response core and last evidence owner exceed the output budget.",
        )


def _delivered_coverage(
    coverage: Coverage, gaps: list[CoverageGap], checks_failed: bool
) -> Coverage:
    """Coverage after delivery: an output-budget omission alone is a truncation.

    The ``delivery`` gap is kept and ``delivery`` joins the truncated channels;
    an unmet requirement or failed checks still make a complete result partial.
    """
    truncated = coverage.truncated_channels
    if any(gap.channel == "delivery" for gap in gaps):
        truncated = tuple(dict.fromkeys((*truncated, "delivery")))
    unmet = any(gap.channel != "delivery" for gap in gaps)
    status = coverage.status
    if (unmet or checks_failed) and status == "complete":
        status = "partial"
    return coverage.model_copy(
        update={"status": status, "truncated_channels": truncated}
    )


def _decision_policy(
    registry: Registry, predicates: tuple[str, ...]
) -> dict[str, tuple[bool, str]]:
    """Each predicate's configured decision requirement and diagnostic category."""
    policy = {}
    for predicate in predicates:
        definition = registry.relations.get(predicate)
        if not isinstance(definition, FrozenJsonMap):
            raise TychoraError(
                "INVALID_REQUEST", "Inspection predicate is not configured."
            )
        required = definition.get("requires_decision", False)
        category = definition.get("diagnostic_category", "none")
        if type(required) is not bool or not isinstance(category, str):
            raise TychoraError(
                "INTEGRITY_ERROR", "Relation decision policy is invalid."
            )
        policy[predicate] = (required, category)
    return policy


def inspect_relations(
    graph: GraphStore,
    view: GraphScope | None,
    selected_unit_ids: tuple[str, ...],
    predicates: tuple[str, ...],
    ctx: ExecutionContext,
    *,
    expansion_inspected_rows: int,
    registry: Registry,
    max_rows: int = 2000,
    max_assertions: int = 100,
    enabled: bool = True,
) -> RelationChecks:
    """Inspect selected owners within the same request's consumed graph work."""
    policy = _decision_policy(registry, predicates)
    if not enabled or view is None:
        return RelationChecks(status="not_evaluated", predicates=predicates)
    if (
        not 0 <= expansion_inspected_rows <= max_rows <= 2000
        or not 0 <= max_assertions <= 100
    ):
        raise TychoraError("INVALID_REQUEST", "Graph inspection budget is invalid.")
    if not selected_unit_ids:
        return RelationChecks(status="complete", predicates=predicates)
    if expansion_inspected_rows == max_rows or max_assertions == 0:
        return RelationChecks(status="partial", predicates=predicates, truncated=True)
    budget = GraphBudget(
        max_rows=max_rows,
        inspected_rows=expansion_inspected_rows,
        max_inspection_edges=max_assertions,
    )
    query = AssertionQuery(selected_unit_ids=selected_unit_ids, predicates=predicates)
    page = graph.get_assertions(view, query, budget, ctx)
    if len(page.assertions) > max_assertions:
        raise TychoraError(
            "INTEGRITY_ERROR", "Graph inspection exceeded assertion cap."
        )
    eligible = view.graph_eligible_unit_ids
    paths: list[RelationPath] = []
    decisions: list[DecisionRequirement] = []
    incomplete = not page.exhausted or page.truncation is not None
    for path in page.assertions:
        if (
            path.source_unit_id not in eligible
            or path.target_unit_id not in eligible
            or not path.required_support_ids
            or not path.required_support_ids <= eligible
            or set(path.assertion_ids) & view.blocked_assertion_ids
            or path.predicate not in predicates
        ):
            incomplete = True
            continue
        endpoints = (path.source_unit_id, path.target_unit_id)
        paths.append(
            RelationPath(
                unit_ids=endpoints,
                assertion_ids=path.assertion_ids,
                predicates=(path.predicate,),
                support_unit_ids=path.required_support_ids,
                provenance=FrozenJsonMap(
                    {
                        "status": path.status,
                        "confidence": path.confidence,
                        "diagnostic_category": policy[path.predicate][1],
                    }
                ),
            )
        )
        if policy[path.predicate][0] and path.status in {"recorded", "confirmed"}:
            decisions.append(
                DecisionRequirement(
                    requirement_id=digest(
                        "conflict-decision-v1", list(path.assertion_ids)
                    ),
                    reason="Recorded conflict requires a human decision."
                    if policy[path.predicate][1] == "conflict"
                    else "Recorded relation requires a human decision.",
                    unit_ids=endpoints,
                    assertion_ids=path.assertion_ids,
                )
            )
    return RelationChecks(
        status="partial" if incomplete else "complete",
        predicates=predicates,
        assertion_ids=tuple(
            dict.fromkeys(aid for path in paths for aid in path.assertion_ids)
        ),
        assertions=tuple(paths),
        decisions=tuple(decisions),
        truncated=incomplete,
    )


def _record_dependencies(value: FrozenJsonMap, owner_id: str) -> frozenset[str]:
    """Collect explicit unit references carried by retained structured values."""
    dependencies = {owner_id}

    def visit(item: object) -> None:
        if isinstance(item, FrozenJsonMap):
            for key, child in item.items():
                if key in {
                    "foundation_ids",
                    "target_ids",
                    "support_ids",
                    "required_unit_ids",
                } or key.endswith("_unit_ids"):
                    if not isinstance(child, tuple) or any(
                        not isinstance(uid, str) for uid in child
                    ):
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Record unit dependencies are malformed."
                        )
                    dependencies.update(cast(tuple[str, ...], child))
                elif key.endswith("_unit_id"):
                    if child is not None:
                        if not isinstance(child, str):
                            raise TychoraError(
                                "INTEGRITY_ERROR",
                                "Record unit dependency is malformed.",
                            )
                        dependencies.add(child)
                visit(child)
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return frozenset(dependencies)


def _readable_units(
    tx: ReadTransaction, view: EligibilityView, unit_ids: frozenset[str]
) -> bool:
    """Whether every unit has a persisted set the view allows."""
    sets = tx.representations.dependencies(tuple(sorted(unit_ids)))
    return len(sets) == len(unit_ids) and all(
        view.allows(sets[unit_id]) for unit_id in unit_ids
    )


def _record_owner(segment: Segment, ranked: RankedEvidence) -> RerankFacts:
    """The facts whose slice holds every matched unit of the segment: its candidate's."""
    matched = set(segment.matched_unit_ids)
    for item in ranked.items:
        facts = item.facts
        if (
            facts is not None
            and facts.prepared.extraction_id == segment.extraction_id
            and matched <= {rep.unit.unit_id for rep in facts.prepared.units}
            and facts.rows.revision.kind_id == segment.kind
        ):
            return facts
    raise TychoraError(
        "INTEGRITY_ERROR",
        "Selected record owner has no retained facts.",
        reason="canonical.facts_missing",
    )


def prepare_records(
    tx: ReadTransaction,
    selection: Selection,
    ranked: RankedEvidence,
    attempt: EligibilityAttempt,
) -> tuple[StructuredRecord, ...]:
    """Materialize records using the pinned original kind/schema envelopes."""
    view = attempt.view
    results: dict[str, StructuredRecord] = {}
    for segment in selection.segments:
        if segment.revision_id not in view.revisions:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Record owner revision differs from the pinned view.",
                reason="invariant.unexpected",
            )
        owner = _record_owner(segment, ranked)
        selected_rows = tuple(
            row for row in owner.rows.records if row.unit_id in segment.matched_unit_ids
        )
        if not selected_rows:
            continue
        envelope = attempt.historical_kind_envelopes.get(segment.kind)
        if envelope is None:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Original record schema envelope is unavailable.",
            )
        try:
            original = json.loads(canonical_bytes(envelope))
            if original["kind"]["id"] != segment.kind:
                raise ValueError("Kind identity differs")
            schema = original["schema_bindings"].get("record_schema")
            if schema is None:
                if all(row.record_type == "code_symbol" for row in selected_rows):
                    continue
                raise ValueError("Original record binding is absent")
            schema_hash = original["schema_digests"][schema]
            for row in selected_rows:
                code_record_binding = (
                    segment.kind == "code_file@1"
                    and row.record_type == "code_symbol"
                    and schema == "code-symbol@1"
                )
                if (
                    row.record_type != schema
                    and not code_record_binding
                    or row.schema_hash != schema_hash
                ):
                    raise ValueError("Retained record type or hash differs")
                owner.prepared.representation(row.unit_id)
                validate_historical_record(envelope, thaw_json(row.data_json))
                dependencies = _record_dependencies(row.data_json, row.unit_id)
                if not _readable_units(tx, view, dependencies):
                    continue
                record_id = digest(
                    "structured-record-v1",
                    row.unit_id,
                    row.record_type,
                    thaw_json(row.data_json),
                )
                results[record_id] = StructuredRecord(
                    record_id=record_id,
                    unit_id=row.unit_id,
                    record_type=row.record_type,
                    schema_id=schema,
                    value=row.data_json,
                    dependencies=dependencies,
                )
        except (ValueError, TypeError, KeyError, TychoraError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Original retained record validation failed."
            ) from None
    return tuple(results[key] for key in sorted(results))
