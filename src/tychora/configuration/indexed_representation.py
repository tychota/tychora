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
    """Permit only credential and reranker rotation around a pinned config."""
    old = retained.model_dump(mode="json")
    new = current.model_dump(mode="json")
    for value in (old, new):
        # These are derived from the changed credential/rerank values. Compare
        # every source field instead, including policy, limits and registry.
        for field in (
            "config_sha",
            "index_fingerprint",
            "query_fingerprint",
            "origins",
        ):
            value.pop(field)
        providers = value["values"]["providers"]
        for modality in ("text", "image"):
            providers[modality].pop("credential_ref")
        providers.pop("rerank")
    return old == new and indexed_representation_identity(
        retained
    ) == indexed_representation_identity(current)
