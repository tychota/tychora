import json
from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from tests.typed_json import json_array, json_object
from tychora.contracts.requests import SourceInput


def source_input_via_keywords(**fields: object) -> SourceInput:
    """Exercise RootModel's public keyword constructor despite its root-only typing."""
    return type.__call__(SourceInput, **fields)


def test_nested_json_is_detached_and_immutable():
    from tychora.contracts.base import FrozenJsonMap

    nested = {"tags": ["one", {"number": 2}]}
    original = {"nested": nested}
    source = source_input_via_keywords(
        type="text",
        source_key="note",
        collection_id=UUID(int=1),
        kind="note",
        text="hi",
        metadata=original,
    )
    nested["tags"].append("two")
    tags = json_array(json_object(source.root.metadata, "nested")["tags"])
    assert tags == (
        "one",
        FrozenJsonMap({"number": 2}),
    )
    with pytest.raises(TypeError):
        from operator import setitem

        # A write to immutable contract JSON must fail at runtime.
        setitem(json_object(source.root.metadata, "nested"), "bad", True)  # ty: ignore[no-matching-overload]
    with pytest.raises((AttributeError, TypeError)):
        tags.append("bad")  # ty: ignore[unresolved-attribute]
    assert json.loads(source.model_dump_json())["metadata"] == {
        "nested": {"tags": ["one", {"number": 2}]}
    }


def test_scope_only_narrows_and_rejects_principal_change():
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.requests import RequestScope

    granted = RequestScope(
        principal_id="alice",
        collection_ids=frozenset({"a", "b"}),
        access_ceiling=2,
        uses=frozenset({"search"}),
        capabilities=frozenset({"read"}),
        upload_providers=frozenset({"local"}),
    )
    requested = RequestScope(
        principal_id="alice",
        collection_ids=frozenset({"b", "c"}),
        access_ceiling=9,
        uses=frozenset({"search", "train"}),
        capabilities=frozenset({"read", "write"}),
        upload_providers=frozenset({"remote"}),
    )
    narrow = granted.narrow(requested)
    assert narrow.collection_ids == frozenset({"b"})
    assert narrow.access_ceiling == 2
    assert narrow.uses == frozenset({"search"})
    assert narrow.capabilities == frozenset({"read"})
    assert narrow.upload_providers == frozenset()
    with pytest.raises(TychoraError, match="principal"):
        granted.narrow(RequestScope(principal_id="bob"))
    assert (
        granted.narrow(RequestScope(principal_id="alice")).collection_ids == frozenset()
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"as_of": "2025-02-30"},
        {"limit": "8"},
        {"limit": 0},
        {"limit": True},
        {"unexpected": 1},
        {"input": {"type": "question", "text": "a"}, "question": "b"},
    ],
)
def test_query_mapping_rejects_invalid_values(patch):
    from tychora.contracts.requests import QueryRequest

    with pytest.raises((ValidationError, ValueError)):
        QueryRequest.from_mapping({"question": "find", **patch})


def test_primitive_query_mapping_roundtrips_losslessly():
    from tychora.contracts.requests import QueryRequest

    request = QueryRequest.from_mapping(
        {
            "input": {"type": "question", "text": "é"},
            "as_of": "2025-02-28",
            "revision_ids": ["r1"],
            "modalities": ["text"],
            "filters": {
                "op": "and",
                "children": [{"op": "eq", "field": "language", "value": "python"}],
            },
        }
    )
    assert request.as_of == date(2025, 2, 28)
    assert request.revision_ids == ("r1",)
    assert QueryRequest.from_mapping(request.model_dump(mode="json")) == request
    from tychora.contracts.requests import QuestionInput

    direct = type.__call__(QueryRequest, question="hello")
    assert isinstance(direct.input, QuestionInput)
    assert direct.input.text == "hello"
    with pytest.raises(ValidationError):
        type.__call__(
            QueryRequest, input={"type": "question", "text": "x"}, revision_ids=["r"]
        )


@pytest.mark.parametrize(
    "end,valid",
    [(None, True), ("2025-03-01", True), ("2025-01-01", False), ("2025-02-30", False)],
)
def test_effective_interval_validation(end, valid):
    from tychora.contracts.base import DateInterval

    data = {"start": "2025-02-01", "end": end}
    if valid:
        assert DateInterval.from_mapping(data).model_dump(mode="json") == data
    else:
        with pytest.raises(ValidationError):
            DateInterval.from_mapping(data)


@pytest.mark.parametrize(
    "extra", [{"path": "file.md"}, {"bytes": "abc"}, {"unexpected": "x"}]
)
def test_source_payload_exclusivity_and_unknown_fields(extra):

    with pytest.raises(ValidationError):
        source_input_via_keywords(
            type="text",
            source_key="note",
            collection_id=UUID(int=1),
            kind="note",
            text="hi",
            **extra,
        )


@pytest.mark.parametrize(
    "patch", [{"max_tokens": 0}, {"max_tokens": True}, {"max_response_bytes": 0}]
)
def test_budget_bounds(patch):
    from tychora.contracts.requests import BudgetContract

    with pytest.raises(ValidationError):
        BudgetContract(**patch)


@pytest.mark.parametrize(
    "variant,payload",
    [
        ("file", {"path": "./a"}),
        ("folder", {"path": "./folder"}),
        ("repository", {"path": "./repo"}),
        ("text", {"text": "é"}),
        ("bytes", {"data": "AP8="}),
        ("url", {"url": "https://example.org/doc"}),
    ],
)
def test_source_wire_variants_roundtrip(variant, payload):
    from tychora.contracts.requests import SourceInput

    data = {
        "type": variant,
        "source_key": "s",
        "collection_id": str(UUID(int=1)),
        "kind": "manual",
        **payload,
    }
    source = SourceInput.from_mapping(data)
    assert "root" not in source.model_dump(mode="json")
    assert SourceInput.from_mapping(source.model_dump(mode="json")) == source
    if variant == "bytes":
        from tychora.contracts.requests import BytesSource

        assert isinstance(source.root, BytesSource)
        assert source.root.data == b"\x00\xff"


def test_evidence_ranges_geometry_and_unknown_page():
    from tychora.contracts.evidence import AnchorRange, DocumentLocator, Geometry

    locator = DocumentLocator(
        artifact_ref="artifact:a", native_ref="section:1", char_start=0, char_end=4
    )
    assert locator.physical_page is None and locator.printed_page is None
    assert DocumentLocator.from_mapping(locator.model_dump(mode="json")) == locator
    with pytest.raises(ValidationError):
        AnchorRange(unit_id="u", char_start=8, char_end=2, origin="source")
    with pytest.raises(ValidationError):
        Geometry(domain="normalized", x0=0.0, y0=0.0, x1=2.0, y1=1.0)
    with pytest.raises(ValidationError):
        Geometry(domain="pixels", x0=5.0, y0=0.0, x1=2.0, y1=1.0)


def test_public_schema_exports_are_stable_and_valid():
    from jsonschema import Draft202012Validator

    from tychora.contracts import public_schemas

    schemas = public_schemas()
    for name in (
        "SourceInput",
        "QueryRequest",
        "QueryResult",
        "DiscoverRequest",
        "DiscoveryResult",
        "InspectRequest",
        "InspectionResult",
        "RelationRequest",
        "RelationResult",
        "JobHandle",
        "JobEvent",
        "MaintenanceRequest",
        "MaintenanceResult",
        "EvaluationRequest",
        "EvaluationReport",
    ):
        schema = schemas[name]
        assert schema["$id"] == f"urn:tychora:contracts:{name}:1"
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "code,http,cli",
    [
        ("INVALID_REQUEST", 400, 2),
        ("FORBIDDEN_SCOPE", 403, 1),
        ("NOT_FOUND", 404, 1),
        ("WORKSPACE_BUSY", 409, 1),
        ("CAPABILITY_MISSING", 503, 1),
        ("INDEX_INCOMPLETE", 503, 1),
        ("PROVIDER_FAILURE", 503, 1),
        ("INTEGRITY_ERROR", 409, 1),
        ("CANCELLED", 409, 130),
        ("DEADLINE_EXCEEDED", 504, 1),
        ("SCOPE_CHANGED", 409, 1),
        ("PROFILE_INCOMPATIBLE", 409, 1),
        ("BUDGET_TOO_SMALL", 400, 2),
        ("OUTPUT_BUDGET_EXCEEDED", 400, 2),
        ("IDEMPOTENCY_CONFLICT", 409, 1),
        ("ORIGINAL_REMOVED", 410, 1),
    ],
)
def test_error_mapping_and_bounded_safe_envelope(code, http, cli):
    from tychora.contracts.errors import TychoraError

    error = TychoraError(code, "é\n" * 10000, retryable=True)
    # Attach an undeclared secret canary to prove error serialization excludes it.
    error.provider_body = {"secret": "never serialize this"}  # ty: ignore[unresolved-attribute]
    assert error.http_status == http
    assert error.cli_exit_code == cli
    wire = error.to_json()
    assert len(wire.encode()) < 8192
    assert json.loads(wire)["code"] == code
    assert "secret" not in wire and "provider_body" not in wire


def test_operations_are_typed_bounded_and_registered_id_only():
    from tychora.contracts.operations import (
        DiscoverRequest,
        InspectRequest,
        MaintenanceRequest,
        RelationRequest,
    )

    assert DiscoverRequest(subject="sources").limit > 0
    for data in (
        {"subject": "sources", "limit": 0},
        {"subject": "anything"},
        {"subject": "sources", "limit": 10001},
    ):
        with pytest.raises(ValidationError):
            DiscoverRequest.from_mapping(data)
    with pytest.raises(ValidationError):
        type.__call__(InspectRequest, subject="evidence", id="u", path="/etc/passwd")
    with pytest.raises(ValidationError):
        RelationRequest(unit_ids=("u",), max_hops=-1)
    valid = MaintenanceRequest.from_mapping(
        {"operation": "remove", "source_ids": ["s"]}
    )
    from tychora.contracts.operations import RemoveMaintenance

    assert isinstance(valid.root, RemoveMaintenance)
    assert valid.root.source_ids == ("s",)
    with pytest.raises(ValidationError):
        MaintenanceRequest.from_mapping(
            {"operation": "remove", "path": "/tmp/arbitrary"}
        )


def test_source_json_constructor_preserves_uuid_and_bytes():
    from tychora.contracts.requests import SourceInput

    source = SourceInput.from_mapping(
        {
            "type": "bytes",
            "source_key": "s",
            "collection_id": str(UUID(int=1)),
            "kind": "file",
            "data": "AP8=",
        }
    )
    assert SourceInput.model_validate_json(source.model_dump_json()) == source


@pytest.mark.parametrize(
    "metric",
    [
        {"name": "recall", "value": None, "sample_count": 0},
        {"name": "recall", "value": 0.5, "sample_count": 1, "denominator": 0},
        {
            "name": "recall",
            "value": None,
            "sample_count": 0,
            "not_applicable_reason": "",
        },
    ],
)
def test_metric_na_is_explicit_and_zero_denominator_never_scores(metric):
    from tychora.contracts.operations import MetricValue

    with pytest.raises(ValidationError):
        MetricValue.from_mapping(metric)


@pytest.mark.parametrize(
    "expr",
    [
        {"op": "sql", "field": "x", "value": "x"},
        {"op": "range", "field": "x", "lower": 4, "upper": 2},
        {"op": "range", "field": "x", "lower": "a", "upper": 2},
        {"op": "range", "field": "x"},
        {"op": "in", "field": "x", "values": []},
        {"op": "and", "children": []},
        {"op": "eq", "field": "x", "value": {"sql": "raw"}},
    ],
)
def test_filter_ast_rejects_invalid_shape_and_order(expr):
    from tychora.contracts.requests import QueryRequest

    with pytest.raises((ValidationError, ValueError)):
        QueryRequest.from_mapping({"question": "find", "filters": expr})


def test_query_result_roundtrip_preserves_exact_excerpts_and_nested_records():
    from tychora.contracts.evidence import QueryResult

    wire = {
        "run_id": "run1",
        "snapshot_id": "snap1",
        "applied_scope": {"principal_id": "reader", "collection_ids": ["c"]},
        "coverage": {"status": "complete", "completed_channels": ["lexical"]},
        "segments": [
            {
                "segment_id": "s1",
                "source_id": "src1",
                "revision_id": "r1",
                "extraction_id": "e1",
                "kind": "manual",
                "role": "reference",
                "lane": "source",
                "excerpts": [
                    {"unit_id": "u1", "char_start": 0, "char_end": 2, "text": "é🙂"}
                ],
                "text": "é🙂",
                "matched_unit_ids": ["u1"],
                "structured_records": [
                    {
                        "record_id": "rec1",
                        "unit_id": "u1",
                        "record_type": "rule",
                        "schema_id": "rule@1",
                        "value": {"nested": [{"requirement": True}]},
                        "dependencies": ["u1"],
                    }
                ],
            }
        ],
    }
    result = QueryResult.from_mapping(wire)
    assert result.segments[0].excerpts[0].text == "é🙂"
    assert QueryResult.from_mapping(result.model_dump(mode="json")) == result
    with pytest.raises(TypeError):
        from operator import setitem

        nested = json_array(
            json_object(result.segments[0].structured_records[0].value)["nested"]
        )
        # Deliberately attempt a write through the frozen nested JSON mapping.
        setitem(json_object(nested[0]), "requirement", False)  # ty: ignore[no-matching-overload]
    with pytest.raises(ValidationError):
        result.segments[0].text = "changed"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "variant",
    [
        {"operation": "setup"},
        {"operation": "sync", "source_ids": ["s"]},
        {"operation": "remove", "source_ids": ["s"]},
        {"operation": "rebuild", "channels": ["lexical"]},
        {"operation": "export", "destination": "backup.tar"},
        {"operation": "import", "archive_path": "backup.tar"},
        {"operation": "health"},
        {"operation": "evaluate", "dataset_id": "d", "split": "test"},
        {"operation": "history-purge", "before": "2025-02-01T12:00:00Z"},
        {"operation": "cache-purge", "namespaces": ["results"]},
        {"operation": "purge-originals", "source_id": "s", "revision_ids": ["r"]},
        {
            "operation": "provider-upload-consent",
            "collection_id": "c",
            "provider": "voyage",
            "action": "grant",
        },
        {"operation": "migrate", "target_version": "3"},
    ],
)
def test_maintenance_variant_roundtrips(variant):
    from tychora.contracts.operations import MaintenanceRequest

    request = MaintenanceRequest.from_mapping(variant)
    assert MaintenanceRequest.from_mapping(request.model_dump(mode="json")) == request


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1: "bad"}])
def test_metadata_rejects_non_json_or_nonfinite_payloads(value):

    with pytest.raises((ValidationError, ValueError)):
        source_input_via_keywords(
            type="text",
            source_key="s",
            collection_id=UUID(int=1),
            kind="note",
            text="x",
            metadata={"bad": value},
        )


def test_public_evidence_and_error_types_are_exported():
    from tychora import contracts
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.evidence import (
        AnchorRange,
        EvidenceUnit,
        Locator,
        Segment,
        VisualEvidence,
    )
    from tychora.contracts.requests import FilterExpr

    for name, expected in {
        "AnchorRange": AnchorRange,
        "EvidenceUnit": EvidenceUnit,
        "Locator": Locator,
        "Segment": Segment,
        "VisualEvidence": VisualEvidence,
        "TychoraError": TychoraError,
        "FilterExpr": FilterExpr,
    }.items():
        assert getattr(contracts, name) is expected


@pytest.mark.parametrize("limits", [{"max_hops": 3}, {"max_edges": 101}])
def test_relation_inspection_cannot_raise_spec_ceiling(limits):
    from tychora.contracts.operations import RelationRequest

    with pytest.raises(ValidationError):
        RelationRequest(unit_ids=("u",), **limits)


@pytest.mark.parametrize("model_name", ["QueryRequest", "QueryResult"])
def test_exported_schema_validates_populated_nested_instances(model_name):
    from jsonschema import Draft202012Validator

    from tychora.contracts import QueryRequest, QueryResult, public_schemas

    target = {
        "renderer": "canonical-json@1",
        "profile_digest": "profile:1",
        "schema_digest": "schema:1",
        "serialization": "canonical-json-v1",
    }
    if model_name == "QueryRequest":
        instance = QueryRequest.from_mapping(
            {
                "question": "find evidence",
                "budgets": {"render_targets": [target]},
                "filters": {
                    "op": "and",
                    "children": [
                        {"op": "eq", "field": "kind", "value": "manual"},
                        {
                            "op": "or",
                            "children": [
                                {"op": "in", "field": "language", "values": ["en"]}
                            ],
                        },
                    ],
                },
            }
        )
    else:
        instance = QueryResult.from_mapping(
            {
                "run_id": "run:1",
                "snapshot_id": "snapshot:1",
                "applied_scope": {"principal_id": "reader"},
                "coverage": {"status": "complete"},
                "budget_contract": {"render_targets": [target]},
                "trace": {"stages": [{"name": "lexical", "counts": [1, 2]}]},
            }
        )
    schema = public_schemas()[model_name]
    Draft202012Validator(schema).validate(instance.model_dump(mode="json"))


def test_thawing_a_frozen_map_reads_entries_without_key_lookups(monkeypatch):
    from tychora.contracts.base import FrozenJsonMap, thaw_json

    frozen = FrozenJsonMap(
        {
            f"key-{index:04}": {"nested": [index, {"leaf": index}]}
            for index in range(500)
        }
    )
    lookups = []
    lookup = FrozenJsonMap.__getitem__

    def counting(self, key):
        lookups.append(key)
        return lookup(self, key)

    monkeypatch.setattr(FrozenJsonMap, "__getitem__", counting)
    thawed = thaw_json(frozen)
    # A key lookup scans the entries, so thawing by lookup is quadratic in map size.
    assert lookups == []
    assert isinstance(thawed, dict)
    assert list(thawed) == sorted(thawed)
    assert thawed["key-0042"] == {"nested": [42, {"leaf": 42}]}
