"""The built-in code record alias stays bound to its immutable original schema."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from tests.code.test_structure import _registration
from tests.formatting.test_response_budget import segment
from tychora.adapters.code.normalize import normalize_code, retain_code_extraction
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap, thaw_json
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.indexing.representations import prepare_extraction
from tychora.retrieval.eligibility import EligibilityAttempt
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import RankedEvidence, RankedEvidenceItem, RerankFacts
from tychora.retrieval.response import prepare_records
from tychora.retrieval.segments import Selection


def test_code_symbol_alias_requires_exact_builtin_kind_schema_and_hash(tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    registry = Registry.from_config(resolve_config(settings, "test", {}))
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry, artifacts)
    bundle = normalize_code(retained, registry)
    prepared = prepare_extraction(bundle)
    record = next(row for row in bundle.records if row.data_json["name"] == "first")
    unit_ids = frozenset(unit.unit_id for unit in bundle.units)
    view = type(
        "View",
        (),
        {
            "revision_ids": frozenset({bundle.revision.revision_id}),
            "context_unit_ids": unit_ids,
            "structural_unit_ids": unit_ids,
        },
    )()
    selected = Selection(
        segments=(
            segment("def first(): return 1").model_copy(
                update={
                    "matched_unit_ids": (record.unit_id,),
                    "extraction_id": bundle.extraction.extraction_id,
                    "revision_id": bundle.revision.revision_id,
                    "kind": "code_file@1",
                }
            ),
        ),
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=("code",),
        text_budget=900,
        visual_reserved_tokens=0,
    )
    item = RankedEvidenceItem(
        FusedCandidate(("unit", record.unit_id), 1, 1.0, {"exact": 1}),
        None,
        RerankFacts(bundle, prepared, ()),
        "disabled",
    )
    envelope = registry.historical_definitions()["code_file@1"]

    def check(row=record, *, kind="code_file@1", historical=envelope):
        rows = bundle.model_copy(update={"records": (row,)})
        ranked = RankedEvidence((replace(item, facts=replace(item.facts, rows=rows)),))
        selection = replace(
            selected,
            segments=(selected.segments[0].model_copy(update={"kind": kind}),),
        )
        attempt = EligibilityAttempt(
            view=view,
            compiler=None,
            historical_kind_envelopes=MappingProxyType({kind: historical}),
        )
        return prepare_records(selection, ranked, attempt)

    (attached,) = check()
    assert attached.record_type == "code_symbol"
    assert attached.schema_id == "code-symbol@1"
    assert attached.value["definition_key"] == record.data_json["definition_key"]
    with pytest.raises(TychoraError) as wrong_kind:
        check(
            kind="feedback@1",
            historical=registry.historical_definitions()["feedback@1"],
        )
    assert wrong_kind.value.code == "INTEGRITY_ERROR"
    with pytest.raises(TychoraError) as wrong_hash:
        check(record.model_copy(update={"schema_hash": "0" * 64}))
    assert wrong_hash.value.code == "INTEGRITY_ERROR"
    changed = thaw_json(envelope)
    changed["schema_bindings"]["record_schema"] = changed["schema_bindings"][
        "metadata_schema"
    ]
    changed["envelope_digest"] = digest(
        "historical-kind-v1",
        {key: value for key, value in changed.items() if key != "envelope_digest"},
    )
    with pytest.raises(TychoraError) as wrong_schema:
        check(historical=FrozenJsonMap(changed))
    assert wrong_schema.value.code == "INTEGRITY_ERROR"
