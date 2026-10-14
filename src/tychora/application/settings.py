"""Quiescent, owner-authoritative workspace configuration activation."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.configuration.additive_compatibility import (
    qualified_additive_registry_extension,
)
from tychora.configuration.indexed_representation import indexed_representation_identity
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config, selected_workspace_profile
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import SettingsActivation
from tychora.ports.execution import ExecutionContext

from .runtime import close_owned_runtimes
from .services import build_services


async def _complete_committed(coroutine) -> bool:
    """Settle post-commit cleanup despite caller cancellation or close failure."""
    task = asyncio.create_task(coroutine)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:  # noqa: BLE001 - inspect the settled result below
            break
    try:
        task.result()
    except BaseException:  # noqa: BLE001 - caller receives the committed result
        return False
    return True


class SettingsService:
    def __init__(self, owner, runtime) -> None:
        self.owner = owner
        self.runtime = runtime

    def preflight(self, candidate):
        """Validate retained identity without constructing runtimes or changing state."""
        registry = Registry.from_config(candidate)
        retained = {}
        rows = self.owner.db.execute(
            "SELECT DISTINCT k.kind_id,k.definition_json FROM kinds k "
            "JOIN revisions r USING(kind_id) JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchall()
        for kind, definition in rows:
            value = json.loads(definition)
            if kind in retained and retained[kind] != value:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained kind identity is ambiguous."
                )
            retained[kind] = value
        registry.validate_activation(frozenset(retained), retained)
        if (
            self.owner.store.active_snapshot_id() is not None
            and indexed_representation_identity(candidate)
            != indexed_representation_identity(self.owner.config)
            and not qualified_additive_registry_extension(self.owner.config, candidate)
        ):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Existing index generations require their original configuration.",
            )
        return registry

    async def activate(self, request: SettingsActivation, scope=None):
        """Direct callers share the same exclusive admission as HTTP activation."""
        self.runtime.admit(activation=True)
        try:
            return await self._activate_admitted(request, scope or self.owner.scope)
        finally:
            self.runtime.release(activation=True)

    async def _activate_admitted(self, request: SettingsActivation, scope):
        """Prepare all ports before replacing the durable file and live binding."""
        owner = self.owner
        from .workspace_authority import authorize_workspace

        await owner.run_blocking(
            lambda: authorize_workspace(owner, scope), ExecutionContext()
        )
        await self.runtime.aclose_retired()
        if self.runtime.retired_count >= 8:
            raise TychoraError(
                "WORKSPACE_BUSY",
                "Previous source schedulers require cleanup before activation.",
                retryable=True,
            )
        previous_services = self.runtime.services
        if not await previous_services.source_jobs.is_idle_async():
            raise TychoraError(
                "WORKSPACE_BUSY",
                "Accepted jobs must finish before settings activation.",
                retryable=True,
            )
        path = owner.path / "workspace.toml"
        try:
            original = path.read_bytes()
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Workspace settings are unavailable."
            ) from None
        if len(original) > 1048576:
            raise TychoraError(
                "INTEGRITY_ERROR", "Workspace settings exceed the local limit."
            )
        candidate_path = owner.path / (".workspace-next-" + str(uuid4()))
        fd = os.open(
            candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        candidate_services = None
        replaced = False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(request.workspace_toml.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if selected_workspace_profile(candidate_path) != request.profile:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Selected profile must match workspace defaults.profile.",
                )
            candidate = resolve_config(candidate_path, request.profile, {})
            registry = Registry.from_config(candidate)

            def prepare():
                self.preflight(candidate)
                slots = {"lexical"}
                if candidate.values.index.text.backend != "disabled":
                    slots.add("text")
                if candidate.values.index.image.backend != "disabled":
                    slots.add("image")
                if candidate.values.index.graph != "disabled":
                    slots.add("graph")
                store = CorpusStore(
                    owner.db,
                    registry=registry,
                    artifacts=owner.artifacts,
                    required_slots=frozenset(slots),
                    vector_indexes=owner.store.vector_indexes,
                    graph_indexes=owner.store.graph_indexes,
                    leases=owner.leases,
                )
                previous_config, previous_store = owner.config, owner.store
                try:
                    # HTTP admission is exclusive here. Constructors bind the
                    # owner's canonical ports; restore the live owner before
                    # returning from this worker even when preparation fails.
                    owner.config, owner.store = candidate, store
                    workspace = self.runtime._workspace
                    if workspace is not None and workspace.injected_providers is None:
                        from tychora.application.default_bindings import (
                            compose_default_bindings,
                        )

                        bindings, runtimes = compose_default_bindings(
                            owner,
                            candidate,
                            credential_resolver=workspace.credential_resolver,
                        )
                    else:
                        bindings, runtimes = previous_services.provider_bindings, ()
                    try:
                        prepared = build_services(
                            owner,
                            candidate,
                            bindings,
                            previous_services.output_counter,
                            owned_runtimes=runtimes,
                        )
                    except BaseException:
                        close_owned_runtimes(runtimes)
                        raise
                finally:
                    owner.config, owner.store = previous_config, previous_store
                return store, prepared

            store, candidate_services = await owner.run_blocking(
                prepare, ExecutionContext()
            )
            if not await previous_services.source_jobs.is_idle_async():
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Accepted jobs started during settings preparation.",
                    retryable=True,
                )

            if candidate.config_sha != owner.config.config_sha:

                def bump():
                    if owner.db.in_transaction:
                        raise TychoraError(
                            "WORKSPACE_BUSY", "Policy update is busy.", retryable=True
                        )
                    owner.db.execute(
                        "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id=?",
                        (owner.workspace_id,),
                    )
                    owner.db.commit()

                await owner.run_blocking(bump, ExecutionContext())
            os.replace(candidate_path, path)
            replaced = True
            directory_fd = os.open(owner.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            owner.config, owner.store = candidate, store
            self.runtime.swap(candidate_services)
            from .workspace_authority import refresh_managed_owner

            if not await _complete_committed(
                owner.run_blocking(
                    lambda: refresh_managed_owner(owner), ExecutionContext()
                )
            ):
                self.runtime.record_cleanup_failure()
            # The durable file and live binding have committed. Cleanup must not
            # turn this successful activation into an apparent failed request.
            if not await _complete_committed(previous_services.source_jobs.aclose()):
                if not await _complete_committed(
                    asyncio.to_thread(previous_services.source_jobs.close)
                ):
                    self.runtime.record_cleanup_failure(previous_services.source_jobs)
                else:
                    self.runtime.record_cleanup_failure()
            if not close_owned_runtimes(previous_services.owned_runtimes):
                self.runtime.record_provider_cleanup_failure()
            return candidate
        except BaseException:
            if (
                candidate_services is not None
                and self.runtime.services is not candidate_services
            ):
                await candidate_services.source_jobs.aclose()
                close_owned_runtimes(candidate_services.owned_runtimes)
            if self.runtime.services is not candidate_services:
                graph = getattr(owner, "_configured_graph_store", None)
                if graph is not None and owner.config.values.index.graph == "ladybug":
                    from tychora.adapters.ladybug.traversal import (
                        resolve_graph_traversal_policy,
                    )

                    policy = resolve_graph_traversal_policy(
                        owner.config, Registry.from_config(owner.config)
                    )
                    await owner.run_blocking(
                        lambda: graph.set_traversal_policy(policy), ExecutionContext()
                    )
            if replaced and self.runtime.services is not candidate_services:
                rollback = owner.path / (".workspace-rollback-" + str(uuid4()))
                rollback.write_bytes(original)
                os.replace(rollback, path)
            raise
        finally:
            candidate_path.unlink(missing_ok=True)
