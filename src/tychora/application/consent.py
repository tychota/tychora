"""Explicit owner-coordinated collection and installation-owner upload consent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from tychora.contracts.errors import TychoraError
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


from .collection_policy import REGISTERED_UPLOAD_PROVIDERS, parse_collection_policy


def enabled_upload_providers(config: ResolvedConfig) -> frozenset[str]:
    configured = config.values.providers
    enabled = set(
        {"voyage"}
        if "voyage"
        in {
            configured.text.provider,
            configured.image.provider,
            configured.rerank.provider,
        }
        else ()
    )
    if configured.vision.provider == "anthropic":
        enabled.add("anthropic")
    return frozenset(enabled)


def parse_upload_policy(
    raw: str, *, allow_injected_provider_ids: bool = False
) -> tuple[frozenset[str], frozenset[str]]:
    policy = parse_collection_policy(
        raw, allow_injected_provider_ids=allow_injected_provider_ids
    )
    return frozenset(policy.upload_providers), frozenset(policy.owner_upload_providers)


def restore_default_owner_upload_scope(owner: WorkspaceOwner) -> RequestScope:
    from .workspace_authority import refresh_managed_owner

    return refresh_managed_owner(owner)


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
        policy = parse_collection_policy(row[1])
        collection_set, owner_set = (
            set(policy.upload_providers),
            set(policy.owner_upload_providers),
        )
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
        if collection_updated != collection_set or owner_updated != owner_set:
            updated = policy.model_copy(
                update={
                    "upload_providers": tuple(sorted(collection_updated)),
                    "owner_upload_providers": tuple(sorted(owner_updated)),
                }
            )
            owner.db.execute(
                "UPDATE collections SET policy_json=? WHERE collection_id=?",
                (updated.model_dump_json(), collection_id),
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
    restore_default_owner_upload_scope(owner)
    return MaintenanceResult(
        plan=MaintenancePlan(
            operation="provider-upload-consent",
            target_ids=(collection_id,),
            actions=(
                f"{request.action} {request.provider} in collection upload policy",
                f"{request.action} {request.provider} for default installation owner",
                "refresh the managed session handshake to acquire the current grant"
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
            default_owner_requires_reopen=False,
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
