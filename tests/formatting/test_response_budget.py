"""Final evidence fitting counts every delivered representation."""

import pytest

from tests.helpers.assets import asset_path
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    Citation,
    Coverage,
    Excerpt,
    RelationChecks,
    RelationPath,
    Segment,
    StructuredRecord,
)
from tychora.contracts.requests import BudgetContract, QueryRequest, RequestScope
from tychora.formatting.render import OutputProfile
from tychora.retrieval.response import ResponseAssembler, ResponseContext
from tychora.retrieval.segments import OutputTokenCounter, Selection

pytestmark = pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")


def counter():
    return OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE"))


def segment(text="quoted source"):
    return Segment(
        segment_id="segment-1",
        source_id="source-1",
        revision_id="revision-1",
        extraction_id="extraction-1",
        kind="feedback@1",
        role="feedback",
        lane="feedback",
        excerpts=(
            Excerpt(unit_id="unit-1", char_start=0, char_end=len(text), text=text),
        ),
        text=text,
        matched_unit_ids=("unit-1",),
        citations=(
            Citation(unit_id="unit-1", source_id="source-1", revision_id="revision-1"),
        ),
    )


def case(
    *,
    tokens=900,
    profiles=None,
    records=(),
    segments=None,
    warnings=(),
    required_lanes=(),
    coverage=None,
    run_id="run-1",
    max_response_bytes=1048576,
    relations=(),
):
    profiles = profiles or (
        OutputProfile(renderer="canonical-json@1"),
        OutputProfile(renderer="agent@1"),
    )
    selected = (segment(),) if segments is None else segments
    selection = Selection(
        segments=selected,
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=tuple(s.lane for s in selected),
        text_budget=tokens,
        visual_reserved_tokens=0,
    )
    request = type.__call__(
        QueryRequest,
        question="What is recorded?",
        budgets=BudgetContract(
            max_tokens=tokens, max_response_bytes=max_response_bytes
        ),
    )
    ctx = ResponseContext(
        run_id=run_id,
        snapshot_id="snapshot-1",
        scope=RequestScope(principal_id="reader"),
        counter=counter(),
        profiles=profiles,
        eligible_unit_ids=frozenset({"unit-1"}),
        relations=relations,
        coverage=coverage or Coverage(status="complete"),
        warnings=warnings,
        required_lanes=required_lanes,
    )
    return ResponseAssembler().assemble(
        selection, records, RelationChecks(status="not_evaluated"), request, ctx
    )


def test_mcp_canonical_and_envelope_are_counted_together():
    delivered = case(tokens=900)
    assert sum(counter().count(b.decode()) for b in delivered.payloads) <= 900
    assert sum(len(b) for b in delivered.payloads) <= 1048576
    assert delivered.result.budget_contract.max_tokens == 900
    assert delivered.token_count == sum(
        counter().count(b.decode()) for b in delivered.payloads
    )
    assert len(delivered.payloads) == 2


def test_graph_paths_count_across_all_rendered_profiles():
    profiles = tuple(
        OutputProfile(renderer=name)
        for name in (
            "canonical-json@1",
            "hierarchical-evidence@1",
            "markdown@1",
            "agent@1",
        )
    )
    path = RelationPath(
        unit_ids=("unit-1",),
        assertion_ids=("assertion-marker",),
        predicates=("supports@1",),
        support_unit_ids=frozenset({"unit-1"}),
        provenance=FrozenJsonMap({"status": "recorded"}),
    )
    baseline = case(tokens=3000, profiles=profiles)
    delivered = case(tokens=3000, profiles=profiles, relations=(path,))
    assert delivered.byte_count > baseline.byte_count
    assert all(b"assertion-marker" in payload for payload in delivered.payloads)
    assert delivered.token_count == sum(
        counter().count(payload.decode()) for payload in delivered.payloads
    )
    cap = baseline.token_count + (delivered.token_count - baseline.token_count) // 2
    with pytest.raises(TychoraError) as error:
        case(tokens=cap, profiles=profiles, relations=(path,))
    assert error.value.code == "BUDGET_TOO_SMALL"


def test_long_optional_record_becomes_stub_without_changing_source_quote():
    record = StructuredRecord(
        record_id="record-1",
        unit_id="unit-1",
        record_type="feedback",
        schema_id="feedback@1",
        value=FrozenJsonMap({"literal_quote": "é</agent>" * 1200}),
        dependencies=frozenset({"unit-1"}),
    )
    delivered = case(tokens=1200, records=(record,))
    kept = delivered.result.segments[0].structured_records[0]
    assert kept.value == FrozenJsonMap({"omitted_budget": True})
    assert delivered.result.segments[0].text == "quoted source"
    assert delivered.result.segments[0].citations


def test_required_record_stays_with_owner_and_tiny_core_fails():
    record = StructuredRecord(
        record_id="required-1",
        unit_id="unit-1",
        record_type="feedback",
        schema_id="feedback@1",
        value=FrozenJsonMap({"quote": "x" * 2000}),
        dependencies=frozenset({"unit-1"}),
    )
    with pytest.raises(TychoraError) as error:
        case(tokens=400, records=(record,), required_lanes=("feedback",))
    assert error.value.code == "BUDGET_TOO_SMALL"


def test_mandatory_warning_and_last_match_cannot_be_dropped():
    with pytest.raises(TychoraError) as error:
        case(tokens=30, warnings=("required warning " * 50,))
    assert error.value.code == "BUDGET_TOO_SMALL"


def test_new_run_id_is_measured_before_delivery():
    first = case(
        tokens=900,
        profiles=(OutputProfile(renderer="canonical-json@1"),),
        run_id="short",
    )
    second = case(
        tokens=900,
        profiles=(OutputProfile(renderer="canonical-json@1"),),
        run_id="new-run-id-with-longer-length",
    )
    assert second.byte_count > first.byte_count
    assert b"new-run-id-with-longer-length" in second.payloads[0]


def test_record_with_ineligible_dependency_is_rejected():
    record = StructuredRecord(
        record_id="record-2",
        unit_id="unit-1",
        record_type="feedback",
        schema_id="feedback@1",
        value=FrozenJsonMap({"quote": "verbatim"}),
        dependencies=frozenset({"unit-1", "hidden-unit"}),
    )
    with pytest.raises(TychoraError) as error:
        case(tokens=900, records=(record,))
    assert error.value.code == "INTEGRITY_ERROR"


def test_required_lane_gap_is_recomputed_after_owner_omission():
    primary = segment("first")
    secondary = segment("second " * 200).model_copy(
        update={
            "segment_id": "segment-2",
            "lane": "standard",
            "kind": "standard@1",
            "matched_unit_ids": ("unit-1",),
        }
    )
    delivered = case(
        tokens=800,
        segments=(primary, secondary),
        required_lanes=("standard",),
        profiles=(OutputProfile(renderer="canonical-json@1"),),
    )
    assert delivered.result.segments == (primary,)
    assert delivered.result.coverage.status == "partial"
    assert delivered.result.coverage_gaps[0].message == "output_budget_omission"


def test_duplicate_canonical_echo_is_counted_as_a_second_target():
    profile = OutputProfile(renderer="canonical-json@1")
    delivered = case(tokens=1500, profiles=(profile, profile))
    assert delivered.payloads[0] == delivered.payloads[1]
    assert delivered.token_count == 2 * counter().count(delivered.payloads[0].decode())


def test_reserved_visual_proxy_can_be_omitted_while_asset_remains():
    from tychora.contracts.evidence import ImageLocator, LabelledProxy, VisualEvidence

    visual = VisualEvidence(
        asset_id="asset-1",
        source_id="source-1",
        revision_id="revision-1",
        asset_ref="artifact-1",
        locator=ImageLocator(artifact_ref="artifact-1", native_ref="img-1"),
        proxies=(
            LabelledProxy(
                proxy_id="proxy-1",
                label="OCR",
                text="derived " * 1500,
                origin="derived",
                dependencies=frozenset({"unit-1", "asset-1"}),
                review_state="accepted",
            ),
        ),
        selection_reasons=("reserved_companion",),
    )
    selected = (segment(),)
    selection = Selection(
        segments=selected,
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=("feedback",),
        text_budget=900,
        visual_reserved_tokens=0,
    )
    request = type.__call__(
        QueryRequest,
        question="What is recorded?",
        budgets=BudgetContract(max_tokens=900),
    )
    ctx = ResponseContext(
        run_id="run-1",
        snapshot_id="snapshot-1",
        scope=RequestScope(principal_id="reader"),
        counter=counter(),
        profiles=(OutputProfile(renderer="canonical-json@1"),),
        eligible_unit_ids=frozenset({"unit-1"}),
        eligible_asset_ids=frozenset({"asset-1"}),
        visuals=(visual,),
    )
    delivered = ResponseAssembler().assemble(
        selection, (), RelationChecks(status="not_evaluated"), request, ctx
    )
    assert delivered.result.visuals[0].asset_id == "asset-1"
    assert delivered.result.visuals[0].proxies == ()


def test_conditional_requirement_uses_explicit_context_and_not_query_text():
    from tychora.retrieval.response import ResponseRequirement

    requirement = ResponseRequirement(
        requirement_id="need-standard",
        target="lane",
        value="standard",
        when=FrozenJsonMap({"eq": ["project", "alpha"]}),
    )
    selection = Selection(
        segments=(segment(),),
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=("feedback",),
        text_budget=900,
        visual_reserved_tokens=0,
    )
    request = type.__call__(
        QueryRequest, question="project alpha", budgets=BudgetContract(max_tokens=900)
    )
    explicit = type.__call__(
        QueryRequest,
        question="project alpha",
        context=FrozenJsonMap({"project": "alpha"}),
        budgets=BudgetContract(max_tokens=900),
    )
    base = ResponseContext(
        run_id="run-1",
        snapshot_id="snapshot-1",
        scope=RequestScope(principal_id="reader"),
        counter=counter(),
        profiles=(OutputProfile(renderer="canonical-json@1"),),
        eligible_unit_ids=frozenset({"unit-1"}),
        requirements=(requirement,),
    )
    absent = ResponseAssembler().assemble(
        selection,
        (),
        RelationChecks(status="not_evaluated"),
        request,
        base,
    )
    present = ResponseAssembler().assemble(
        selection,
        (),
        RelationChecks(status="not_evaluated"),
        explicit,
        base,
    )
    assert absent.result.coverage_gaps == ()
    assert present.result.coverage_gaps[0].code == "need-standard"
    assert present.result.coverage.status == "partial"


def test_zero_hit_mandatory_core_still_obeys_byte_cap():
    with pytest.raises(TychoraError) as error:
        case(
            tokens=6000,
            segments=(),
            profiles=(OutputProfile(renderer="canonical-json@1"),),
            warnings=("required warning",),
            max_response_bytes=64,
        )
    assert error.value.code == "BUDGET_TOO_SMALL"


def test_optional_owner_omission_marks_delivery_partial_without_erasing_completed_channel():
    primary = segment("first")
    secondary = segment("second " * 200).model_copy(update={"segment_id": "segment-2"})
    delivered = case(
        tokens=800,
        segments=(primary, secondary),
        profiles=(OutputProfile(renderer="canonical-json@1"),),
        coverage=Coverage(status="complete", completed_channels=("lexical",)),
    )
    assert delivered.result.segments == (primary,)
    assert delivered.result.coverage.status == "partial"
    assert delivered.result.coverage.completed_channels == ("lexical",)
    assert any(
        gap.code == "output_budget_omission" for gap in delivered.result.coverage_gaps
    )
