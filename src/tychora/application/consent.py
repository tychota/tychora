"""Explicit owner-coordinated collection and installation-owner upload consent."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    MaintenancePlan,
    MaintenanceResult,
    OperationReceipt,
    ProviderUploadConsentMaintenance,
    ProviderUploadConsentState,
)
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.models import ResolvedConfig


# Anthropic is reserved for the separately qualified adapter/configuration task.
REGISTERED_UPLOAD_PROVIDERS = frozenset({"voyage"})


def enabled_upload_providers(config: ResolvedConfig) -> frozenset[str]:
    configured = config.values.providers
    return frozenset(
        {"voyage"}
        if "voyage"
        in {
            configured.text.provider,
            configured.image.provider,
            configured.rerank.provider,
        }
        else ()
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate collection policy field")
        value[key] = item
    return value


def _providers(value: object, *, registered_only: bool = True) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or any(type(item) is not str or not item.strip() for item in value)
        or len(set(value)) != len(value)
        or (registered_only and not set(value) <= REGISTERED_UPLOAD_PROVIDERS)
    ):
        raise TychoraError("INTEGRITY_ERROR", "Collection upload policy is malformed.")
    return frozenset(value)


def parse_upload_policy(
    raw: str, *, allow_injected_provider_ids: bool = False
) -> tuple[frozenset[str], frozenset[str]]:
    """Schema-v1 extension: absent owner grant means deny without migration."""
    try:
        policy = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, TypeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Collection upload policy is malformed."
        ) from None
    if (
        not isinstance(policy, dict)
        or set(policy)
        not in (
            {"schema_version", "upload_providers"},
            {"schema_version", "upload_providers", "owner_upload_providers"},
        )
        or type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
    ):
        raise TychoraError("INTEGRITY_ERROR", "Collection upload policy is malformed.")
    collection = _providers(
        policy["upload_providers"], registered_only=not allow_injected_provider_ids
    )
    owner = _providers(policy.get("owner_upload_providers", []))
    return collection, owner


def restore_default_owner_upload_scope(owner: WorkspaceOwner) -> RequestScope:
    """Derive a new immutable future-call grant only for the default owner."""
    if not owner.managed_default_owner:
        return owner.scope
    enabled = enabled_upload_providers(owner.config)
    granted: set[str] = set()
    for collection_id in sorted(owner.scope.collection_ids):
        row = owner.db.execute(
            "SELECT policy_json FROM collections WHERE collection_id=? AND workspace_id=?",
            (collection_id, owner.workspace_id),
        ).fetchone()
        if row is not None:
            _, owner_providers = parse_upload_policy(row[0])
            granted.update(owner_providers & enabled)
    owner.scope = owner.scope.model_copy(
        update={"upload_providers": frozenset(granted)}
    )
    return owner.scope


def apply_upload_consent(
    owner: WorkspaceOwner,
    scope: RequestScope,
    request: ProviderUploadConsentMaintenance,
) -> MaintenanceResult:
    """Change both independent policy sets in one epoch transaction."""
    grant = owner.scope.narrow(scope)
    collection_id = request.collection_id
    if collection_id == "default":
        default = owner.db.execute(
            "SELECT collection_id FROM collections WHERE workspace_id=? AND slug='default'",
            (owner.workspace_id,),
        ).fetchone()
        collection_id = default[0] if default is not None else "default"
    if "manage" not in grant.capabilities or collection_id not in grant.collection_ids:
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Collection is outside the management grant."
        )
    if owner.db.in_transaction:
        raise TychoraError("INTEGRITY_ERROR", "Consent requires a fresh policy write.")
    owner.db.execute("BEGIN IMMEDIATE")
    try:
        row = owner.db.execute(
            "SELECT access_floor,policy_json FROM collections "
            "WHERE collection_id=? AND workspace_id=?",
            (collection_id, owner.workspace_id),
        ).fetchone()
        if row is None or row[0] > grant.access_ceiling:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Collection is outside the management grant."
            )
        collection_set, owner_set = parse_upload_policy(row[1])
        if request.action == "grant":
            if request.provider not in enabled_upload_providers(owner.config):
                raise TychoraError(
                    "INVALID_REQUEST", "Provider is not enabled in this profile."
                )
        elif request.provider not in REGISTERED_UPLOAD_PROVIDERS or (
            request.provider not in enabled_upload_providers(owner.config)
            and request.provider not in collection_set | owner_set
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Provider has no registered consent to revoke."
            )
        collection_updated = set(collection_set)
        owner_updated = set(owner_set)
        for selected in (collection_updated, owner_updated):
            if request.action == "grant":
                selected.add(request.provider)
            else:
                selected.discard(request.provider)
        owner.db.execute(
            "UPDATE collections SET policy_json=? WHERE collection_id=?",
            (
                canonical_bytes(
                    {
                        "schema_version": 1,
                        "upload_providers": sorted(collection_updated),
                        "owner_upload_providers": sorted(owner_updated),
                    }
                ).decode("utf-8"),
                collection_id,
            ),
        )
        owner.db.execute(
            "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id=?",
            (owner.workspace_id,),
        )
        epoch = owner.db.execute(
            "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
            (owner.workspace_id,),
        ).fetchone()[0]
        owner.db.commit()
    except BaseException:
        owner.db.rollback()
        raise
    return MaintenanceResult(
        plan=MaintenancePlan(
            operation="provider-upload-consent",
            target_ids=(collection_id,),
            actions=(
                f"{request.action} {request.provider} in collection upload policy",
                f"{request.action} {request.provider} for default installation owner",
                "reopen the default owner session to acquire a new grant"
                if request.action == "grant"
                else "revocation blocks current and queued uploads immediately",
            ),
        ),
        receipt=OperationReceipt(
            operation="provider-upload-consent",
            receipt_id=str(uuid4()),
            target_ids=(collection_id,),
            completed_at=datetime.now(UTC),
        ),
        upload_consent=ProviderUploadConsentState(
            collection_id=collection_id,
            provider=cast(Literal["voyage", "anthropic"], request.provider),
            action=request.action,
            collection_upload_providers=tuple(sorted(collection_updated)),
            owner_upload_providers=tuple(sorted(owner_updated)),
            policy_epoch=epoch,
            default_owner_requires_reopen=request.action == "grant",
        ),
    )


async def run_public_upload_consent(
    owner: WorkspaceOwner,
    scope: RequestScope,
    request: ProviderUploadConsentMaintenance,
) -> MaintenanceResult:
    return await owner.run_blocking(
        lambda: apply_upload_consent(owner, scope, request), ExecutionContext()
    )
