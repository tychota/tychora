"""Quiescent, owner-authoritative workspace configuration activation."""

from __future__ import annotations

import json
import os
from uuid import uuid4

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import SettingsActivation
from tychora.ports.execution import ExecutionContext

from .services import build_services


class SettingsService:
    def __init__(self, owner, runtime) -> None:
        self.owner = owner
        self.runtime = runtime

    async def activate(self, request: SettingsActivation):
        """Prepare all ports before replacing the durable file and live binding."""
        owner = self.owner
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
            candidate = resolve_config(candidate_path, request.profile, {})
            registry = Registry.from_config(candidate)

            def prepare():
                retained = {}
                rows = owner.db.execute(
                    "SELECT DISTINCT k.kind_id,k.definition_json FROM kinds k "
                    "JOIN revisions r USING(kind_id) JOIN sources s USING(source_id) "
                    "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
                    (owner.workspace_id,),
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
                    owner.store.active_snapshot_id() is not None
                    and candidate.index_fingerprint != owner.config.index_fingerprint
                ):
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Existing index generations require their original configuration.",
                    )
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
                    prepared = build_services(
                        owner,
                        candidate,
                        previous_services.provider_bindings,
                        previous_services.output_counter,
                    )
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
            await previous_services.source_jobs.aclose()
            return candidate
        except BaseException:
            if (
                candidate_services is not None
                and self.runtime.services is not candidate_services
            ):
                await candidate_services.source_jobs.aclose()
            if replaced and self.runtime.services is not candidate_services:
                rollback = owner.path / (".workspace-rollback-" + str(uuid4()))
                rollback.write_bytes(original)
                os.replace(rollback, path)
            raise
        finally:
            candidate_path.unlink(missing_ok=True)
