"""Explicit managed-owner future-call authority; accepted scopes stay immutable."""

from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope

from .collection_policy import parse_collection_policy


def require_all_collections(owner, grant: RequestScope, operation: str) -> None:
    """Refuse a whole-workspace operation unless the grant covers every collection."""
    with owner.store.uow.read() as tx:
        collections = tx.workspace.collection_ids()
    if not collections <= grant.collection_ids:
        raise TychoraError(
            "FORBIDDEN_SCOPE",
            f"{operation} requires all collection grants",
            reason="scope.whole_workspace",
        )


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


def authorize_workspace(
    owner, scope: RequestScope, *, capability: str = "manage"
) -> RequestScope:
    """Prove canonical complete collection, access and use authority."""
    import json

    grant = owner.scope.narrow(scope)
    if capability not in grant.capabilities:
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Workspace operation is outside the grant."
        )
    db = owner.db
    collections = db.execute(
        "SELECT collection_id,access_floor FROM collections WHERE workspace_id=?",
        (owner.workspace_id,),
    ).fetchall()
    floors = db.execute(
        "SELECT s.access_floor,r.access_rank FROM sources s "
        "JOIN collections c USING(collection_id) LEFT JOIN revisions r USING(source_id) "
        "WHERE c.workspace_id=?",
        (owner.workspace_id,),
    ).fetchall()
    uses = set()
    for definitions in (
        owner.store.registry.kinds,
        owner.store.registry.modes,
    ):
        for definition in definitions.values():
            uses.update(definition.get("allowed_uses", ()))
    for raw, definition in db.execute(
        "SELECT r.allowed_uses_json,k.definition_json FROM revisions r "
        "JOIN kinds k USING(kind_id) JOIN sources s USING(source_id) "
        "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
        (owner.workspace_id,),
    ):
        uses.update(json.loads(raw))
        uses.update(json.loads(definition)["kind"].get("allowed_uses", ()))
    if (
        not grant.collection_ids
        or any(
            cid not in grant.collection_ids or floor > grant.access_ceiling
            for cid, floor in collections
        )
        or any(
            floor is not None and floor > grant.access_ceiling
            for row in floors
            for floor in row
        )
        or not uses <= grant.uses
    ):
        raise TychoraError(
            "FORBIDDEN_SCOPE",
            "Workspace administration requires whole-workspace access and uses.",
        )

    return grant
