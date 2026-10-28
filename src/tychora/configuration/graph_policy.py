"""Native graph traversal permissions bound from a profile and its registry."""

from typing import Literal, cast

from tychora.configuration.models import GraphSettings, ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphPredicatePolicy, GraphTraversalPolicy

_TABLE_CONTINUATION = "continues_table@1"
type _Searchable = tuple[Literal["recorded", "confirmed"], ...]


def _searchable_statuses(settings: GraphSettings) -> _Searchable:
    statuses: _Searchable = tuple(
        status for status in settings.statuses if status in ("recorded", "confirmed")
    )
    if len(statuses) != len(settings.statuses):
        raise TychoraError("INVALID_REQUEST", "Graph status is not searchable")
    return statuses


def _search_predicate(
    predicate: str, registry: Registry, settings: GraphSettings, continuation: str
) -> GraphPredicatePolicy | None:
    """The search permission of one configured predicate, or None when it never runs."""
    definition = registry.relations.get(predicate)
    if not isinstance(definition, FrozenJsonMap):
        raise TychoraError("INVALID_REQUEST", "Graph predicate is not registered")
    search_directions = cast(tuple[str, ...], definition.get("search_directions", ()))
    directions = tuple(settings.directions)
    if not directions or set(directions) - set(search_directions):
        raise TychoraError(
            "INVALID_REQUEST", "Graph predicate has no permitted search direction"
        )
    statuses = _searchable_statuses(settings)
    if not settings.semantic:
        return None
    if predicate == _TABLE_CONTINUATION:
        if continuation == "none":
            return None
        if continuation == "confirmed_only":
            statuses = tuple(status for status in statuses if status == "confirmed")
    if not statuses:
        return None
    return GraphPredicatePolicy(
        predicate=predicate, directions=directions, statuses=statuses
    )


def _require_code_structure(registry: Registry) -> None:
    for predicate in ("contains@1", "precedes@1"):
        definition = registry.relations.get(predicate)
        if not isinstance(definition, FrozenJsonMap) or set(
            cast(tuple[str, ...], definition.get("search_directions", ()))
        ) != {"outgoing", "incoming"}:
            raise TychoraError(
                "INVALID_REQUEST", "Code structural predicate is unavailable"
            )


def _inspection_predicates(
    registry: Registry, structural: str, continuation: str
) -> tuple[GraphPredicatePolicy, ...]:
    inspection = []
    for predicate, definition in sorted(registry.relations.items()):
        if not isinstance(definition, FrozenJsonMap):
            continue
        directions = cast(tuple[str, ...], definition.get("inspection_directions", ()))
        if (
            not directions
            or predicate == _TABLE_CONTINUATION
            and continuation == "none"
            or definition.get("structural_semantics")
            and structural == "none"
        ):
            continue
        statuses = ("recorded", "proposed", "confirmed", "rejected")
        if predicate == _TABLE_CONTINUATION and continuation == "confirmed_only":
            statuses = ("confirmed",)
        inspection.append(
            GraphPredicatePolicy(
                predicate=predicate,
                view="structural"
                if definition.get("structural_semantics")
                else "semantic",
                directions=cast(
                    tuple[Literal["outgoing", "incoming"], ...], directions
                ),
                statuses=statuses,
            )
        )
    return tuple(inspection)


def resolve_graph_traversal_policy(
    config: ResolvedConfig, registry: Registry
) -> GraphTraversalPolicy:
    """Bind a validated profile and registry to native traversal permissions."""
    if config.registry_digest != registry.registry_digest:
        raise TychoraError(
            "INVALID_REQUEST", "Graph registry differs from resolved profile"
        )
    settings = config.values.retrieval.graph
    continuation = config.values.retrieval.table_continuations
    predicates = tuple(
        policy
        for predicate in settings.predicates
        if (policy := _search_predicate(predicate, registry, settings, continuation))
        is not None
    )
    if settings.structural == "code_context@1":
        _require_code_structure(registry)
    return GraphTraversalPolicy(
        predicates=predicates,
        inspection_predicates=_inspection_predicates(
            registry, settings.structural, continuation
        ),
        structural=settings.structural,
        semantic=settings.semantic,
        table_continuations=continuation,
    )
