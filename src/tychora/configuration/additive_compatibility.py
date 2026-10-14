"""Source-level proof for adding qualified kinds without reinterpreting history."""

from tychora.contracts.errors import TychoraError

from .models import ResolvedConfig
from .registry import Registry
from .resolve import validate_retained_config


def qualified_additive_registry_extension(
    retained: ResolvedConfig, current: ResolvedConfig
) -> bool:
    """Prove a strictly additive vocabulary change; never substitute fingerprints."""
    if retained.registry_digest == current.registry_digest:
        return False
    try:
        validate_retained_config(retained)
        validate_retained_config(current)
        Registry.from_config(retained)
        Registry.from_config(current)
    except (TychoraError, ValueError, KeyError, TypeError):
        return False
    old = retained.model_dump(mode="json")
    new = current.model_dump(mode="json")
    if old["profile"] != new["profile"]:
        return False
    a, b = old["values"], new["values"]
    if a["offline"] and not b["offline"]:
        return False
    if not set(b["acquisition"]["roots"]) <= set(a["acquisition"]["roots"]):
        return False
    for values in (a, b):
        values.pop("offline")
        values["acquisition"].pop("roots")
        for modality in ("text", "image"):
            values["providers"][modality].pop("credential_ref")
        values["providers"].pop("rerank")
    if a != b:
        return False
    a, b = old["registry"], new["registry"]
    added = set(b["kinds"]) - set(a["kinds"])
    if not added or set(a) != set(b):
        return False
    for key in added:
        kind = b["kinds"][key]
        # This extension is qualified for the existing structured document adapters.
        if kind["normalizer"] != "structured_sections@1" or kind.get(
            "normalizer_options"
        ):
            return False
    for category in (
        "kinds",
        "schemas",
        "schema_reference_bindings",
        "schema_bindings",
    ):
        if any(b[category].get(key) != value for key, value in a[category].items()):
            return False
    if set(a["definition_schema_bindings"]) != set(b["definition_schema_bindings"]):
        return False
    for category in ("kinds", "relations"):
        if any(
            b["definition_schema_bindings"][category].get(key) != value
            for key, value in a["definition_schema_bindings"][category].items()
        ):
            return False
    if set(a["lanes"]) != set(b["lanes"]):
        return False
    for key, before in a["lanes"].items():
        after = b["lanes"][key]
        if {k: v for k, v in before.items() if k != "kinds"} != {
            k: v for k, v in after.items() if k != "kinds"
        }:
            return False
        if [kind for kind in after["kinds"] if kind not in added] != before["kinds"]:
            return False
        if set(after["kinds"]) & added and not any(
            key in mode["allowed_lanes"] for mode in a["modes"].values()
        ):
            return False
    permitted = {
        "kinds",
        "schemas",
        "schema_reference_bindings",
        "schema_bindings",
        "definition_schema_bindings",
        "lanes",
    }
    return all(a[key] == b[key] for key in a.keys() - permitted)
