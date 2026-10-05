"""Immutable per-revision binding for explicitly global metadata facets."""

from __future__ import annotations

from collections.abc import Mapping

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes

from .registry import Registry

ROLE = "global_facet_binding"
SOURCE_REF = "global-facets@1"
MAX_GLOBAL_FACETS = 64
_SEMANTICS = (
    "key",
    "value_type",
    "source_binding",
    "filter_scope",
    "missing",
    "allow_global",
    "operators",
)


def _declared(metadata: Mapping[str, object]) -> tuple[str, ...]:
    scope = metadata.get("scope", {})
    if not isinstance(scope, Mapping):
        raise TychoraError("INVALID_REQUEST", "Metadata scope is invalid")
    raw = scope.get("global_facets", ())
    if not isinstance(raw, (tuple, list)) or len(raw) > MAX_GLOBAL_FACETS:
        raise TychoraError("INVALID_REQUEST", "Global facet declaration is too large")
    if any(not isinstance(item, str) or not item for item in raw):
        raise TychoraError("INVALID_REQUEST", "Global facet names must be nonempty")
    if len(set(raw)) != len(raw):
        raise TychoraError("INVALID_REQUEST", "Duplicate global facet declaration")
    return tuple(raw)


def normalized_metadata(metadata: FrozenJsonMap, registry: Registry) -> FrozenJsonMap:
    """Resolve caller aliases before immutable metadata and revision identity."""
    declared = _declared(metadata)
    if not declared:
        return metadata
    canonical: list[str] = []
    for name in declared:
        facet = registry.facet(name)
        if facet.get("allow_global") is not True:
            raise TychoraError("INVALID_REQUEST", "Facet does not permit global use")
        key = facet["key"]
        assert isinstance(key, str)
        canonical.append(key)
    if len(set(canonical)) != len(canonical):
        raise TychoraError("INVALID_REQUEST", "Global facet aliases overlap")
    scope = metadata["scope"]
    assert isinstance(scope, FrozenJsonMap)
    return FrozenJsonMap(
        {
            **dict(metadata),
            "scope": {**dict(scope), "global_facets": sorted(canonical)},
        }
    )


def binding_bytes(
    metadata: FrozenJsonMap,
    revision_id: str,
    metadata_sha: str,
    registry: Registry,
) -> tuple[frozenset[str], bytes | None]:
    """Require canonical keys; alias labels cannot reinterpret old revisions."""
    declared = _declared(metadata)
    if not declared:
        return frozenset(), None
    semantics = []
    for key in sorted(declared):
        if key not in registry.facets:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Historical global facet lacks a verified canonical binding; publish a new edition",
            )
        facet = registry.facet(key)
        if facet.get("allow_global") is not True:
            raise TychoraError("PROFILE_INCOMPATIBLE", "Global facet semantics changed")
        projection = {name: facet.get(name) for name in _SEMANTICS}
        operators = facet["operators"]
        if not isinstance(operators, tuple) or any(
            not isinstance(item, str) for item in operators
        ):
            raise TychoraError("PROFILE_INCOMPATIBLE", "Facet operators differ")
        projection["operators"] = tuple(
            sorted(item for item in operators if isinstance(item, str))
        )
        semantics.append(projection)
    return frozenset(declared), canonical_bytes(
        {
            "type": "global_facet_binding",
            "schema_version": "1",
            "data": {
                "revision_id": revision_id,
                "metadata_sha": metadata_sha,
                "facets": semantics,
            },
        }
    )
