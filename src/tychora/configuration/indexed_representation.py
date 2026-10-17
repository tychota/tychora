"""Stable representation semantics shared by activation and pinned reads."""

from tychora.contracts.identity import digest

from .models import ResolvedConfig


def indexed_representation_identity(config: ResolvedConfig) -> str:
    providers = config.values.providers
    embeddings = {}
    for modality in ("text", "image"):
        settings = getattr(providers, modality).model_dump(mode="json")
        settings.pop("credential_ref", None)
        embeddings[modality] = settings
    return digest(
        "indexed-representation-compat-v1",
        embeddings,
        config.values.index.model_dump(mode="json"),
        config.extraction_fingerprint,
    )


def pinned_runtime_compatible(
    retained: ResolvedConfig, current: ResolvedConfig
) -> bool:
    """Permit credential rotation and monotonic offline/source-root tightening."""
    from .additive_compatibility import qualified_additive_registry_extension

    if qualified_additive_registry_extension(retained, current):
        return True
    old = retained.model_dump(mode="json")
    new = current.model_dump(mode="json")
    old_values, new_values = old["values"], new["values"]
    if old_values["offline"] and not new_values["offline"]:
        return False
    if not set(new_values["acquisition"]["roots"]) <= set(
        old_values["acquisition"]["roots"]
    ):
        return False
    for value in (old, new):
        # These are derived from the changed credential/rerank values. Compare
        # every source field instead, including policy, limits and registry.
        for field in (
            "config_sha",
            "index_fingerprint",
            "query_fingerprint",
            "origins",
            "acquire_fingerprint",
        ):
            value.pop(field)
        value["values"].pop("offline")
        value["values"]["acquisition"].pop("roots")
        providers = value["values"]["providers"]
        for modality in ("text", "image"):
            providers[modality].pop("credential_ref")
        providers.pop("rerank")
    return old == new and indexed_representation_identity(
        retained
    ) == indexed_representation_identity(current)
