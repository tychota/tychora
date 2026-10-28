"""One pinned query attempt: its snapshot, its view and the epoch checks around it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.publication import SnapshotView
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.retrieval.cache import EligibilityCache
from tychora.retrieval.catalog import SnapshotCatalog
from tychora.retrieval.compiler import (
    CompiledEligibility,
    EligibilityCompiler,
    EligibilityInputs,
)
from tychora.retrieval.selection import EditionDecision


class SnapshotLeases(Protocol):
    """Leases that keep pinned snapshots from collection."""

    def pin(self, keys: frozenset[str]) -> AbstractContextManager[None]:
        """Hold the listed snapshots until the context exits."""
        ...


class AttemptStore(Protocol):
    """The store operations a pinned attempt needs."""

    @property
    def leases(self) -> SnapshotLeases:
        """The store's snapshot leases."""
        ...

    def retained_config(self, snapshot: SnapshotView) -> ResolvedConfig:
        """The configuration the snapshot was published under."""
        ...


class AttemptOwner(Protocol):
    """The owner operations a pinned attempt runs on."""

    scope: RequestScope
    catalog: SnapshotCatalog
    eligibility_cache: EligibilityCache

    @property
    def store(self) -> AttemptStore:
        """The canonical store the attempt pins and reads retained configs from."""
        ...

    async def run_read(
        self, call: Callable[[ReadTransaction], Any], ctx: ExecutionContext
    ) -> Any:
        """Run ``call`` in a pooled read-only transaction."""
        ...

    async def run_write(
        self,
        call: Callable[[], Any],
        ctx: ExecutionContext,
        priority: Literal["interactive", "background"] = "background",
    ) -> Any:
        """Run ``call`` on the writer thread."""
        ...


def _changed() -> TychoraError:
    return TychoraError(
        "SCOPE_CHANGED",
        "Workspace policy changed during retrieval",
        reason="policy.epoch_changed",
    )


def resolve_snapshot(
    catalog: SnapshotCatalog, tx: ReadTransaction, selector: str | None
) -> SnapshotView:
    """The selected or active snapshot's view, through the catalog."""
    snapshot_id = selector or tx.snapshots.active_id()
    if snapshot_id is None:
        raise TychoraError(
            "NOT_FOUND", "No published snapshot is available", reason="snapshot.absent"
        )
    return catalog.get(tx, snapshot_id)


@dataclass(frozen=True)
class EligibilityAttempt:
    """One compiled attempt; provider calls and delivery recheck its policy epoch."""

    compiled: CompiledEligibility
    snapshot: SnapshotView
    owner: AttemptOwner | None = None
    ctx: ExecutionContext | None = None

    @property
    def view(self) -> EligibilityView:
        """The revision-level view this attempt reads."""
        return self.compiled.view

    @property
    def warnings(self) -> tuple[str, ...]:
        """Edition warnings of the compiled view."""
        return self.compiled.warnings

    @property
    def decisions(self) -> tuple[EditionDecision, ...]:
        """Edition decisions the compiled view leaves to the caller."""
        return self.compiled.decisions

    @property
    def historical_kind_roles(self) -> Mapping[str, str]:
        """Roles of selected kinds the current registry lacks."""
        return self.compiled.historical_kind_roles

    @property
    def historical_kind_envelopes(self) -> Mapping[str, FrozenJsonMap]:
        """The verified envelope of every selected kind."""
        return self.compiled.historical_kind_envelopes

    async def revalidate(self, stage: Literal["provider", "delivery"]) -> None:
        """Refuse when the policy epoch moved since the view was compiled."""
        if self.owner is None or self.ctx is None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                f"An unowned attempt cannot be revalidated at {stage}",
                reason="invariant.unexpected",
            )
        epoch = await self.owner.run_read(
            lambda tx: tx.workspace.policy_epoch(), self.ctx
        )
        if epoch != self.view.policy_epoch:
            raise _changed()

    async def owner_provider[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Recheck the epoch before an owner-authorized provider operation."""
        await self.revalidate("provider")
        return await call()


@dataclass(frozen=True)
class _Delivered[T]:
    value: T


@dataclass(frozen=True)
class AttemptHost:
    """The compiler and owner one request's attempts run on, under its context."""

    compiler: EligibilityCompiler
    owner: AttemptOwner
    ctx: ExecutionContext


async def run_pinned_eligibility[T](
    host: AttemptHost,
    inputs: EligibilityInputs[str | None],
    operation: Callable[[EligibilityAttempt], Awaitable[T]],
    *,
    on_invalidated: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Hold one snapshot lease through both attempts, provider calls and delivery."""
    compiler, owner, ctx, config = host.compiler, host.owner, host.ctx, inputs.config
    scope = owner.scope.narrow(inputs.scope)
    snapshot = await owner.run_read(
        lambda tx: resolve_snapshot(owner.catalog, tx, inputs.snapshot), ctx
    )
    with owner.store.leases.pin(frozenset({snapshot.snapshot_id})):
        if not compiler.knows_compatible(snapshot, config):
            retained = await owner.run_write(
                lambda: owner.store.retained_config(snapshot), ctx, "interactive"
            )
            compiler.mark_compatible(snapshot, config, retained)
        pinned = EligibilityInputs(snapshot, scope, inputs.request, config)
        for attempt_number in range(2):
            attempt = await _attempt(host, pinned)
            delivered = await _deliver_once(
                attempt, operation, on_invalidated, final=attempt_number == 1
            )
            if delivered is not None:
                return delivered.value
        raise _changed()


async def _deliver_once[T](
    attempt: EligibilityAttempt,
    operation: Callable[[EligibilityAttempt], Awaitable[T]],
    on_invalidated: Callable[[T], Awaitable[None]] | None,
    *,
    final: bool,
) -> _Delivered[T] | None:
    """Run the operation and recheck at delivery; ``None`` asks for a retry."""
    try:
        result = await operation(attempt)
    except TychoraError as error:
        if error.code != "SCOPE_CHANGED" or final:
            raise
        return None
    try:
        await attempt.revalidate("delivery")
    except TychoraError as error:
        if error.code == "SCOPE_CHANGED" and on_invalidated is not None:
            await on_invalidated(result)
        if error.code != "SCOPE_CHANGED" or final:
            raise
        return None
    return _Delivered(result)


async def _attempt(
    host: AttemptHost, inputs: EligibilityInputs[SnapshotView]
) -> EligibilityAttempt:
    """Compile the view, cached, on the read pool."""
    owner = host.owner
    compiled = await owner.run_read(
        lambda tx: host.compiler.compile_cached(tx, owner.eligibility_cache, inputs),
        host.ctx,
    )
    return EligibilityAttempt(
        compiled=compiled,
        snapshot=inputs.snapshot,
        owner=owner,
        ctx=host.ctx,
    )


def readable_units(
    tx: ReadTransaction, view: EligibilityView, unit_ids: Iterable[str]
) -> frozenset[str]:
    """The listed units whose persisted sets the view allows."""
    wanted = tuple(sorted(set(unit_ids)))
    if not wanted:
        return frozenset()
    return frozenset(
        fact.owner_id
        for fact in tx.eligibility.owner_facts(wanted)
        if is_eligible_unit(view, fact)
    )


def require_epoch(tx: ReadTransaction, view: EligibilityView) -> None:
    """Refuse delivery when the policy epoch moved since the view was compiled."""
    if tx.workspace.policy_epoch() != view.policy_epoch:
        raise _changed()


def _candidate_filter(view: EligibilityView, unit_id: str) -> bool:
    return view.unit_filter is None or unit_id in view.unit_filter


def is_eligible_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """A unit whose dependency set is readable: context or structural."""
    return fact.kind == "unit" and view.allows(fact.dependency_ids)


def is_source_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible citable original that passes the candidate filter."""
    return (
        is_eligible_unit(view, fact)
        and not fact.proxy
        and fact.citable
        and _candidate_filter(view, fact.owner_id)
    )


def is_candidate_proxy(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible proxy unit that passes the candidate filter."""
    return (
        is_eligible_unit(view, fact)
        and fact.proxy
        and _candidate_filter(view, fact.owner_id)
    )


def is_eligible_asset(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An asset whose dependency set is readable."""
    return fact.kind == "asset" and view.allows(fact.dependency_ids)


def is_candidate_asset(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """An eligible asset; with a candidate filter, one of its source units passes it."""
    if not is_eligible_asset(view, fact):
        return False
    if view.unit_filter is None:
        return True
    return any(
        owner in fact.dependency_ids
        and (unit := facts.get(owner)) is not None
        and is_eligible_unit(view, unit)
        and not unit.proxy
        and _candidate_filter(view, owner)
        for owner in fact.source_owner_ids
    )


def is_admitted_chunk(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """A window of a source unit or candidate proxy whose own set is readable."""
    unit = facts.get(fact.unit_id or "")
    return (
        fact.kind == "chunk"
        and unit is not None
        and (is_source_unit(view, unit) or is_candidate_proxy(view, unit))
        and view.allows(fact.dependency_ids)
    )


def is_admitted_record(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """A selected vector record over an admitted window or a candidate asset."""
    if (
        fact.kind != "record"
        or fact.batch_id not in view.batch_ids
        or not view.allows(fact.dependency_ids)
    ):
        return False
    if fact.chunk_id is not None:
        chunk = facts.get(fact.chunk_id)
        return chunk is not None and is_admitted_chunk(view, chunk, facts)
    asset = facts.get(fact.asset_id or "")
    return asset is not None and is_candidate_asset(view, asset, facts)
