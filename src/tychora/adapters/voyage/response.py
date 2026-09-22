"""Shared lossless raw Voyage response mapping and vector validation."""

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from tychora.contracts.errors import TychoraError

from .validation import normalize_vector


def _invalid_mapping() -> NoReturn:
    raise TychoraError("PROVIDER_FAILURE", "Invalid provider response mapping.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid_mapping()
        result[key] = value
    return result


def _response_object(body: bytes) -> Mapping[str, Any]:
    if not body or len(body) > 16 * 1024 * 1024:
        _invalid_mapping()
    try:
        result = json.loads(body, object_pairs_hook=_unique_object)
    except TychoraError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        _invalid_mapping()
    if not isinstance(result, dict):
        _invalid_mapping()
    return result


def _indexed(
    entries: object, expected: int, *, nested_key: str | None = None
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(entries, list) or len(entries) != expected:
        _invalid_mapping()
    mapped: dict[int, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _invalid_mapping()
        index = entry.get("index")
        if type(index) is not int or index < 0 or index >= expected or index in mapped:
            _invalid_mapping()
        if nested_key is not None and nested_key not in entry:
            _invalid_mapping()
        mapped[index] = entry
    if set(mapped) != set(range(expected)):
        _invalid_mapping()
    return tuple(mapped[index] for index in range(expected))


def _stored_vector(entry: Mapping[str, Any], dimension: int) -> bytes:
    values = entry.get("embedding")
    if not isinstance(values, list) or any(
        type(value) not in (int, float) for value in values
    ):
        _invalid_mapping()
    try:
        converted = tuple(float(value) for value in values)
    except OverflowError:
        _invalid_mapping()
    return normalize_vector(converted, dimension)


def _flat_stored_response(
    body: bytes, expected: int, dimension: int
) -> tuple[bytes, ...]:
    response = _response_object(body)
    entries = _indexed(response.get("data"), expected)
    return tuple(_stored_vector(entry, dimension) for entry in entries)
