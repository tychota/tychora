"""One fail-closed parser for retained v1 and lifecycle-aware v2 policies."""

import json

from tychora.contracts.collections import CollectionPolicyV2
from tychora.contracts.errors import TychoraError

REGISTERED_UPLOAD_PROVIDERS = frozenset({"voyage", "anthropic"})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy key")
        result[key] = value
    return result


def parse_collection_policy(
    raw: str, *, allow_injected_provider_ids=False
) -> CollectionPolicyV2:
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            raise ValueError
        if value["schema_version"] == 1:
            if not {
                "schema_version",
                "upload_providers",
            } <= value.keys() or not value.keys() <= {
                "schema_version",
                "upload_providers",
                "owner_upload_providers",
            }:
                raise ValueError
            value["schema_version"] = 2
        policy = CollectionPolicyV2.from_mapping(value)
        for providers in (policy.upload_providers, policy.owner_upload_providers):
            if len(set(providers)) != len(providers) or any(
                not p.strip() for p in providers
            ):
                raise ValueError
        if (
            not allow_injected_provider_ids
            and not set(policy.upload_providers) <= REGISTERED_UPLOAD_PROVIDERS
        ):
            raise ValueError
        if not set(policy.owner_upload_providers) <= REGISTERED_UPLOAD_PROVIDERS:
            raise ValueError
        return policy
    except (ValueError, TypeError, KeyError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Collection policy is malformed."
        ) from None


def collection_is_active(raw: str) -> bool:
    return (
        parse_collection_policy(raw, allow_injected_provider_ids=True).archived_at
        is None
    )


def require_active_collection(raw: str) -> None:
    if not collection_is_active(raw):
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Archived collection does not admit new work."
        )
