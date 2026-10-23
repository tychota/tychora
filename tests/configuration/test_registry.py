"""Configuration semantics against real installed and synthetic local packs."""

from collections.abc import Mapping
from typing import Any, cast

import pytest

from tests.typed_json import json_array, json_number, json_object, json_string
from tychora.configuration.schemas import LocalSchemas, import_inclusive_end
from tychora.contracts.errors import TychoraError
from tychora.contracts.sections import Depth


def test_schema_replay_uses_persisted_transitive_targets():
    schemas = {
        "root@1": {"$id": "root@1", "$ref": "renamed.json"},
        "middle@1": {
            "$id": "middle@1",
            "$defs": {"part": {"$ref": "other.json#/$defs/value"}},
        },
        "target@1": {
            "$id": "target@1",
            "$defs": {"value": {"type": "integer", "minimum": 4}},
        },
    }
    refs = {
        "root@1": {"/$ref": {"schema_id": "middle@1", "fragment": "/$defs/part"}},
        "middle@1": {
            "/$defs/part/$ref": {"schema_id": "target@1", "fragment": "/$defs/value"}
        },
        "target@1": {},
    }
    validators = LocalSchemas(schemas, refs)
    assert validators.validate("root@1", 4) == 4
    with pytest.raises(TychoraError, match="schema"):
        validators.validate("root@1", 3)
    assert schemas["root@1"]["$ref"] == "renamed.json"


@pytest.mark.parametrize(
    "schema,refs",
    [
        ({"$id": "bad@1", "pattern": "x"}, {}),
        ({"$id": "bad@1", "$ref": "https://example.invalid/schema"}, {}),
        ({"$id": "bad@1", "$ref": "../escape.json"}, {}),
        ({"$id": "bad@1", "$ref": "local.json"}, {}),
        ({"$id": "bad@1", "type": "array", "items": {"type": "string"}}, {}),
        ({"$id": "bad@1", "format": "uri"}, {}),
        (
            {"$id": "bad@1", "$ref": "#"},
            {"/$ref": {"schema_id": "bad@1", "fragment": ""}},
        ),
        ({"$id": "bad@1", "$defs": {"x": {"$id": "nested@1"}}}, {}),
    ],
)
def test_schema_replay_rejects_unbounded_or_ambient_resolution(schema, refs):
    with pytest.raises(TychoraError) as err:
        LocalSchemas({"bad@1": schema}, {"bad@1": refs})
    assert err.value.code == "INVALID_REQUEST"


def test_schema_enforces_dates_unknown_fields_and_bounds():
    schema = {
        "$id": "dated@1",
        "type": "object",
        "properties": {"date": {"type": "string", "format": "date"}},
        "required": ["date"],
        "additionalProperties": False,
    }
    validators = LocalSchemas({"dated@1": schema}, {"dated@1": {}})
    for value in ({"date": "2025-02-29"}, {"date": "2024-02-29", "extra": 1}, {}):
        with pytest.raises(TychoraError):
            validators.validate("dated@1", value)
    assert validators.validate("dated@1", {"date": "2024-02-29"}) == {
        "date": "2024-02-29"
    }
    deep = {"type": "string"}
    for _ in range(34):
        deep = {"type": "object", "properties": {"child": deep}}
    with pytest.raises(TychoraError):
        LocalSchemas({"deep@1": {"$id": "deep@1", **deep}}, {"deep@1": {}})


def test_import_inclusive_date_preserves_original_and_rejects_invalid():
    assert dict(import_inclusive_end("2024-02-29")) == {
        "original_inclusive_end": "2024-02-29",
        "exclusive_end": "2024-03-01",
    }
    assert import_inclusive_end(None)["exclusive_end"] is None
    for value in ("2025-02-29", "9999-12-31", "20240101"):
        with pytest.raises(TychoraError):
            import_inclusive_end(value)


from pathlib import Path

from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.identity import digest
from tychora.contracts.requests import AndFilter, EqFilter, OrFilter


@pytest.fixture
def builtin_config(tmp_path):
    def load(name="documents"):
        workspace = tmp_path / "workspace.toml"
        workspace.write_text(
            f'schema_version = 1\n[profiles.test]\nbuiltin = "{name}@1"\n'
        )
        return resolve_config(workspace, "test", {})

    return load


def test_installed_packs_discover_complete_zero_count_definitions(builtin_config):
    expected = {
        "law",
        "regulation",
        "decision",
        "standard",
        "guidance",
        "manual",
        "template",
        "procedure",
        "feedback",
        "evidence",
        "example",
        "rationale",
        "research_note",
        "code_file",
        "architecture_decision",
    }
    for profile in ("documents", "code", "verified"):
        registry = Registry.from_config(builtin_config(profile))
        rows = registry.discover_kinds({})
        assert {row["key"] for row in rows} == expected
        for row in rows:
            assert row["count"] == 0
            assert row["description"] and row["examples"] and row["metadata_schema"]
            with pytest.raises(TychoraError):
                registry.validate_metadata(json_string(row["id"]), {})
        assert set(registry.lanes) == {
            "authority",
            "standard",
            "feedback",
            "evidence",
            "example",
            "maintenance",
        }
        assert set(registry.normalizers) == {
            "structured_sections@1",
            "numbered_provisions@1",
            "rule_records@1",
            "feedback_records@1",
            "template_slots@1",
            "code_symbols@1",
        }
        assert set(registry.relations) == {
            f"{name}@1"
            for name in (
                "supports",
                "contradicts",
                "limits",
                "supersedes",
                "reviews",
                "illustrates",
                "derived_from",
                "contains",
                "precedes",
                "calls",
                "imports",
                "inherits",
                "cites",
                "continues_table",
            )
        }
        expected_states = (
            ("accepted",) if profile == "verified" else ("unreviewed", "accepted")
        )
        assert (
            json_object(registry.modes, "compose")["original_review_states"]
            == expected_states
        )
        assert "maintenance" not in json_array(
            json_object(registry.modes, "compose")["allowed_lanes"]
        )
        assert "maintenance" in json_array(
            json_object(registry.modes, "audit")["allowed_lanes"]
        )
        assert json_object(registry.modes, "lookup")["allowed_uses"] == ("lookup",)


def test_builtin_record_schemas_retain_literal_requirements(builtin_config):
    registry = Registry.from_config(builtin_config())
    rule = {
        "requirement": "Retain the recorded source.",
        "target": "record",
        "provenance": ["source-1"],
        "status": "recorded",
        "foundation_ids": ["unit-1"],
    }
    assert registry.validate_record("standard@1", rule) == rule
    for field in rule:
        with pytest.raises(TychoraError):
            registry.validate_record(
                "standard@1", {k: v for k, v in rule.items() if k != field}
            )
    with pytest.raises(TychoraError):
        registry.validate_metadata(
            "law@1",
            {
                "publisher": "Assembly",
                "jurisdiction": "Example",
                "publication_date": "2025-02-29",
            },
        )


def changed_config(config, change):
    data = config.model_dump(mode="json")["registry"]
    change(data)
    return config.model_copy(
        update={
            "registry": FrozenJsonMap(data),
            "registry_digest": digest("registry-v1", data),
        }
    )


@pytest.mark.parametrize(
    "category,key,field",
    [
        ("kinds", "standard@1", field)
        for field in (
            "key",
            "label",
            "description",
            "role",
            "metadata_schema",
            "normalizer",
            "examples",
        )
    ]
    + [
        ("roles", "normative_reference", "intended_use"),
        ("lanes", "standard", "citation_prefix"),
        ("modes", "compose", "original_review_states"),
        ("relations", "supports@1", "require_origin"),
        ("relations", "supports@1", "subject_unit_types"),
        ("facets", "scope.project", "missing"),
    ],
)
def test_missing_semantic_definition_requirements_fail(
    builtin_config, category, key, field
):
    config = changed_config(
        builtin_config(), lambda data: data[category][key].pop(field)
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "category,key,field,value",
    [
        ("kinds", "standard@1", "normalizer", "execute@1"),
        ("kinds", "standard@1", "role", "unknown"),
        ("kinds", "standard@1", "normalizer_options", {"regex": ".*"}),
        ("relations", "supports@1", "require_origin", False),
        ("relations", "supports@1", "require_supports", False),
        ("relations", "supports@1", "methods", ["llm"]),
        ("relations", "supports@1", "object_kinds", ["missing@1"]),
        ("relations", "contains@1", "structural_semantics", "all_descendants"),
        ("relations", "precedes@1", "require_endpoint_supports", False),
        ("lanes", "standard", "citation_prefix", "A"),
        ("modes", "compose", "allowed_lanes", ["missing"]),
        ("modes", "compose", "derived_proxy_review_states", ["rejected"]),
        ("facets", "scope.project", "key", "project"),
        ("facets", "scope.project", "source_binding", "policy.allowed_uses"),
        ("facets", "code.symbol_kind", "filter_scope", "all_units"),
    ],
)
def test_invalid_semantics_fail_closed(builtin_config, category, key, field, value):
    config = changed_config(
        builtin_config(), lambda data: data[category][key].update({field: value})
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"


def test_custom_kind_and_predicate_load_without_code_changes(tmp_path):
    import json

    pack = tmp_path / "custom"
    pack.mkdir()
    (pack / "profile.toml").write_text("""schema_version=1
id="custom"
version="1"
extends=["builtin:documents@1"]
kind_definitions=["kind.toml"]
relation_definitions=["relation.toml"]
[lanes.evidence]
kinds=["evidence@1", "research_note@1", "code_file@1", "observation@1"]
""")
    (pack / "meta.json").write_text(
        json.dumps(
            {
                "$id": "observation-meta@1",
                "type": "object",
                "properties": {"observer": {"type": "string"}},
                "required": ["observer"],
                "additionalProperties": False,
            }
        )
    )
    (pack / "kind.toml").write_text("""schema_version=1
id="observation@1"
key="observation"
label="Observation"
description="An attributed field observation."
role="supporting_evidence"
metadata_schema="meta.json"
normalizer="structured_sections@1"
examples=["Find the field observation."]
""")
    (pack / "relation.toml").write_text("""schema_version=1
id="substantiates@1"
key="substantiates"
label="Substantiates"
description="Explicit support for an observation."
directed=true
subject_unit_types=["paragraph"]
object_unit_types=["paragraph"]
methods=["human"]
default_status="recorded"
revision_binding="explicit"
require_origin=true
require_supports=true
search_directions=["outgoing"]
""")
    workspace = tmp_path / "workspace.toml"
    workspace.write_text('schema_version=1\n[profiles.test]\npath="custom"\n')
    registry = Registry.from_config(resolve_config(workspace, "test", {}))
    assert registry.validate_metadata("observation@1", {"observer": "Survey team"}) == {
        "observer": "Survey team"
    }
    assert json_object(registry.relations, "substantiates@1")["methods"] == ("human",)
    with pytest.raises(TychoraError):
        registry.validate_metadata("observation@1", {"unregistered": "value"})


def test_custom_predicate_may_explicitly_permit_proposed_model_method(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["relations"]["supports@1"].update(methods=["human", "model"]),
    )
    assert json_object(Registry.from_config(config).relations, "supports@1")[
        "methods"
    ] == (
        "human",
        "model",
    )


def test_filters_separate_candidate_selectors_from_context_constraints(builtin_config):
    from tychora.configuration.registry import compile_filter

    registry = Registry.from_config(builtin_config("code"))
    expr = AndFilter(
        children=(
            EqFilter(field="symbol_kind", value="method"),
            EqFilter(field="code.language", value="python"),
        )
    )
    scoped = compile_filter(expr, registry)
    assert scoped.matched_candidates == EqFilter(
        field="code.symbol_kind", value="method"
    )
    assert scoped.all_units == EqFilter(field="code.language", value="python")
    with pytest.raises(TychoraError):
        compile_filter(OrFilter(children=expr.children), registry)
    with pytest.raises(TychoraError):
        compile_filter(
            OrFilter(children=(expr, EqFilter(field="code.language", value="python"))),
            registry,
        )
    for expression in (
        EqFilter(field="unregistered", value="x"),
        EqFilter(field="language", value=1),
    ):
        with pytest.raises(TychoraError):
            compile_filter(expression, registry)


def test_global_is_explicit_and_cannot_broaden_source_scope(builtin_config):
    registry = Registry.from_config(builtin_config())
    assert registry.facet_matches("project", "red", "red")
    assert not registry.facet_matches("project", "red", None, include_global=True)
    assert not registry.facet_matches("project", "red", None, explicit_global=True)
    assert registry.facet_matches(
        "project", "red", None, explicit_global=True, include_global=True
    )
    assert not registry.facet_matches(
        "project", "red", "blue", child_value="red", include_global=True
    )
    assert not registry.facet_matches("project", "red", "red", child_value="blue")
    assert not registry.facet_matches(
        "language", "python", None, explicit_global=True, include_global=True
    )


def test_hint_contributions_are_exact_non_policy_and_capped(builtin_config):
    registry = Registry.from_config(builtin_config())
    hints = {"project": "red", "language": "python", "symbol_kind": "method"}
    values = {
        "scope.project": "red",
        "code.language": "python",
        "code.symbol_kind": "method",
    }
    assert registry.hint_contributions(hints, values) == FrozenJsonMap()
    contributions = registry.hint_contributions(hints, values, bonus=0.008)
    assert sum(json_number(value) for value in contributions.values()) == pytest.approx(
        0.01
    )
    assert (
        registry.hint_contributions({"project": "blue"}, values, bonus=0.008)
        == FrozenJsonMap()
    )
    with pytest.raises(TychoraError):
        registry.hint_contributions({"access": "public"}, values, bonus=0.008)
    with pytest.raises(TychoraError):
        registry.hint_contributions(hints, values, bonus=0.02)


def test_historical_exports_and_owner_activation_require_known_bindings(builtin_config):
    from tychora.configuration.compatibility import require_kind_coverage

    registry = Registry.from_config(builtin_config())
    envelopes = registry.historical_definitions()
    lane_bindings = {
        key: json_string(value) for key, value in registry.lane_bindings.items()
    }
    require_kind_coverage(frozenset(registry.kinds), lane_bindings, envelopes)
    registry.validate_activation(frozenset(registry.kinds), envelopes)
    config = changed_config(
        builtin_config(),
        lambda data: data["compatibility"].update(
            {"kind_lanes": {"missing@0": "standard"}}
        ),
    )
    upgraded = Registry.from_config(config)
    # Unrelated invisible/retained history is not disclosed during a scoped query.
    upgraded_lanes = {
        key: json_string(value) for key, value in upgraded.lane_bindings.items()
    }
    require_kind_coverage(frozenset({"standard@1"}), upgraded_lanes, envelopes)
    with pytest.raises(TychoraError) as err:
        upgraded.validate_activation(frozenset({"standard@1"}), envelopes)
    assert err.value.code == "PROFILE_INCOMPATIBLE"


def test_historical_lane_conflicts_fail_before_selection(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["compatibility"].update(
            {"kind_lanes": {"standard@1": "maintenance"}}
        ),
    )
    with pytest.raises(TychoraError):
        Registry.from_config(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("value_type", "integer"),
        ("source_binding", "metadata.other"),
        ("filter_scope", "matched_candidates"),
        ("allow_global", False),
    ],
)
def test_persisted_facet_semantics_cannot_be_redefined_in_workspace(
    tmp_path, field, value
):
    import json

    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n[configuration.facets."scope.project"]\n'
        + field
        + "="
        + json.dumps(value)
        + "\n"
    )
    with pytest.raises(TychoraError) as err:
        resolve_config(workspace, "test", {})
    assert err.value.code == "INVALID_REQUEST"


def test_code_profile_enables_only_structural_graph_context(builtin_config):
    documents = builtin_config("documents")
    code = builtin_config("code")
    assert documents.values.retrieval.graph.structural == "none"
    assert not documents.values.retrieval.graph.semantic
    assert code.values.retrieval.graph.structural == "code_context@1"
    assert not code.values.retrieval.graph.semantic
    assert code.values.index.graph == "ladybug"


def test_code_structural_context_requires_compatible_backend(tmp_path):
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n[configuration.index]\ngraph="disabled"\n'
    )
    with pytest.raises(TychoraError) as err:
        resolve_config(workspace, "test", {})
    assert err.value.code == "INVALID_REQUEST"


def test_complete_generic_golden_records_validate_and_keep_required_fields(
    builtin_config,
):
    import json

    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "generic-records.json").read_text()
    )
    registry = Registry.from_config(builtin_config())
    assert set(fixtures) == set(registry.kinds)
    for kind, fixture in fixtures.items():
        assert (
            registry.validate_metadata(kind, fixture["metadata"]) == fixture["metadata"]
        )
        with pytest.raises(TychoraError):
            registry.validate_metadata(
                kind, {**fixture["metadata"], "unknown": "not registered"}
            )
        if "record" in fixture:
            assert (
                registry.validate_record(kind, fixture["record"]) == fixture["record"]
            )
            for field in fixture["record"]:
                with pytest.raises(TychoraError):
                    registry.validate_record(
                        kind,
                        {
                            key: value
                            for key, value in fixture["record"].items()
                            if key != field
                        },
                    )


def test_activation_rejects_changed_definition_of_retained_version(builtin_config):
    original = Registry.from_config(builtin_config())
    changed = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["standard@1"].update({"allowed_uses": ["lookup"]}),
    )
    proposed = Registry.from_config(changed)
    with pytest.raises(TychoraError) as err:
        proposed.validate_activation(
            frozenset({"standard@1"}), original.historical_definitions()
        )
    assert err.value.code == "PROFILE_INCOMPATIBLE"


@pytest.mark.parametrize(
    "category,key",
    [
        ("modes", "compose"),
        ("relations", "supports@1"),
        ("roles", "normative_reference"),
        ("facets", "scope.project"),
        ("lanes", "standard"),
        ("kinds", "standard@1"),
    ],
)
def test_malformed_definition_returns_typed_error(builtin_config, category, key):
    config = changed_config(
        builtin_config(), lambda data: data[category].update({key: None})
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"


def test_kind_discovery_materializes_bound_original_schemas(builtin_config):
    registry = Registry.from_config(builtin_config())
    row = next(
        row
        for row in registry.discover_kinds({"standard@1": 2})
        if row["id"] == "standard@1"
    )
    assert row["count"] == 2
    assert json_object(row, "schema_ids")["metadata_schema"] == "standard-metadata@1"
    assert json_object(row, "metadata_schema_definition")["required"] == (
        "title",
        "issuer",
        "edition",
    )
    assert json_object(row, "record_schema_definition")["required"] == (
        "requirement",
        "target",
        "provenance",
        "status",
        "foundation_ids",
    )


def test_schema_replay_preserves_literal_percent_in_pointer_keys():
    schema = {
        "$id": "literal@1",
        "$ref": "#/$defs/x%2Fy",
        "$defs": {"x%2Fy": {"type": "integer"}},
    }
    local = LocalSchemas(
        {"literal@1": schema},
        {
            "literal@1": {
                "/$ref": {"schema_id": "literal@1", "fragment": "/$defs/x%2Fy"}
            }
        },
    )
    assert local.validate("literal@1", 4) == 4
    with pytest.raises(TychoraError):
        local.validate("literal@1", "wrong type")


@pytest.mark.parametrize(
    "constraints",
    [
        {"items": {"type": "integer"}},
        {"minItems": 1},
        {"items": {"type": "integer"}, "maxItems": 4097},
        {"items": {"type": "integer"}, "maxItems": True},
        {"items": {"type": "integer"}, "maxItems": -1},
    ],
)
def test_implicit_array_applicators_require_bounded_max_items(constraints):
    schema = {"$id": "bounded@1", **constraints}
    with pytest.raises(TychoraError) as err:
        LocalSchemas({"bounded@1": schema}, {"bounded@1": {}})
    assert err.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("explicit_type", [None, "array", ["array", "null"]])
def test_bounded_array_forms_enforce_the_4096_item_boundary(explicit_type):
    schema = {"$id": "bounded@1", "items": {"type": "integer"}, "maxItems": 4096}
    if explicit_type is not None:
        schema["type"] = explicit_type
    local = LocalSchemas({"bounded@1": schema}, {"bounded@1": {}})
    validated = local.validate("bounded@1", [1] * 4096)
    assert isinstance(validated, list)
    assert len(validated) == 4096
    with pytest.raises(TychoraError) as err:
        local.validate("bounded@1", [1] * 4097)
    assert err.value.code == "INVALID_REQUEST"


def test_pack_resolution_rejects_unbounded_implicit_array_schema(tmp_path):
    (tmp_path / "profile.toml").write_text(
        'schema_version=1\nid="bounded"\nversion="1"\nschema_definitions=["array.json"]\n'
    )
    (tmp_path / "array.json").write_text(
        '{"$id":"bounded@1","items":{"type":"integer"}}'
    )
    workspace = tmp_path / "workspace.toml"
    workspace.write_text('schema_version=1\n[profiles.test]\npath="."\n')
    with pytest.raises(TychoraError) as err:
        resolve_config(workspace, "test", {})
    assert err.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "offset", ["+00:99", "-00:99", "+01:60", "-01:60", "+24:00", "-24:00"]
)
def test_date_time_rejects_invalid_timezone_components(offset):
    schema = {"$id": "timestamp@1", "type": "string", "format": "date-time"}
    local = LocalSchemas({"timestamp@1": schema}, {"timestamp@1": {}})
    with pytest.raises(TychoraError) as err:
        local.validate("timestamp@1", "2024-01-01T00:00:00" + offset)
    assert err.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "offset", ["Z", "z", "+00:00", "-00:00", "+23:59", "-23:59", "+05:45", "-03:30"]
)
def test_date_time_preserves_valid_timezone_boundaries(offset):
    schema = {"$id": "timestamp@1", "type": "string", "format": "date-time"}
    local = LocalSchemas({"timestamp@1": schema}, {"timestamp@1": {}})
    value = "2024-02-29T23:59:59.123" + offset
    assert local.validate("timestamp@1", value) == value


@pytest.mark.parametrize(
    "category,key,field,value",
    [
        ("relations", "supports@1", "self_links", "false"),
        ("relations", "supports@1", "self_links", 0),
        ("relations", "supports@1", "self_links", None),
        ("relations", "supports@1", "cardinality", "invalid"),
        ("relations", "supports@1", "cardinality", None),
        ("modes", "compose", "required_lanes", {"standard": 17}),
        ("modes", "compose", "required_lanes", ["standard", "standard"]),
        ("modes", "compose", "required_lanes", [17]),
        ("modes", "compose", "required_lanes", "standard"),
    ],
)
def test_optional_relation_and_mode_fields_reject_malformed_values(
    builtin_config, category, key, field, value
):
    config = changed_config(
        builtin_config(), lambda data: data[category][key].update({field: value})
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "category,key,field,value",
    [
        ("relations", "supports@1", "self_links", False),
        ("relations", "supports@1", "self_links", True),
        ("relations", "supports@1", "cardinality", "one_to_one"),
        ("relations", "supports@1", "cardinality", "one_to_many"),
        ("relations", "supports@1", "cardinality", "many_to_one"),
        ("relations", "supports@1", "cardinality", "many_to_many"),
        ("modes", "compose", "required_lanes", []),
        ("modes", "compose", "required_lanes", ["standard"]),
    ],
)
def test_optional_relation_and_mode_fields_preserve_supported_values(
    builtin_config, category, key, field, value
):
    config = changed_config(
        builtin_config(), lambda data: data[category][key].update({field: value})
    )
    registry = Registry.from_config(config)
    assert json_object(registry.artifact, category, key)[field] == (
        tuple(value) if isinstance(value, list) else value
    )


@pytest.mark.parametrize(
    "requirements",
    [
        [
            {
                "requirement_id": "r",
                "target": "lane",
                "value": "hidden",
                "when": {"exists": "project"},
            }
        ],
        [
            {
                "requirement_id": "r",
                "target": "lane",
                "value": "standard",
                "when": {"eval": "yes"},
            }
        ],
        [{"requirement_id": "r", "target": "lane", "value": "standard", "minimum": 0}],
        [
            {"requirement_id": "r", "target": "lane", "value": "standard"},
            {"requirement_id": "r", "target": "kind", "value": "manual@1"},
        ],
    ],
)
def test_mode_requirements_reject_unknown_targets_and_unbounded_conditions(
    builtin_config, requirements
):
    config = changed_config(
        builtin_config(),
        lambda data: data["modes"]["compose"].update({"requirements": requirements}),
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"


def test_mode_requirement_accepts_bounded_explicit_context(builtin_config):
    requirement = {
        "requirement_id": "r",
        "description": "Needs standard",
        "target": "lane",
        "value": "standard",
        "when": {"and": [{"eq": ["mode", "compose"]}, {"exists": "project"}]},
    }
    config = changed_config(
        builtin_config(),
        lambda data: data["modes"]["compose"].update({"requirements": [requirement]}),
    )
    assert (
        Registry.from_config(config).model_dump(mode="json")["modes"]["compose"][
            "requirements"
        ][0]
        == requirement
    )


def _counting_check_schema(monkeypatch):
    from tychora.configuration import schemas as module

    module._checked_schemas.clear()
    calls: list[str] = []
    original = module.Draft202012Validator.check_schema

    def counting(schema, *args, **kwargs):
        calls.append(schema["$id"])
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(module.Draft202012Validator, "check_schema", counting)
    return module, calls


def test_schema_metaschema_check_runs_once_per_schema_content(monkeypatch):
    _, calls = _counting_check_schema(monkeypatch)
    schema = {"$id": "cached@1", "type": "integer", "minimum": 1}
    first = LocalSchemas({"cached@1": schema}, {"cached@1": {}})
    second = LocalSchemas({"cached@1": dict(schema)}, {"cached@1": {}})
    assert calls == ["cached@1"]
    assert second.validate("cached@1", 2) == first.validate("cached@1", 2) == 2
    changed = {"$id": "cached@1", "type": "integer", "minimum": 2}
    LocalSchemas({"cached@1": changed}, {"cached@1": {}})
    assert calls == ["cached@1", "cached@1"]


def test_schema_rejected_by_the_metaschema_is_rejected_every_time(monkeypatch):
    module, calls = _counting_check_schema(monkeypatch)
    invalid = {"$id": "invalid@1", "type": "not-a-json-type"}
    for _ in range(2):
        with pytest.raises(TychoraError) as err:
            LocalSchemas({"invalid@1": invalid}, {"invalid@1": {}})
        assert err.value.code == "INVALID_REQUEST"
    assert calls == ["invalid@1", "invalid@1"]
    assert not module._checked_schemas


def test_schema_check_cache_is_bounded(monkeypatch):
    module, calls = _counting_check_schema(monkeypatch)
    monkeypatch.setattr(module, "_SCHEMA_CHECK_CACHE_SIZE", 2)
    for minimum in (1, 2, 3):
        schema = {"$id": "bounded@1", "type": "integer", "minimum": minimum}
        LocalSchemas({"bounded@1": schema}, {"bounded@1": {}})
    assert len(module._checked_schemas) == 2
    LocalSchemas(
        {"bounded@1": {"$id": "bounded@1", "type": "integer", "minimum": 1}},
        {"bounded@1": {}},
    )
    assert len(calls) == 4


@pytest.mark.parametrize(
    "value",
    [
        [{"pattern": "(", "level": "article"}],
        [{"pattern": "^A", "level": "paragraph"}],
        "^Article",
    ],
)
def test_invalid_heading_levels_are_refused_at_load(builtin_config, value):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["standard@1"].update({"heading_levels": value}),
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.section_levels_invalid"


def test_heading_levels_load_when_valid(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["standard@1"].update(
            {"heading_levels": [{"pattern": r"^\d+(\.\d+)*", "level": "clause"}]}
        ),
    )
    kind = cast(Mapping[str, Any], Registry.from_config(config).kinds["standard@1"])
    assert kind["heading_levels"]


def test_context_level_defaults_to_top_level_chapter(builtin_config):
    assert Registry.from_config(builtin_config()).context_level("manual@1") == Depth(
        n=1
    )


def test_context_level_reads_the_kind_value(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["law@1"].update({"context_level": "article"}),
    )
    assert Registry.from_config(config).context_level("law@1") == "article"


@pytest.mark.parametrize("value", ["depth:0", "paragraph", 3])
def test_invalid_context_level_is_refused_at_load(builtin_config, value):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["law@1"].update({"context_level": value}),
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.reason == "kind.section_levels_invalid"


def test_unknown_kind_has_no_context_level(builtin_config):
    with pytest.raises(TychoraError) as err:
        Registry.from_config(builtin_config()).context_level("missing@1")
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.unknown"
