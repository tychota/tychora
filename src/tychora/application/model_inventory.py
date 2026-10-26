"""Read-only qualified model inventory and bounded cache inspection."""

import json
import os
import stat
import time
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

from tychora.contracts.administration import (
    ModelCatalog,
    ModelReadiness,
    ModelSelectionCapability,
    ModelSelections,
    ProviderInventory,
    VerificationReceipt,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

from .collection_policy import parse_collection_policy
from .settings_draft import QUALIFIED_MODELS
from .setup import ModelAssetPlan, SetupService, _open_directory
from .workspace_authority import authorize_workspace


class ModelInventoryService:
    def __init__(self, owner, repository):
        self.owner = owner
        self.repository = repository

    def _setup(self):
        return SetupService(
            Path(os.environ.get("TYCHORA_MODEL_CACHE", self.owner.path / "models")),
            offline=True,
        )

    def _plan(self, selections):
        ModelSelections(selections=selections)
        try:
            plan = self._setup().plan(selections)
        except ValueError:
            raise TychoraError(
                "INVALID_REQUEST", "Unknown or duplicate model selection."
            ) from None
        if len(plan.assets) > 64:
            raise TychoraError("INVALID_REQUEST", "Selection exceeds 64 assets.")
        return plan

    async def setup_plan(
        self, selections: tuple[str, ...], scope: RequestScope, ctx: ExecutionContext
    ) -> ModelAssetPlan:
        def read():
            authorize_workspace(self.owner, scope)
            return self._plan(selections)

        return await self.owner.run_write(read, ctx)

    async def catalog(self, scope: RequestScope, ctx: ExecutionContext) -> ModelCatalog:
        def read():
            grant = authorize_workspace(self.owner, scope)
            from . import setup

            manifest = json.loads(setup.MANIFEST.read_bytes())
            selections = tuple(sorted({a["selection"] for a in manifest["assets"]}))
            plan = self._setup().plan(selections)
            policies = [
                (cid, parse_collection_policy(raw))
                for cid, raw in self.owner.db.execute(
                    "SELECT collection_id,policy_json FROM collections WHERE workspace_id=?",
                    (self.owner.workspace_id,),
                )
            ]
            providers = []
            for slot in ("text", "image", "rerank", "vision"):
                configured = getattr(self.owner.config.values.providers, slot)
                qualified = QUALIFIED_MODELS[slot]
                qualification = (
                    "disabled"
                    if configured.provider == "disabled"
                    else "explicit_identifier"
                    if slot == "vision"
                    else "qualified"
                    if configured.model in qualified
                    else "unsupported"
                )
                providers.append(
                    ProviderInventory(
                        slot=slot,
                        configured_provider=configured.provider,
                        configured_model=configured.model,
                        qualified_models=qualified,
                        model_qualification=qualification,
                        package_available=find_spec(
                            "anthropic" if slot == "vision" else "tokenizers"
                        )
                        is not None,
                        upload_allowed_collection_ids=tuple(
                            cid
                            for cid, policy in policies
                            if configured.provider in grant.upload_providers
                            and configured.provider in policy.upload_providers
                            and configured.provider in policy.owner_upload_providers
                        ),
                    )
                )
            capabilities = []
            for selection in selections:
                modules = {
                    "documents": ("docling",),
                    "ocr": ("rapidocr",),
                    "output-tokenizer": ("tiktoken",),
                }.get(selection, ("tokenizers", "httpx"))
                capabilities.append(
                    ModelSelectionCapability(
                        selection=selection,
                        required_modules=modules,
                        packages_available=all(
                            find_spec(module) is not None for module in modules
                        ),
                    )
                )
            return ModelCatalog(
                selection_capabilities=tuple(capabilities),
                providers=tuple(providers),
                selections=selections,
                manifest_sha256=plan.manifest_sha256,
                offline=self.owner.config.values.offline,
            )

        return await self.owner.run_write(read, ctx)

    def _read_receipt(self, ref, deadline, remaining):
        """Hash the bounded receipt bytes once; do not use an unbounded CAS reader."""
        if ref.storage_key != ref.sha256 or ref.size_bytes > remaining:
            return None, 0
        root_fd = fd = None
        payload = bytearray()
        try:
            root_fd = _open_directory(self.owner.artifacts.root, create=False)
            fd = os.open(
                ref.storage_key,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
            initial = os.fstat(fd)
            if not stat.S_ISREG(initial.st_mode) or initial.st_size != ref.size_bytes:
                return None, 0
            while len(payload) < ref.size_bytes:
                if time.monotonic() >= deadline:
                    return None, len(payload)
                block = os.read(fd, min(65536, ref.size_bytes - len(payload)))
                if not block:
                    return None, len(payload)
                payload.extend(block)
            final = os.fstat(fd)
            if (
                time.monotonic() >= deadline
                or (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                != (final.st_size, final.st_mtime_ns, final.st_ctime_ns)
                or sha256(payload).hexdigest() != ref.sha256
            ):
                return None, len(payload)
            return bytes(payload), len(payload)
        except OSError:
            return None, len(payload)
        finally:
            if fd is not None:
                os.close(fd)
            if root_fd is not None:
                os.close(root_fd)

    def _receipts(self, plan, deadline, budget):
        rows = self.owner.db.execute(
            "SELECT job_id FROM jobs WHERE operation='setup' AND state='complete' ORDER BY updated_at DESC LIMIT 33"
        ).fetchall()
        receipts = []
        complete = len(rows) <= 32
        remaining = min(1048576, budget)
        measured = 0
        wanted = {a.id: (a.sha256, a.size_bytes, a.path) for a in plan.assets}
        for (jid,) in rows[:32]:
            if time.monotonic() >= deadline:
                complete = False
                break
            status = self.repository.get(jid)
            ref = status.result
            if ref is None:
                continue
            if ref.size_bytes > remaining:
                complete = False
                continue
            payload, checked = self._read_receipt(ref, deadline, remaining)
            remaining -= checked
            measured += checked
            if payload is None:
                complete = False
                continue
            try:
                envelope = json.loads(payload)
                if envelope.get("type") != "setup_result":
                    complete = False
                    continue
                data = envelope.get("data", {})
                if data.get("manifest_sha256") != plan.manifest_sha256 or not data.get(
                    "verified_at"
                ):
                    continue
                matches = tuple(
                    a["id"]
                    for a in data.get("assets", [])
                    if a.get("id") in wanted
                    and (a.get("sha256"), a.get("size_bytes"), a.get("path"))
                    == wanted[a["id"]]
                )
                if matches:
                    receipts.append(
                        VerificationReceipt(
                            job_id=jid,
                            manifest_sha256=plan.manifest_sha256,
                            verified_at=data["verified_at"],
                            asset_ids=matches,
                        )
                    )
            except (TychoraError, OSError, ValueError, TypeError):
                complete = False
        return tuple(receipts), complete, measured

    async def readiness(
        self, selections: tuple[str, ...], scope: RequestScope, ctx: ExecutionContext
    ) -> ModelReadiness:
        def read():
            authorize_workspace(self.owner, scope)
            plan = self._plan(selections)
            deadline = min(time.monotonic() + 5, ctx.deadline or float("inf"))
            bounded = ExecutionContext(deadline=deadline, cancelled=ctx.cancelled)
            result = self._setup().verify_bounded(plan, bounded)
            receipts, complete, measured = self._receipts(
                plan, deadline, 256 * 1024 * 1024 - result.checked_bytes
            )
            return result.model_copy(
                update={
                    "receipts": receipts,
                    "receipt_search_complete": complete,
                    "receipt_checked_bytes": measured,
                }
            )

        return await self.owner.run_write(read, ctx)
