"""Strict DTO primitives and deeply immutable JSON values."""

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, model_validator
from pydantic import JsonValue as PydanticJsonValue
from pydantic_core import CoreSchema, core_schema

from .identity import canonical_bytes

NonEmptyStr = Annotated[str, Field(min_length=1)]
Identifier = NonEmptyStr
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


type FrozenJsonValue = (
    None | bool | int | float | str | tuple[FrozenJsonValue, ...] | FrozenJsonMap
)


def freeze_json(value: object) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return FrozenJsonMap(value)
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    if value is None or type(value) in (str, int, bool):
        return cast(str | int | bool | None, value)
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("Expected finite JSON primitives with string object keys")


def thaw_json(value: FrozenJsonValue) -> PydanticJsonValue:
    if isinstance(value, FrozenJsonMap):
        # Mapping.items() looks each key up again, a linear scan of the entries.
        return {key: thaw_json(item) for key, item in value._entries}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, init=False)
class FrozenJsonMap(Mapping[str, FrozenJsonValue]):
    """A detached immutable mapping; even its backing storage is a tuple."""

    _entries: tuple[tuple[str, FrozenJsonValue], ...]

    def __init__(self, value: Mapping[str, object] | None = None) -> None:
        value = {} if value is None else value
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        object.__setattr__(
            self,
            "_entries",
            tuple(sorted((key, freeze_json(item)) for key, item in value.items())),
        )

    def __getitem__(self, key: str) -> FrozenJsonValue:
        for stored_key, value in self._entries:
            if key == stored_key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = core_schema.dict_schema(
            core_schema.str_schema(strict=True),
            handler.generate_schema(PydanticJsonValue),
        )
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.no_info_before_validator_function(
                lambda value: thaw_json(value) if isinstance(value, cls) else value,
                schema,
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                thaw_json, return_schema=schema
            ),
        )


class Contract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        """Explicit JSON primitive boundary; strict validation does not coerce scalars."""
        return cls.model_validate_json(canonical_bytes(value))


class DateInterval(Contract):
    """Half-open effective interval; a null end is open-ended."""

    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("Interval end must be after start")
        return self


class CodepointRange(Contract):
    char_start: NonNegativeInt
    char_end: NonNegativeInt

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.char_end < self.char_start:
            raise ValueError("Range end precedes start")
        return self
