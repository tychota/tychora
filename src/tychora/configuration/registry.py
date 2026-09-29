"""Validated evidence vocabulary and scoped, declarative filter compilation."""

import math
import re
from collections.abc import Mapping
from typing import Any, Self, cast

from tychora.contracts.base import Contract, FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, digest
from tychora.contracts.requests import (
    AndFilter,
    EqFilter,
    FilterExpr,
    InFilter,
    OrFilter,
)
from tychora.contracts.requirements import ResponseRequirement

from .models import ResolvedConfig
from .schemas import LocalSchemas

NORMALIZERS = FrozenJsonMap(
    {
        "structured_sections@1": {
            "description": "Original headings, paragraphs, steps, tables and figures.",
            "unit_types": ["section", "paragraph", "step", "table", "figure"],
        },
        "numbered_provisions@1": {
            "description": "Original articles, numbered paragraphs and annex rows.",
            "unit_types": ["section", "paragraph", "table_row"],
        },
        "rule_records@1": {
            "description": "Literal requirements with targets, provenance, status and foundations.",
            "unit_types": ["rule"],
        },
        "feedback_records@1": {
            "description": "Attributed comments, targets and recorded decisions.",
            "unit_types": ["review"],
        },
        "template_slots@1": {
            "description": "Literal fields, slots and completion instructions.",
            "unit_types": ["slot"],
        },
        "code_symbols@1": {
            "description": "Original definitions with exact source and context ranges.",
            "unit_types": ["symbol", "code_file"],
        },
    }
)


def _invalid(message: str) -> None:
    raise TychoraError("INVALID_REQUEST", message)


class Registry(Contract):
    """Detached immutable definitions; no clients, filesystem or mutable validators."""

    artifact: FrozenJsonMap
    registry_digest: str
    kinds: FrozenJsonMap
    roles: FrozenJsonMap
    lanes: FrozenJsonMap
    modes: FrozenJsonMap
    facets: FrozenJsonMap
    relations: FrozenJsonMap
    schemas: FrozenJsonMap
    schema_digests: FrozenJsonMap
    lane_bindings: FrozenJsonMap
    normalizers: FrozenJsonMap = NORMALIZERS

    @classmethod
    def from_config(cls, config: ResolvedConfig) -> Self:
        try:
            return cls._from_config(config)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise TychoraError(
                "INVALID_REQUEST", "Malformed registry definition"
            ) from None

    @classmethod
    def _from_config(cls, config: ResolvedConfig) -> Self:
        if digest("registry-v1", config.registry) != config.registry_digest:
            _invalid("Registry artifact digest mismatch")
        data = config.model_dump(mode="json")["registry"]
        _validate_definitions(data)
        LocalSchemas(data["schemas"], data["schema_reference_bindings"])
        bindings = {}
        for lane, definition in data["lanes"].items():
            for kind in definition["kinds"]:
                if kind in bindings and bindings[kind] != lane:
                    _invalid("Kind has conflicting lane bindings")
                bindings[kind] = lane
        for kind, lane in data["compatibility"].get("kind_lanes", {}).items():
            if not _versioned(kind) or lane not in data["lanes"]:
                _invalid("Unknown historical lane binding")
            if kind in bindings and bindings[kind] != lane:
                _invalid("Conflicting direct and historical lane bindings")
            bindings[kind] = lane
        if set(data["kinds"]) - bindings.keys():
            _invalid("Each current kind requires exactly one lane")
        if set(config.values.retrieval.graph.predicates) - data["relations"].keys():
            _invalid("Unknown configured graph predicate")
        return cls(
            artifact=config.registry,
            registry_digest=config.registry_digest,
            **{
                key: FrozenJsonMap(data[key])
                for key in (
                    "kinds",
                    "roles",
                    "lanes",
                    "modes",
                    "facets",
                    "relations",
                    "schemas",
                )
            },
            schema_digests=FrozenJsonMap(
                {
                    key: digest("schema-v1", value)
                    for key, value in data["schemas"].items()
                }
            ),
            lane_bindings=FrozenJsonMap(bindings),
        )

    def _validate(self, kind_id: str, field: str, value: JsonValue) -> JsonValue:
        bindings = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]],
            self.artifact["definition_schema_bindings"],
        )
        if kind_id not in self.kinds or field not in bindings["kinds"].get(kind_id, {}):
            _invalid("Kind or requested schema is unavailable")
        return LocalSchemas(
            self.schemas,
            cast(Mapping[str, JsonValue], self.artifact["schema_reference_bindings"]),
        ).validate(bindings["kinds"][kind_id][field], value)

    def validate_metadata(self, kind_id: str, value: JsonValue) -> JsonValue:
        return self._validate(kind_id, "metadata_schema", value)

    def validate_record(self, kind_id: str, value: JsonValue) -> JsonValue:
        return self._validate(kind_id, "record_schema", value)

    def discover_kinds(self, counts: Mapping[str, int]) -> tuple[FrozenJsonMap, ...]:
        """Counts are supplied by the separately scoped corpus discovery service."""
        bindings = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]],
            self.artifact["definition_schema_bindings"],
        )["kinds"]
        return tuple(
            FrozenJsonMap(
                {
                    **cast(Mapping[str, JsonValue], definition),
                    "count": counts.get(key, 0),
                    "schema_ids": bindings[key],
                    **{
                        field + "_definition": self.schemas[schema_id]
                        for field, schema_id in bindings[key].items()
                    },
                    "normalizer_capabilities": self.normalizers[
                        cast(Mapping[str, str], definition)["normalizer"]
                    ],
                }
            )
            for key, definition in self.kinds.items()
        )

    def historical_definitions(self) -> FrozenJsonMap:
        """Export complete bounded old-kind validation artifacts for retained storage."""
        definitions = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]],
            self.artifact["definition_schema_bindings"],
        )["kinds"]
        references = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]],
            self.artifact["schema_reference_bindings"],
        )
        envelopes = {}
        for key, kind in self.kinds.items():
            needed = set(definitions[key].values())
            pending = list(needed)
            while pending:
                for reference in references[pending.pop()].values():
                    target = reference["schema_id"]
                    if target not in needed:
                        needed.add(target)
                        pending.append(target)
            envelope = {
                "kind": kind,
                "kind_digest": digest("kind-v1", kind),
                "schemas": {schema: self.schemas[schema] for schema in sorted(needed)},
                "schema_digests": {
                    schema: self.schema_digests[schema] for schema in sorted(needed)
                },
                "schema_bindings": definitions[key],
                "schema_reference_bindings": {
                    schema: references[schema] for schema in sorted(needed)
                },
            }
            envelopes[key] = {
                **envelope,
                "envelope_digest": digest("historical-kind-v1", envelope),
            }
        return FrozenJsonMap(envelopes)

    def validate_activation(
        self,
        retained_kinds: frozenset[str],
        historical_definitions: Mapping[str, JsonValue],
    ) -> None:
        """Owner supplies all retained collection kinds; publication belongs to storage."""
        from .compatibility import require_kind_coverage

        unknown = set(self.lane_bindings) - (set(self.kinds) | set(retained_kinds))
        if unknown:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Unknown explicit kind bindings: " + ", ".join(sorted(unknown)),
            )
        require_kind_coverage(
            retained_kinds,
            cast(Mapping[str, str], self.lane_bindings),
            historical_definitions,
        )
        current = self.historical_definitions()
        for key in sorted(set(retained_kinds) & set(self.kinds)):
            if digest("historical-envelope-v1", current[key]) != digest(
                "historical-envelope-v1", historical_definitions[key]
            ):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Conflicting original kind/schema version: " + key,
                )

    def facet(self, field: str) -> FrozenJsonMap:
        if field in self.facets:
            return cast(FrozenJsonMap, self.facets[field])
        for definition in self.facets.values():
            facet = cast(FrozenJsonMap, definition)
            if facet.get("alias") == field:
                return facet
        _invalid("Unknown registered facet")
        raise AssertionError("unreachable")

    def facet_matches(
        self,
        field: str,
        expected: JsonValue,
        source_value: JsonValue,
        *,
        explicit_global: bool = False,
        include_global: bool = False,
        child_value: JsonValue = None,
    ) -> bool:
        """Evaluate one exact applicability facet; this never grants policy access."""
        facet = self.facet(field)
        _facet_value(facet, expected)
        source_matches = (
            source_value is not None
            and source_value == expected
            and type(source_value) is type(expected)
        )
        source_matches |= (
            source_value is None
            and explicit_global
            and include_global
            and facet.get("allow_global") is True
        )
        return bool(
            source_matches
            and (
                child_value is None
                or child_value == expected
                and type(child_value) is type(expected)
            )
        )

    def hint_contributions(
        self,
        hints: Mapping[str, JsonValue],
        values: Mapping[str, JsonValue],
        *,
        bonus: float = 0.0,
    ) -> FrozenJsonMap:
        if (
            type(bonus) not in (int, float)
            or not math.isfinite(bonus)
            or not 0 <= bonus <= 0.01
        ):
            _invalid("Hint bonus must be finite and between zero and 0.01")
        registered = {}
        for field, value in hints.items():
            facet = self.facet(field)
            if facet.get("hint") is not True:
                _invalid("Field is not registered for relevance hints")
            _facet_value(facet, value)
            key = cast(str, facet["key"])
            if key in registered:
                _invalid("Duplicate hint aliases")
            registered[key] = value
        contributions = {}
        remaining = 0.01
        for key, value in sorted(registered.items()):
            if (
                bonus
                and remaining > 0
                and values.get(key) == value
                and type(values.get(key)) is type(value)
            ):
                contribution = min(bonus, remaining)
                contributions[key] = contribution
                remaining = max(0.0, remaining - contribution)
        return FrozenJsonMap(contributions)


def _versioned(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", value)
        is not None
    )


def _strings(value: Any, *, nonempty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not nonempty)
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def _require(definition: Any, fields: tuple[str, ...]) -> None:
    if not isinstance(definition, dict) or any(
        not isinstance(definition.get(field), str) or not definition[field].strip()
        for field in fields
    ):
        _invalid("Missing or invalid semantic definition requirement")


_POLICY_PARTS = frozenset(
    {
        "policy",
        "access",
        "access_level",
        "access_floor",
        "allowed_uses",
        "uses",
        "review",
        "review_state",
        "required_review",
        "publication_date",
        "effective_start",
        "effective_end",
        "dates",
        "upload",
        "upload_providers",
        "capabilities",
        "withdrawn",
    }
)
_RESERVED = {
    "contains@1": {
        "structural_semantics": "immediate_lexical_parent",
        "subject_unit_types": ["symbol", "code_file"],
        "object_unit_types": ["symbol"],
    },
    "precedes@1": {
        "structural_semantics": "adjacent_siblings_source_order",
        "subject_unit_types": ["symbol"],
        "object_unit_types": ["symbol"],
    },
}
_CODE_FACETS = {
    "code.language": ("file.language", "all_units"),
    "code.path": ("file.relative_path", "all_units"),
    "code.symbol_kind": ("record.symbol_kind", "matched_candidates"),
    "code.qualified_name": ("record.qualified_name", "matched_candidates"),
}


def _validate_definitions(data: dict[str, Any]) -> None:
    for key, role in data["roles"].items():
        _require(role, ("id", "key", "label", "description", "intended_use"))
        if (
            role["key"] != key
            or not _versioned(role["id"])
            or role["id"].split("@")[0] != key
        ):
            _invalid("Invalid role identity")
    for key, kind in data["kinds"].items():
        _require(
            kind,
            (
                "id",
                "key",
                "label",
                "description",
                "role",
                "metadata_schema",
                "normalizer",
            ),
        )
        if kind["id"] != key or not _versioned(key) or kind["key"] != key.split("@")[0]:
            _invalid("Invalid kind identity")
        if (
            kind["role"] not in data["roles"]
            or kind["normalizer"] not in NORMALIZERS
            or not _strings(kind.get("examples"))
        ):
            _invalid("Unresolved role, normalizer or examples")
        if "allowed_uses" in kind and not _strings(
            kind["allowed_uses"], nonempty=False
        ):
            _invalid("Invalid kind usage restrictions")
        options = kind.get("normalizer_options", {})
        common = {"target_labels", "status_labels", "foundation_labels"}
        allowed = {
            "rule_records@1": common
            | {
                "identifier_prefixes",
                "requirement_labels",
                "proof_labels",
                "control_labels",
                "severity_labels",
            },
            "feedback_records@1": common
            | {
                "comment_labels",
                "author_labels",
                "decision_labels",
                "observation_labels",
            },
            "template_slots@1": common | {"slot_labels", "instruction_labels"},
        }.get(kind["normalizer"], set())
        if (
            not isinstance(options, dict)
            or set(options) - allowed
            or any(
                not _strings(value)
                or len(value) > 64
                or any(len(label) > 256 for label in value)
                for value in options.values()
            )
        ):
            _invalid("Unsupported normalizer options")
    for category in ("kinds", "relations"):
        bindings = data["definition_schema_bindings"].get(category, {})
        if set(bindings) != set(data[category]):
            _invalid("Incomplete immutable definition schema bindings")
        for key, definition in data[category].items():
            expected = {
                field
                for field in ("metadata_schema", "record_schema")
                if field in definition
            }
            if set(bindings[key]) != expected or any(
                value not in data["schemas"] for value in bindings[key].values()
            ):
                _invalid("Unresolved original definition schema")
    prefixes = set()
    for key, lane in data["lanes"].items():
        _require(lane, ("label", "description", "citation_prefix"))
        if (
            lane.get("key", key) != key
            or lane["citation_prefix"] in prefixes
            or not _strings(lane.get("kinds"), nonempty=False)
        ):
            _invalid("Invalid lane or duplicate citation prefix")
        prefixes.add(lane["citation_prefix"])
        if any(not _versioned(kind) for kind in lane["kinds"]):
            _invalid("Lanes require explicit versioned kinds")
    for mode in data["modes"].values():
        for field in ("allowed_lanes", "allowed_uses", "original_review_states"):
            if not _strings(mode.get(field), nonempty=False):
                _invalid("Missing or invalid mode rules")
        if "required_lanes" in mode and not _strings(
            mode["required_lanes"], nonempty=False
        ):
            _invalid("Mode required lanes must be a unique string list")
        requirements = mode.get("requirements", [])
        if not isinstance(requirements, list) or len(requirements) > 64:
            _invalid("Invalid mode requirements")
        checked_requirements = [
            ResponseRequirement.model_validate(item) for item in requirements
        ]
        if len({item.requirement_id for item in checked_requirements}) != len(
            checked_requirements
        ):
            _invalid("Duplicate mode requirement")
        for requirement in checked_requirements:
            if requirement.minimum > 100:
                _invalid("Mode requirement count exceeds response bound")
            if (
                requirement.target == "lane"
                and requirement.value not in mode["allowed_lanes"]
            ):
                _invalid("Unresolved required lane")
            if requirement.target == "kind" and requirement.value not in data[
                "kinds"
            ] | data["compatibility"].get("kind_lanes", {}):
                _invalid("Unresolved required kind")
            if (
                requirement.target == "record_schema"
                and requirement.value not in data["schemas"]
            ):
                _invalid("Unresolved required record schema")
        if set(mode["allowed_lanes"]) - data["lanes"].keys() or set(
            mode.get("required_lanes", [])
        ) - set(mode["allowed_lanes"]):
            _invalid("Unresolved mode lanes")
        if set(mode["original_review_states"]) - {
            "unreviewed",
            "needs_review",
            "accepted",
            "rejected",
        }:
            _invalid("Unknown original review state")
        if mode.get("derived_proxy_policy") not in (
            "disabled",
            "accepted_only",
            "permitted",
        ):
            _invalid("Missing or invalid proxy policy")
        if (
            "allow_derived_proxies" in mode
            and type(mode["allow_derived_proxies"]) is not bool
        ):
            _invalid("Invalid derived proxy switch")
        states = mode.get(
            "derived_proxy_review_states",
            ["accepted"] if mode["derived_proxy_policy"] == "accepted_only" else [],
        )
        if (
            not _strings(states, nonempty=False)
            or set(states) - {"unreviewed", "needs_review", "accepted"}
            or mode["derived_proxy_policy"] == "accepted_only"
            and set(states) - {"accepted"}
        ):
            _invalid("Invalid derived proxy review states")
    aliases = set(data["facets"])
    for key, facet in data["facets"].items():
        _require(
            facet, ("key", "value_type", "source_binding", "filter_scope", "missing")
        )
        if (
            facet["key"] != key
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", key)
            is None
        ):
            _invalid("Facets require immutable namespaced keys")
        if (
            facet["value_type"]
            not in ("string", "integer", "number", "boolean", "date", "date-time")
            or facet["filter_scope"] not in ("all_units", "matched_candidates")
            or facet["missing"] not in ("exclude", "diagnose")
        ):
            _invalid("Invalid facet type, scope or missing semantics")
        if not _strings(facet.get("operators")) or set(facet["operators"]) - {
            "eq",
            "in",
            "range",
        }:
            _invalid("Unsupported facet operators")
        if any(
            type(facet.get(field, False)) is not bool
            for field in ("allow_global", "hint")
        ):
            _invalid("Invalid facet global or hint flag")
        parts = set(re.split(r"[.\[/]", facet["source_binding"])) | set(key.split("."))
        if facet.get("hint") and parts & _POLICY_PARTS:
            _invalid("Policy fields cannot be relevance hints")
        if (
            key in _CODE_FACETS
            and (facet["source_binding"], facet["filter_scope"]) != _CODE_FACETS[key]
        ):
            _invalid("Reserved code facet semantics cannot change")
        alias = facet.get("alias")
        if alias is not None:
            if not isinstance(alias, str) or not alias or alias in aliases:
                _invalid("Conflicting facet alias")
            aliases.add(alias)
    for key, relation in data["relations"].items():
        _require(
            relation,
            ("id", "key", "label", "description", "default_status", "revision_binding"),
        )
        if (
            relation["id"] != key
            or not _versioned(key)
            or relation["key"] != key.split("@")[0]
        ):
            _invalid("Invalid relation identity")
        if (
            type(relation.get("directed")) is not bool
            or relation.get("require_origin") is not True
            or relation.get("require_supports") is not True
        ):
            _invalid("Relations require directedness, origin and complete supports")
        if "self_links" in relation and type(relation["self_links"]) is not bool:
            _invalid("Relation self-link policy must be boolean")
        if "cardinality" in relation and relation["cardinality"] not in (
            "one_to_one",
            "one_to_many",
            "many_to_one",
            "many_to_many",
        ):
            _invalid("Unsupported relation cardinality")
        for field in ("subject_unit_types", "object_unit_types", "methods"):
            if not _strings(relation.get(field)):
                _invalid("Missing relation endpoint constraints or methods")
        if (
            set(relation["methods"]) - {"native", "human", "parser_inferred", "model"}
            or relation["default_status"]
            not in ("recorded", "proposed", "confirmed", "rejected")
            or relation["revision_binding"] not in ("explicit", "same_revision")
        ):
            _invalid("Unsupported relation method, status or revision binding")
        for field in ("subject_kinds", "object_kinds"):
            if (
                not _strings(relation.get(field, []), nonempty=False)
                or set(relation.get(field, [])) - data["kinds"].keys()
            ):
                _invalid("Unresolved relation endpoint kind")
        for field in ("search_directions", "inspection_directions"):
            if field in relation and (
                not _strings(relation[field], nonempty=False)
                or set(relation[field]) - {"outgoing", "incoming"}
            ):
                _invalid("Unsupported relation direction")
        if (
            relation.get("diagnostic_category", "none")
            not in ("none", "conflict", "limitation", "dependency")
            or type(relation.get("requires_decision", False)) is not bool
        ):
            _invalid("Invalid diagnostic relation rules")
        if key in _RESERVED:
            invariant = {
                **_RESERVED[key],
                "require_endpoint_supports": True,
                "directed": True,
                "methods": ["native"],
                "revision_binding": "same_revision",
                "self_links": False,
                "subject_kinds": ["code_file@1"],
                "object_kinds": ["code_file@1"],
            }
            if any(relation.get(field) != value for field, value in invariant.items()):
                _invalid("Reserved structural relation semantics cannot change")


class ScopedFilter(Contract):
    """Candidate selectors are not applied to allowed context or relation supports."""

    all_units: FilterExpr | None = None
    matched_candidates: FilterExpr | None = None


def _facet_value(facet: Mapping[str, Any], value: JsonValue) -> None:
    kind = facet["value_type"]
    valid = (
        kind == "string"
        and type(value) is str
        or kind == "integer"
        and type(value) is int
        or kind == "number"
        and type(value) in (int, float)
        and math.isfinite(cast(int | float, value))
        or kind == "boolean"
        and type(value) is bool
    )
    if kind in ("date", "date-time") and isinstance(value, str):
        validator = LocalSchemas(
            {"facet-date@1": {"$id": "facet-date@1", "type": "string", "format": kind}},
            {"facet-date@1": {}},
        )
        validator.validate("facet-date@1", value)
        valid = True
    if not valid:
        _invalid("Filter or hint value has the wrong registered type")


def compile_filter(expr: FilterExpr, registry: Registry) -> ScopedFilter:
    """Validate names/types/operators and split only distributable conjunctions."""

    def combine(children: list[FilterExpr]) -> FilterExpr | None:
        return (
            None
            if not children
            else children[0]
            if len(children) == 1
            else AndFilter(children=tuple(children))
        )

    def visit(node: FilterExpr, depth: int = 0) -> ScopedFilter:
        nonlocal budget
        budget -= 1
        if depth > 32 or budget < 0:
            _invalid("Filter complexity exceeds bounded limits")
        if isinstance(node, (AndFilter, OrFilter)):
            children = [visit(child, depth + 1) for child in node.children]
            all_units = [
                child.all_units for child in children if child.all_units is not None
            ]
            candidates = [
                child.matched_candidates
                for child in children
                if child.matched_candidates is not None
            ]
            if isinstance(node, OrFilter):
                if all_units and candidates:
                    _invalid("OR cannot mix candidate and all-unit filter scopes")
                return (
                    ScopedFilter(all_units=OrFilter(children=tuple(all_units)))
                    if all_units
                    else ScopedFilter(
                        matched_candidates=OrFilter(children=tuple(candidates))
                    )
                )
            return ScopedFilter(
                all_units=combine(all_units), matched_candidates=combine(candidates)
            )
        facet = registry.facet(node.field)
        if node.op not in cast(tuple[str, ...], facet["operators"]):
            _invalid("Operator is not registered for this facet")
        values = (
            (node.value,)
            if isinstance(node, EqFilter)
            else node.values
            if isinstance(node, InFilter)
            else tuple(bound for bound in (node.lower, node.upper) if bound is not None)
        )
        for value in values:
            _facet_value(facet, value)
        canonical = node.model_copy(update={"field": facet["key"]})
        return ScopedFilter(**{cast(str, facet["filter_scope"]): canonical})

    budget = 4096
    return visit(expr)
