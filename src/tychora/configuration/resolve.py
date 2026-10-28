"""Resolve explicitly registered local, data-only profile packs."""

import importlib.resources
import json
import re
import tomllib
from collections.abc import Mapping
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from tychora.contracts.base import FrozenJsonMap, thaw_json
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import (
    JsonValue,
    canonical_bytes,
    digest,
    normalize_source_key,
)

from .models import (
    AcquisitionCeilings,
    AcquisitionSettings,
    ChecksSettings,
    ConfigurationValues,
    EmbeddingSettings,
    ExtractionSettings,
    GraphSettings,
    IndexSettings,
    LexicalSettings,
    OutputSettings,
    PackIdentity,
    ProfileDocument,
    ProviderSettings,
    RelationChecks,
    RerankerSettings,
    ResolvedConfig,
    ResourceLimits,
    RetrievalSettings,
    SelectionSettings,
    UrlAcquisitionSettings,
    VectorSettings,
    VisionSettings,
    WorkspaceDocument,
)
from .stopwords import load_stopwords


def merge_values(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge maps recursively and replace lists, preserving explicit empty values."""
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


_REGISTRY_FIELDS = (
    "roles",
    "lanes",
    "facets",
    "modes",
    "renderers",
    "output_profiles",
    "compatibility",
)
_DEFINITION_FIELDS = {
    "kind_definitions": "kinds",
    "relation_definitions": "relations",
    "schema_definitions": "schemas",
}


def _read(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(1048577)
    if len(data) > 1048576:
        raise TychoraError(
            "INVALID_REQUEST", "Configuration file must have bounded size"
        )
    return data.decode("utf-8")


def _contained(root: Path, name: str) -> Path:
    try:
        path = (root / normalize_source_key(name)).resolve(strict=True)
    except (ValueError, OSError):
        raise TychoraError(
            "INVALID_REQUEST", "Include must name a contained local file"
        ) from None
    if not path.is_relative_to(root) or not path.is_file():
        raise TychoraError(
            "INVALID_REQUEST", "Include must name a contained local file"
        )
    return path


def _load_pack(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    profile = tomllib.loads(_read(_contained(root, "profile.toml")))
    ProfileDocument.from_mapping(profile)
    files = {"profile.toml": profile}
    registry: dict[str, dict[str, Any]] = {
        key: {} for key in _DEFINITION_FIELDS.values()
    }
    schema_names = list(profile.get("schema_definitions", []))
    for field, category in _DEFINITION_FIELDS.items():
        names = schema_names if category == "schemas" else profile.get(field, [])
        for name in names:
            path = _contained(root, name)
            definition = (
                json.loads(_read(path))
                if category == "schemas"
                else tomllib.loads(_read(path))
            )
            if not isinstance(definition, dict):
                raise TychoraError(
                    "INVALID_REQUEST", "Definition document must be an object"
                )
            files[name] = definition
            key = definition.get("$id" if category == "schemas" else "id")
            if not isinstance(key, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_.:-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", key
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Definition requires a versioned identity"
                )
            if key in registry[category] and registry[category][key] != definition:
                raise TychoraError(
                    "INVALID_REQUEST", "Conflicting immutable definition"
                )
            registry[category][key] = definition
            if category != "schemas":
                for schema_field in ("metadata_schema", "record_schema"):
                    if (
                        schema_field in definition
                        and definition[schema_field] not in schema_names
                    ):
                        schema_names.append(definition[schema_field])
    schema_files = {name: files[name] for name in schema_names}
    registry["schema_reference_bindings"] = _validate_schemas(schema_files)
    registry["schema_bindings"] = {
        f"{profile['id']}@{profile['version']}": {
            name: schema["$id"] for name, schema in schema_files.items()
        }
    }
    registry["definition_schema_bindings"] = {
        category: {
            key: {
                field: schema_files[definition[field]]["$id"]
                for field in ("metadata_schema", "record_schema")
                if field in definition
            }
            for key, definition in registry[category].items()
        }
        for category in ("kinds", "relations")
    }
    return profile, registry, digest("profile-pack-v1", files)


def _validate_schemas(schemas: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    """Validate a bounded schema subset and resolve only preloaded local references."""
    keywords = {
        "$id",
        "$schema",
        "$defs",
        "$ref",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "format",
    }
    budget = 4096
    references: dict[str, dict[str, dict[str, str]]] = {
        schema["$id"]: {} for schema in schemas.values()
    }

    def walk(
        node: Any, name: str, stack: tuple[int, ...] = (), location: str = ""
    ) -> None:
        nonlocal budget
        budget -= 1
        if budget < 0 or len(stack) > 32 or id(node) in stack:
            raise ValueError("schema bounds or cycle")
        if type(node) is bool:
            return
        if not isinstance(node, dict) or set(node) - keywords:
            raise ValueError("unsupported schema keyword")
        stack = (*stack, id(node))
        if node.get("format") not in (None, "date", "date-time"):
            raise ValueError("unsupported schema format")
        types = node.get("type", [])
        if (
            types == "array"
            or isinstance(types, list)
            and "array" in types
            or bool({"items", "minItems", "maxItems"} & node.keys())
        ) and (
            type(node.get("maxItems")) is not int or not 0 <= node["maxItems"] <= 4096
        ):
            raise ValueError("unbounded schema array")
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or ":" in ref:
                raise ValueError("remote schema reference")
            filename, _, fragment = ref.partition("#")
            target_name = (
                str(PurePosixPath(name).parent / normalize_source_key(filename))
                if filename
                else name
            )
            target = schemas[target_name]
            association = {"schema_id": target["$id"], "fragment": fragment}
            source = references[schemas[name]["$id"]]
            reference_location = f"{location}/$ref"
            if (
                reference_location in source
                and source[reference_location] != association
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Conflicting immutable schema reference binding"
                )
            source[reference_location] = association
            if fragment:
                if not fragment.startswith("/"):
                    raise ValueError("unsupported schema anchor")
                for segment in fragment[1:].split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
            walk(target, target_name, stack, fragment)
        for keyword in ("properties", "$defs"):
            for key, child in node.get(keyword, {}).items():
                escaped_key = key.replace("~", "~0").replace("/", "~1")
                walk(child, name, stack, f"{location}/{keyword}/{escaped_key}")
        for keyword in ("items", "additionalProperties"):
            if keyword in node:
                walk(node[keyword], name, stack, f"{location}/{keyword}")

    try:
        for name, schema in schemas.items():
            walk(schema, name)
            Draft202012Validator.check_schema(schema)
        return references
    except (ValueError, TypeError, KeyError, AttributeError, SchemaError):
        raise TychoraError(
            "INVALID_REQUEST", "Invalid, unsupported or unresolved local schema"
        ) from None


def _resolve_config(
    workspace_file: Path,
    profile: str,
    overrides: Mapping[str, JsonValue],
    resources: ExitStack,
) -> ResolvedConfig:
    """Resolve a registered profile into a detached, replayable configuration."""
    if set(overrides) - {"query", "filters", "mode", "output_profile", "limits"}:
        raise TychoraError("INVALID_REQUEST", "Request override is not permitted")
    workspace = WorkspaceDocument.from_mapping(tomllib.loads(_read(workspace_file)))
    roots = {
        key: (workspace_file.parent / binding.path).resolve()
        for key, binding in workspace.profiles.items()
        if binding.path is not None
    }
    if set(workspace.configuration) - (
        set(ConfigurationValues.model_fields) | set(_REGISTRY_FIELDS)
    ):
        raise TychoraError("INVALID_REQUEST", "Unknown workspace configuration option")
    if profile not in workspace.profiles:
        raise TychoraError("INVALID_REQUEST", "Profile has no registered root binding")
    loaded = {root: _load_pack(root) for root in roots.values()}
    by_identity: dict[tuple[str, str], Path] = {}
    for root, (doc, _, sha) in loaded.items():
        identity = (doc["id"], doc["version"])
        if identity in by_identity and loaded[by_identity[identity]][2] != sha:
            raise TychoraError("INVALID_REQUEST", "Conflicting versioned pack digest")
        by_identity[identity] = root
    expected_digests: list[tuple[Path, str]] = []
    ordered: list[Path] = []
    visiting: set[Path] = set()

    builtin_roots: dict[str, Path] = {}

    def builtin(reference: str) -> Path:
        if not reference.startswith("builtin:"):
            raise TychoraError(
                "INVALID_REQUEST", "Dependency shorthand must name a builtin"
            )
        key = reference.removeprefix("builtin:")
        if key in builtin_roots:
            return builtin_roots[key]
        package_root = resources.enter_context(
            importlib.resources.as_file(
                importlib.resources.files("tychora").joinpath("profiles")
            )
        ).resolve()
        manifest = json.loads(_read(_contained(package_root, "manifest.json")))
        if key not in manifest or set(manifest[key]) != {"path", "digest"}:
            raise TychoraError("INVALID_REQUEST", "Builtin dependency is unresolved")
        binding = manifest[key]
        root = (package_root / normalize_source_key(binding["path"])).resolve(
            strict=True
        )
        if not root.is_relative_to(package_root):
            raise TychoraError("INVALID_REQUEST", "Builtin root must be contained")
        loaded[root] = _load_pack(root)
        doc, _, sha = loaded[root]
        identity = (doc["id"], doc["version"])
        if key != f"{doc['id']}@{doc['version']}" or sha != binding["digest"]:
            raise TychoraError(
                "INVALID_REQUEST", "Builtin dependency digest or identity mismatch"
            )
        if identity in by_identity and loaded[by_identity[identity]][2] != sha:
            raise TychoraError("INVALID_REQUEST", "Conflicting versioned pack digest")
        by_identity[identity] = root
        builtin_roots[key] = root
        return root

    def visit(root: Path) -> None:
        if root in visiting:
            raise TychoraError("INVALID_REQUEST", "Profile dependency cycle")
        if root in ordered:
            return
        visiting.add(root)
        document = loaded[root][0]
        if document.get("preset") is not None:
            visit(builtin(document["preset"]))
        for dependency in document.get("extends", []):
            if isinstance(dependency, str):
                visit(builtin(dependency))
                continue
            identity = (dependency["id"], dependency["version"])
            if identity not in by_identity:
                raise TychoraError(
                    "INVALID_REQUEST", "Profile dependency is unresolved"
                )
            target = by_identity[identity]
            expected_digests.append((target, dependency["digest"]))
            visit(target)
        visiting.remove(root)
        ordered.append(root)

    for key, binding in workspace.profiles.items():
        if binding.builtin is not None:
            roots[key] = builtin(f"builtin:{binding.builtin}")
    selected = roots[profile]
    visit(selected)
    for target, expected in expected_digests:
        if loaded[target][2] != expected:
            raise TychoraError("INVALID_REQUEST", "Profile dependency digest mismatch")
    values = ConfigurationValues().model_dump(mode="json")
    registry: dict[str, Any] = {
        key: {}
        for key in (
            *_REGISTRY_FIELDS,
            *_DEFINITION_FIELDS.values(),
            "schema_bindings",
            "definition_schema_bindings",
            "schema_reference_bindings",
        )
    }
    origins: dict[str, Any] = {}
    packs: list[PackIdentity] = []

    def apply(overlay: dict[str, Any], layer: str, source: str) -> None:
        nonlocal values, registry

        def record(node: dict[str, Any], prefix: str = "") -> None:
            for key, item in node.items():
                field = f"{prefix}.{key}" if prefix else key
                if isinstance(item, dict) and item:
                    record(item, field)
                else:
                    origins[field] = {"layer": layer, "key": field, "source": source}

        for category in _DEFINITION_FIELDS.values():
            for key, definition in overlay.get(category, {}).items():
                if key in registry[category] and registry[category][key] != definition:
                    raise TychoraError(
                        "INVALID_REQUEST", "Conflicting immutable definition"
                    )
        for category, bindings in overlay.get("definition_schema_bindings", {}).items():
            for key, binding in bindings.items():
                previous = (
                    registry["definition_schema_bindings"].get(category, {}).get(key)
                )
                if previous is not None and previous != binding:
                    raise TychoraError(
                        "INVALID_REQUEST", "Conflicting immutable schema binding"
                    )
        for key, binding in overlay.get("schema_reference_bindings", {}).items():
            previous = registry["schema_reference_bindings"].get(key)
            if previous is not None and previous != binding:
                raise TychoraError(
                    "INVALID_REQUEST", "Conflicting immutable schema reference binding"
                )
        for key, facet in overlay.get("facets", {}).items():
            previous = registry["facets"].get(key)
            if previous is not None:
                for field in (
                    "key",
                    "value_type",
                    "source_binding",
                    "filter_scope",
                    "allow_global",
                    "missing",
                    "operators",
                ):
                    if field in facet and facet[field] != previous.get(field):
                        raise TychoraError(
                            "INVALID_REQUEST", "Conflicting immutable facet semantics"
                        )
        record(overlay)
        values = merge_values(
            values,
            {k: v for k, v in overlay.items() if k in ConfigurationValues.model_fields},
        )
        registry = merge_values(
            registry, {k: v for k, v in overlay.items() if k in registry}
        )

    apply(values, "defaults", "engine")
    for root in ordered:
        doc, definitions, sha = loaded[root]
        apply(
            {**doc, **definitions},
            f"pack:{doc['id']}@{doc['version']}",
            str(root / "profile.toml"),
        )
        packs.append(PackIdentity(id=doc["id"], version=doc["version"], digest=sha))
    apply(
        workspace.model_dump(mode="json")["configuration"],
        "workspace",
        str(workspace_file.resolve()),
    )
    # Validate the workspace layer before a lower request can conceal invalid limits.
    ConfigurationValues.from_mapping(values)
    request = deepcopy(dict(overrides))
    if "limits" in request and not isinstance(request["limits"], dict):
        raise TychoraError("INVALID_REQUEST", "Request limits must be an object")
    for key, limit in request.get("limits", {}).items():
        if (
            key not in values["limits"]
            or type(limit) is not int
            or limit > values["limits"][key]
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Request limit exceeds a configured ceiling or is invalid",
            )
    ConfigurationValues.from_mapping(request)
    configured_filter = values["filters"]
    if "filters" in request and configured_filter is not None:
        request["filters"] = {
            "op": "and",
            "children": [configured_filter, request["filters"]],
        }
        values["filters"] = None
    apply(request, "request", "request")
    if values["mode"] is None:
        values["mode"] = values["default_mode"]
        origins["mode"] = deepcopy(origins["default_mode"])
    if values["mode"] is not None and values["mode"] not in registry["modes"]:
        raise TychoraError("INVALID_REQUEST", "Selected mode is unresolved")
    output_profile = values["output_profile"]
    if output_profile is not None:
        if output_profile not in registry["output_profiles"]:
            raise TychoraError(
                "INVALID_REQUEST", "Selected output profile is unresolved"
            )
        apply(
            {"output": registry["output_profiles"][output_profile]},
            "output_profile",
            "profile",
        )
    canonical_roots = []
    for registered in values["acquisition"]["roots"]:
        root = Path(registered).resolve(strict=True)
        if not root.is_dir():
            raise TychoraError(
                "INVALID_REQUEST", "Acquisition root must be a directory"
            )
        canonical_roots.append(str(root))
    values["acquisition"]["roots"] = sorted(set(canonical_roots))
    typed = ConfigurationValues.from_mapping(values)
    for modality in ("text", "image"):
        provider = getattr(typed.providers, modality)
        index = getattr(typed.index, modality)
        if provider.provider != "disabled" and provider.model is None:
            raise TychoraError(
                "INVALID_REQUEST", "Enabled provider requires an explicit model"
            )
        if index.backend != "disabled" and provider.provider == "disabled":
            raise TychoraError(
                "INVALID_REQUEST", "Vector index requires a matching modality provider"
            )
    if typed.retrieval.graph.structural != "none" and typed.index.graph != "ladybug":
        raise TychoraError(
            "INVALID_REQUEST",
            "Code structural context requires the Ladybug graph backend",
        )
    return ResolvedConfig(
        profile=profile,
        values=typed,
        registry=FrozenJsonMap(registry),
        origins=FrozenJsonMap(origins),
        packs=tuple(packs),
        **_resolved_hashes(registry, values),
    )


class _ResolvedHashes(TypedDict):
    registry_digest: str
    config_sha: str
    acquire_fingerprint: str
    extraction_fingerprint: str
    index_fingerprint: str
    eligibility_fingerprint: str
    query_fingerprint: str
    output_fingerprint: str


def _resolved_hashes(registry: dict, values: dict) -> _ResolvedHashes:
    registry_sha = digest("registry-v1", registry)
    return {
        "registry_digest": registry_sha,
        "config_sha": digest("config-v1", registry_sha, values),
        "acquire_fingerprint": digest(
            "acquisition-config-v1", values["acquisition"], values["offline"]
        ),
        "extraction_fingerprint": digest(
            "extraction-config-v1",
            values["extraction"],
            registry["kinds"],
            registry["schemas"],
            registry["relations"],
            registry["definition_schema_bindings"],
            registry["schema_reference_bindings"],
        ),
        "index_fingerprint": digest(
            "index-config-v1", values["providers"], values["index"]
        ),
        "eligibility_fingerprint": digest(
            "eligibility-config-v1",
            registry["modes"],
            {
                key: definition.get("allowed_uses")
                for key, definition in registry["kinds"].items()
            },
            {key: lane.get("kinds") for key, lane in registry["lanes"].items()},
            registry["facets"],
            registry["compatibility"],
            values["mode"],
            values["filters"],
            values["retrieval"]["table_continuations"],
        ),
        "query_fingerprint": digest(
            "query-config-v2",
            registry_sha,
            values,
            load_stopwords(
                tuple(values["retrieval"]["lexical"]["languages"]),
                values["retrieval"]["lexical"]["stopwords"],
            ).digest,
        ),
        "output_fingerprint": digest(
            "output-config-v1",
            values["output"],
            registry["lanes"],
            registry["renderers"],
        ),
    }


def validate_retained_config(config: ResolvedConfig) -> ResolvedConfig:
    """Verify every derived digest of a retained, data-only resolved profile."""
    value = config.model_dump(mode="json")
    if _resolved_hashes(value["registry"], value["values"]) != {
        key: value[key] for key in _resolved_hashes(value["registry"], value["values"])
    }:
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained configuration fingerprint differs"
        )
    return config


_EXPORT_SHAPES = {
    ResolvedConfig: (
        "schema_version",
        "profile",
        "values",
        "registry",
        "origins",
        "packs",
        "registry_digest",
        "config_sha",
        "acquire_fingerprint",
        "extraction_fingerprint",
        "index_fingerprint",
        "eligibility_fingerprint",
        "query_fingerprint",
        "output_fingerprint",
    ),
    ConfigurationValues: (
        "offline",
        "default_mode",
        "query",
        "filters",
        "mode",
        "output_profile",
        "retrieval",
        "checks",
        "selection",
        "output",
        "limits",
        "providers",
        "index",
        "extraction",
        "acquisition",
    ),
    ProviderSettings: ("text", "image", "rerank", "vision"),
    EmbeddingSettings: (
        "representation_version",
        "provider",
        "model",
        "dimensions",
        "dtype",
        "credential_ref",
        "enable_auto_chunking",
        "truncation",
        "max_group_tokens",
        "max_request_tokens",
        "max_assets",
        "max_multimodal_tokens",
    ),
    RerankerSettings: ("provider", "model", "credential_ref", "truncation"),
    VisionSettings: ("provider", "model", "credential_ref"),
    GraphSettings: ("structural", "semantic", "predicates", "directions", "statuses"),
    RetrievalSettings: (
        "table_continuations",
        "graph",
        "scoped_rrf_bonus",
        "allow_degraded",
        "lexical",
    ),
    LexicalSettings: ("languages", "stopwords"),
    RelationChecks: ("enabled", "required", "max_assertions"),
    ChecksSettings: ("relations",),
    SelectionSettings: ("lane_reservation",),
    OutputSettings: (
        "renderer",
        "lane_order",
        "include_empty_lanes",
        "locale",
        "title",
        "envelope_tag",
    ),
    ResourceLimits: (
        "limit",
        "max_tokens",
        "max_response_bytes",
        "graph_seed_limit",
        "graph_candidate_limit",
        "graph_work_limit",
        "semantic_hops",
        "inspection_hops",
        "inspection_edges",
    ),
    VectorSettings: ("backend", "metric", "ef_search"),
    IndexSettings: ("text", "image", "graph"),
    ExtractionSettings: ("parser", "representation_version"),
    AcquisitionCeilings: (
        "max_file_bytes",
        "max_files",
        "max_total_bytes",
        "max_inline_bytes",
        "max_redirects",
        "download_timeout_seconds",
    ),
    UrlAcquisitionSettings: ("enabled", "allowed_hosts", "allowed_schemes"),
    AcquisitionSettings: ("roots", "limits", "url"),
    PackIdentity: ("id", "version", "digest"),
}


def exportable_resolved_config(config: ResolvedConfig) -> dict:
    """Project only reviewed profile fields; never resolve credential references."""
    validate_retained_config(config)
    for model, fields in _EXPORT_SHAPES.items():
        if set(model.model_fields) != set(fields):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE", "Configuration export schema needs review"
            )
    if config.values.query is not None:
        raise TychoraError("INVALID_REQUEST", "Export a profile without a stored query")
    origins = thaw_json(config.origins)
    if not isinstance(origins, dict):
        raise TychoraError("INVALID_REQUEST", "Configuration origins are malformed")
    for origin in origins.values():
        if (
            not isinstance(origin, dict)
            or set(origin) != {"layer", "key", "source"}
            or not all(isinstance(value, str) for value in origin.values())
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Configuration origin is outside export schema"
            )
    names = tuple(
        name
        for name in _EXPORT_SHAPES[ConfigurationValues]
        if name not in {"query", "providers"}
    )
    values = config.values.model_dump(mode="json", include=set(names))
    values["query"] = None
    values["providers"] = {
        name: getattr(config.values.providers, name).model_dump(
            mode="json",
            include=set(_EXPORT_SHAPES[type(getattr(config.values.providers, name))]),
        )
        for name in _EXPORT_SHAPES[ProviderSettings]
    }
    document = {
        "schema_version": config.schema_version,
        "profile": config.profile,
        "values": values,
        "registry": thaw_json(config.registry),
        "origins": origins,
        "packs": [
            {field: getattr(pack, field) for field in _EXPORT_SHAPES[PackIdentity]}
            for pack in config.packs
        ],
        **{
            field: getattr(config, field)
            for field in _EXPORT_SHAPES[ResolvedConfig]
            if field.endswith("fingerprint")
            or field in {"registry_digest", "config_sha"}
        },
    }
    rebuilt = validate_retained_config(
        ResolvedConfig.model_validate_json(canonical_bytes(document))
    )
    if rebuilt != config:
        raise TychoraError(
            "INTEGRITY_ERROR", "Selected configuration projection differs"
        )
    return document


def restored_runtime_config(retained: ResolvedConfig) -> ResolvedConfig:
    """Disable inherited acquisition paths and remote runtime use for a restore."""
    validate_retained_config(retained)
    value = retained.model_dump(mode="json")
    value["values"]["offline"] = True
    value["values"]["acquisition"]["roots"] = []
    value["origins"] = {}
    value.update(_resolved_hashes(value["registry"], value["values"]))
    return validate_retained_config(
        ResolvedConfig.model_validate_json(canonical_bytes(value))
    )


def resolve_config(
    workspace_file: Path, profile: str, overrides: Mapping[str, JsonValue]
) -> ResolvedConfig:
    """Validate primitive configuration without constructing storage or providers."""
    try:
        with ExitStack() as resources:
            return _resolve_config(
                workspace_file,
                profile,
                json.loads(canonical_bytes(overrides)),
                resources,
            )
    except (ValueError, TypeError, OSError, KeyError, StopIteration):
        raise TychoraError(
            "INVALID_REQUEST", "Invalid configuration options, values or local binding"
        ) from None


def selected_workspace_profile(workspace_file: Path) -> str:
    """Read the strict, durable selector; old workspaces default to local."""
    try:
        workspace = WorkspaceDocument.from_mapping(tomllib.loads(_read(workspace_file)))
    except (ValueError, TypeError, OSError, KeyError):
        raise TychoraError(
            "INVALID_REQUEST", "Workspace profile selection is invalid."
        ) from None
    return workspace.defaults.profile or "local"
