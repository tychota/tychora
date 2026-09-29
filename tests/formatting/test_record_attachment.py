"""Retained record rows are revalidated against the pinned eligibility view."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from tests.formatting.test_response_budget import segment
from tests.retrieval.test_segments import case, config
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import StructuredRecordRow
from tychora.retrieval.eligibility import EligibilityAttempt
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
    attempt = EligibilityAttempt(
        view=view,
        compiler=None,
        historical_kind_envelopes=MappingProxyType(
            {"feedback@1": registry.historical_definitions()["feedback@1"]}
        ),
    )
    prepared = prepare_records(selection, ranked, attempt)
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
        prepare_records(
            selection,
            RankedEvidence((hidden_item,)),
            attempt,
        )
        == ()
    )


def test_changed_original_schema_envelope_controls_retained_record(tmp_path):
    import copy

    from tychora.contracts.base import thaw_json
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import digest

    bundle, _, ranked, view = case("Historical feedback quote.", (), kind="feedback@1")
    uid = bundle.units[0].unit_id
    registry = Registry.from_config(config(tmp_path))
    original = thaw_json(registry.historical_definitions()["feedback@1"])
    schema = original["schema_bindings"]["record_schema"]
    value = FrozenJsonMap(
        {
            "comment": "Historical feedback quote.",
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
        schema_hash=original["schema_digests"][schema],
        data_json=value,
    )
    item = ranked.items[0]
    retained = replace(
        item,
        facts=replace(
            item.facts, rows=bundle.model_copy(update={"records": (record,)})
        ),
    )
    selection = Selection(
        segments=(
            segment("Historical feedback quote.").model_copy(
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

    changed = copy.deepcopy(original)
    changed["schemas"][schema]["properties"]["new_required"] = {"type": "string"}
    changed["schemas"][schema]["required"].append("new_required")
    changed["schema_digests"][schema] = digest("schema-v1", changed["schemas"][schema])
    changed["envelope_digest"] = digest(
        "historical-kind-v1",
        {key: val for key, val in changed.items() if key != "envelope_digest"},
    )

    assert (
        prepare_records(
            selection,
            RankedEvidence((retained,)),
            EligibilityAttempt(
                view=view,
                compiler=None,
                historical_kind_envelopes=MappingProxyType(
                    {"feedback@1": FrozenJsonMap(original)}
                ),
            ),
        )[0].value
        == value
    )
    with pytest.raises(TychoraError):
        prepare_records(
            selection,
            RankedEvidence((retained,)),
            EligibilityAttempt(
                view=view,
                compiler=None,
                historical_kind_envelopes=MappingProxyType(
                    {"feedback@1": FrozenJsonMap(changed)}
                ),
            ),
        )
