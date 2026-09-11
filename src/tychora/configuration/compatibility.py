"""Explicit historical presentation bindings; never migrate stored evidence."""

from collections.abc import Mapping

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue


def require_kind_coverage(
    readable_kinds: frozenset[str],
    lane_bindings: Mapping[str, str],
    historical_definitions: Mapping[str, JsonValue],
) -> None:
    """Check the entire readable membership before applying any lane filter."""
    missing = sorted(
        kind
        for kind in readable_kinds
        if kind not in lane_bindings or kind not in historical_definitions
    )
    if missing:
        raise TychoraError(
            "PROFILE_INCOMPATIBLE",
            "Missing explicit kind/schema bindings: " + ", ".join(missing),
        )

    for kind in sorted(readable_kinds):
        _verify_historical(historical_definitions[kind], kind)


def _verify_historical(value: JsonValue, expected_kind: str | None = None) -> dict:
    """Verify every original dependency; errors contain no metadata or private paths."""
    import json

    from tychora.contracts.identity import canonical_bytes, digest

    from .schemas import LocalSchemas

    try:
        envelope = json.loads(canonical_bytes(value))
        if not isinstance(envelope, dict) or set(envelope) != {
            "kind",
            "kind_digest",
            "schemas",
            "schema_digests",
            "schema_bindings",
            "schema_reference_bindings",
            "envelope_digest",
        }:
            raise ValueError("Invalid historical envelope")
        body = {key: item for key, item in envelope.items() if key != "envelope_digest"}
        if digest("historical-kind-v1", body) != envelope["envelope_digest"]:
            raise ValueError("Envelope digest mismatch")
        kind = envelope["kind"]
        if expected_kind is not None and kind["id"] != expected_kind:
            raise ValueError("Kind identity mismatch")
        if digest("kind-v1", kind) != envelope["kind_digest"]:
            raise ValueError("Kind digest mismatch")
        schemas = envelope["schemas"]
        if set(schemas) != set(envelope["schema_digests"]):
            raise ValueError("Missing schema digests")
        for key, schema in schemas.items():
            if digest("schema-v1", schema) != envelope["schema_digests"][key]:
                raise ValueError("Original schema digest mismatch")
        expected = {
            field for field in ("metadata_schema", "record_schema") if field in kind
        }
        if (
            "metadata_schema" not in expected
            or set(envelope["schema_bindings"]) != expected
        ):
            raise ValueError("Missing original schema binding")
        if any(key not in schemas for key in envelope["schema_bindings"].values()):
            raise ValueError("Unresolved original schema")
        LocalSchemas(schemas, envelope["schema_reference_bindings"])
        return envelope
    except (ValueError, TypeError, KeyError, AttributeError, TychoraError):
        raise TychoraError(
            "PROFILE_INCOMPATIBLE", "Invalid original kind/schema artifact"
        ) from None


def validate_historical_record(envelope: JsonValue, value: JsonValue) -> JsonValue:
    """Validate using the immutable old schema; lane compatibility changes nothing."""
    from .schemas import LocalSchemas

    original = _verify_historical(envelope)
    if "record_schema" not in original["schema_bindings"]:
        raise TychoraError(
            "PROFILE_INCOMPATIBLE", "Original record schema is unavailable"
        )
    return LocalSchemas(
        original["schemas"], original["schema_reference_bindings"]
    ).validate(original["schema_bindings"]["record_schema"], value)
