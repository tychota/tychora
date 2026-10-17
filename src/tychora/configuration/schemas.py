"""Bounded Draft 2020-12 validation from immutable, preloaded artifacts only."""

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import (
    JsonValue,
    canonical_bytes,
    digest,
    normalize_source_key,
)

_KEYWORDS = frozenset(
    {
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
)
_FORMATS = FormatChecker(formats=["date"])
# Metaschema checks dominate workspace open; a schema with identical canonical
# content is checked once per process. Failures are never remembered.
_SCHEMA_CHECK_CACHE_SIZE = 4096
_checked_schemas: OrderedDict[str, None] = OrderedDict()
_checked_schemas_lock = threading.Lock()


@_FORMATS.checks("date-time", raises=ValueError)
def _date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value,
    ):
        return False
    return datetime.fromisoformat(value.upper()).tzinfo is not None


def _check_schema_once(document: Any) -> None:
    """Check a schema against the Draft 2020-12 metaschema once per exact content."""
    key = digest("checked-schema-v1", document)
    with _checked_schemas_lock:
        if key in _checked_schemas:
            _checked_schemas.move_to_end(key)
            return
    Draft202012Validator.check_schema(document)
    with _checked_schemas_lock:
        _checked_schemas[key] = None
        while len(_checked_schemas) > _SCHEMA_CHECK_CACHE_SIZE:
            _checked_schemas.popitem(last=False)


def _pointer(document: Any, pointer: str) -> Any:
    if pointer and not pointer.startswith("/"):
        raise ValueError("Only JSON pointers are permitted")
    for part in pointer[1:].split("/") if pointer else ():
        if re.search(r"~(?![01])", part):
            raise ValueError("Invalid JSON pointer")
        document = document[part.replace("~1", "/").replace("~0", "~")]
    return document


class LocalSchemas:
    """Never retrieve a URI. Ref targets come solely from persisted associations."""

    def __init__(
        self,
        schemas: Mapping[str, JsonValue],
        reference_bindings: Mapping[str, JsonValue],
    ) -> None:
        try:
            encoded = canonical_bytes(schemas)
            if len(encoded) > 1048576:
                raise ValueError("Schema size limit")
            documents = json.loads(encoded)
            bindings = json.loads(canonical_bytes(reference_bindings))
            if set(bindings) != set(documents):
                raise ValueError("Missing schema reference map")
            uris = {
                key: "urn:tychora:schema:" + digest("schema-id-v1", key)
                for key in documents
            }
            budget = 4096
            seen: dict[str, set[str]] = {key: set() for key in documents}

            def walk(
                node: Any, key: str, location: str, stack: tuple[tuple[str, str], ...]
            ) -> None:
                nonlocal budget
                budget -= 1
                address = (key, location)
                if budget < 0 or len(stack) > 32 or address in stack:
                    raise ValueError("Schema depth, size or cycle")
                if type(node) is bool:
                    return
                if not isinstance(node, dict) or set(node) - _KEYWORDS:
                    raise ValueError("Unsupported schema keyword")
                if "$id" in node and (location or node["$id"] != key):
                    raise ValueError("Schema identity mismatch")
                if node.get("format") not in (None, "date", "date-time"):
                    raise ValueError("Unsupported format")
                types = node.get("type", [])
                if (
                    types == "array"
                    or isinstance(types, list)
                    and "array" in types
                    or bool({"items", "minItems", "maxItems"} & node.keys())
                ) and (
                    type(node.get("maxItems")) is not int
                    or not 0 <= node["maxItems"] <= 4096
                ):
                    raise ValueError("Unbounded array")
                stack = (*stack, address)
                if "$ref" in node:
                    ref = node["$ref"]
                    if not isinstance(ref, str) or ":" in ref:
                        raise ValueError("Ambient reference")
                    filename, _, fragment = ref.partition("#")
                    if filename:
                        normalize_source_key(filename)
                    if fragment and not fragment.startswith("/"):
                        raise ValueError("Unsupported anchor")
                    ref_location = location + "/$ref"
                    binding = bindings[key][ref_location]
                    if set(binding) != {"schema_id", "fragment"}:
                        raise ValueError("Invalid reference association")
                    target_key = binding["schema_id"]
                    target_location = binding["fragment"]
                    target = _pointer(documents[target_key], target_location)
                    seen[key].add(ref_location)
                    walk(target, target_key, target_location, stack)
                for keyword in ("properties", "$defs"):
                    for child_key, child in node.get(keyword, {}).items():
                        escaped = child_key.replace("~", "~0").replace("/", "~1")
                        walk(child, key, f"{location}/{keyword}/{escaped}", stack)
                for keyword in ("items", "additionalProperties"):
                    if keyword in node:
                        walk(node[keyword], key, f"{location}/{keyword}", stack)

            for key, document in documents.items():
                if not isinstance(document, dict) or document.get("$id") != key:
                    raise ValueError("Missing schema identity")
                walk(document, key, "", ())
                _check_schema_once(document)
            if any(set(bindings[key]) != seen[key] for key in documents):
                raise ValueError("Unexpected reference association")
            # Rewriting only the detached validator copy preserves the stored originals.
            for key, document in documents.items():
                for location, target in bindings[key].items():
                    parent = _pointer(document, location.rsplit("/", 1)[0])
                    parent["$ref"] = (
                        uris[target["schema_id"]]
                        + "#"
                        + quote(target["fragment"], safe="/~")
                    )
                document["$id"] = uris[key]
            resources = Registry().with_resources(
                (
                    uris[key],
                    Resource.from_contents(document, default_specification=DRAFT202012),
                )
                for key, document in documents.items()
            )
            self._validators = {
                key: Draft202012Validator(
                    document, registry=resources, format_checker=_FORMATS
                )
                for key, document in documents.items()
            }
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            SchemaError,
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Invalid, unsupported or unresolved local schema"
            ) from None

    def validate(self, schema_id: str, value: JsonValue) -> JsonValue:
        """Validate a detached JSON value without leaking instance contents in errors."""
        if schema_id not in self._validators:
            raise TychoraError("INVALID_REQUEST", "Unknown schema identity")
        try:
            encoded = canonical_bytes(value)
            if len(encoded) > 1048576:
                raise ValueError("Instance size limit")
            detached = json.loads(encoded)
            self._validators[schema_id].validate(detached)
            return detached
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise TychoraError(
                "INVALID_REQUEST", "Value does not satisfy its original schema"
            ) from None


def import_inclusive_end(value: str | None) -> FrozenJsonMap:
    """Explicit import conversion, retaining the source value as provenance."""
    try:
        if value is None:
            end = None
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Invalid date")
            end = (date.fromisoformat(value) + timedelta(days=1)).isoformat()
        return FrozenJsonMap({"original_inclusive_end": value, "exclusive_end": end})
    except (ValueError, OverflowError, TypeError):
        raise TychoraError(
            "INVALID_REQUEST", "Invalid inclusive effective date"
        ) from None
