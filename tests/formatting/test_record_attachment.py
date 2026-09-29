"""Retained record rows are revalidated against the pinned eligibility view."""

from dataclasses import replace

from tests.formatting.test_response_budget import segment
from tests.retrieval.test_segments import case, config
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import StructuredRecordRow
from tychora.retrieval.rerank import RankedEvidence
from tychora.retrieval.response import prepare_records
from tychora.retrieval.segments import Selection


def test_retained_feedback_record_has_complete_eligible_dependencies(tmp_path):
    bundle, _, ranked, view = case("Exact review quote.", (), kind="feedback@1")
    uid = bundle.units[0].unit_id
    registry = Registry.from_config(config(tmp_path))
    schema = registry.artifact["definition_schema_bindings"]["kinds"]["feedback@1"][
        "record_schema"
    ]
    value = FrozenJsonMap(
        {
            "comment": "Exact review quote.",
            "decision": "Clarification requested",
            "target": "draft-1",
            "provenance": ["source-3"],
            "status": "recorded",
            "foundation_ids": [uid],
        }
    )
    record = StructuredRecordRow(
        unit_id=uid,
        record_type=schema,
        schema_hash=registry.schema_digests[schema],
        data_json=value,
    )
    item = ranked.items[0]
    with_record = replace(
        item,
        facts=replace(
            item.facts, rows=bundle.model_copy(update={"records": (record,)})
        ),
    )
    ranked = RankedEvidence((with_record,))
    selection = Selection(
        segments=(
            segment("Exact review quote.").model_copy(
                update={
                    "matched_unit_ids": (uid,),
                    "extraction_id": bundle.extraction.extraction_id,
                    "revision_id": bundle.revision.revision_id,
                    "kind": "feedback@1",
                }
            ),
        ),
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=("feedback",),
        text_budget=900,
        visual_reserved_tokens=0,
    )
    prepared = prepare_records(selection, ranked, view, registry)
    assert len(prepared) == 1
    assert prepared[0].value == value
    assert prepared[0].dependencies == frozenset({uid})
    hidden = record.model_copy(
        update={
            "data_json": FrozenJsonMap(
                {**dict(value), "foundation_ids": [uid, "hidden-unit"]}
            )
        }
    )
    hidden_item = replace(
        item,
        facts=replace(
            item.facts, rows=bundle.model_copy(update={"records": (hidden,)})
        ),
    )
    assert (
        prepare_records(selection, RankedEvidence((hidden_item,)), view, registry) == ()
    )
