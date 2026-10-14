"""Explicit managed-owner future-call authority; accepted scopes stay immutable."""

from tychora.contracts.requests import RequestScope

from .collection_policy import parse_collection_policy


def refresh_managed_owner(owner) -> RequestScope:
    if not owner.managed_default_owner:
        return owner.scope
    from .consent import enabled_upload_providers

    enabled = enabled_upload_providers(owner.config)
    collections = set()
    providers = set()
    for cid, floor, raw in owner.db.execute(
        "SELECT collection_id,access_floor,policy_json FROM collections WHERE workspace_id=?",
        (owner.workspace_id,),
    ):
        if floor <= owner.scope.access_ceiling:
            collections.add(cid)
            policy = parse_collection_policy(raw)
            providers.update(
                set(policy.owner_upload_providers)
                & set(policy.upload_providers)
                & enabled
            )
    owner.scope = owner.scope.model_copy(
        update={
            "collection_ids": frozenset(collections),
            "upload_providers": frozenset(providers),
        }
    )
    for callback in tuple(owner._managed_scope_listeners):
        callback(owner.scope)
    return owner.scope
