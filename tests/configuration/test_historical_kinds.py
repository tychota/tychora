import pytest

from tychora.configuration.compatibility import require_kind_coverage
from tychora.contracts.errors import TychoraError


def test_missing_historical_kind_is_not_silently_dropped():
    with pytest.raises(TychoraError) as err:
        require_kind_coverage(frozenset({"standard@1"}), {}, {})
    assert err.value.code == "PROFILE_INCOMPATIBLE"


from copy import deepcopy

from tychora.configuration.compatibility import validate_historical_record
from tychora.contracts.identity import digest


def historical_kind():
    metadata = {
        "$id": "standard-meta@1",
        "type": "object",
        "properties": {"issuer": {"type": "string"}},
        "required": ["issuer"],
        "additionalProperties": False,
    }
    record = {
        "$id": "rule@1",
        "type": "object",
        "properties": {"requirement": {"type": "string"}},
        "required": ["requirement"],
        "additionalProperties": False,
    }
    kind = {
        "id": "standard@1",
        "key": "standard",
        "label": "Standard",
        "description": "An original requirement.",
        "role": "normative_reference",
        "normalizer": "rule_records@1",
        "metadata_schema": "old/meta.json",
        "record_schema": "old/rule.json",
        "examples": ["Find the requirement."],
        "allowed_uses": ["audit"],
    }
    schemas = {"standard-meta@1": metadata, "rule@1": record}
    envelope = {
        "kind": kind,
        "kind_digest": digest("kind-v1", kind),
        "schemas": schemas,
        "schema_digests": {
            key: digest("schema-v1", schema) for key, schema in schemas.items()
        },
        "schema_bindings": {
            "metadata_schema": "standard-meta@1",
            "record_schema": "rule@1",
        },
        "schema_reference_bindings": {key: {} for key in schemas},
    }
    return {**envelope, "envelope_digest": digest("historical-kind-v1", envelope)}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["schemas"]["rule@1"].update({"required": []}),
        lambda value: value["kind"].update({"allowed_uses": ["compose"]}),
        lambda value: value["schema_bindings"].update({"record_schema": "new@2"}),
        lambda value: value["schemas"].pop("standard-meta@1"),
        lambda value: value["schema_reference_bindings"].pop("rule@1"),
    ],
)
def test_historical_coverage_verifies_original_definitions_and_schemas(mutation):
    value = historical_kind()
    mutation(value)
    with pytest.raises(TychoraError) as err:
        require_kind_coverage(
            frozenset({"standard@1"}), {"standard@1": "standard"}, {"standard@1": value}
        )
    assert err.value.code == "PROFILE_INCOMPATIBLE"
    assert "issuer" not in str(err.value)


def test_historical_record_uses_unchanged_original_schema():
    value = historical_kind()
    original = deepcopy(value)
    require_kind_coverage(
        frozenset({"standard@1"}), {"standard@1": "maintenance"}, {"standard@1": value}
    )
    assert validate_historical_record(
        value, {"requirement": "Keep the original wording."}
    ) == {"requirement": "Keep the original wording."}
    with pytest.raises(TychoraError):
        validate_historical_record(value, {"replacement_field": "new schema only"})
    assert value == original


def test_missing_list_is_sorted_and_only_exposes_scoped_kinds():
    with pytest.raises(TychoraError) as err:
        require_kind_coverage(frozenset({"zeta@1", "alpha@1"}), {}, {"hidden@1": {}})
    assert str(err.value).endswith("alpha@1, zeta@1")
    assert "hidden" not in str(err.value)
