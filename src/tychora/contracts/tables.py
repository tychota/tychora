"""Retained physical table fragments and conservative reconstruction decisions."""

from typing import Annotated, Literal, Self

from pydantic import Field, field_serializer, model_validator

from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
)
from .evidence import DocumentLocator, Geometry


class TableFragment(Contract):
    fragment_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    unit_id: Identifier
    native_ref: NonEmptyStr
    page: PositiveInt | None
    column_count: PositiveInt
    grid: tuple[tuple[str, ...], ...]
    source_cells: tuple[FrozenJsonMap, ...] = ()
    header_rows: tuple[NonNegativeInt, ...] = ()
    caption: str = ""
    table_number: str | None = None
    continuation_marker: bool = False
    heading_path: tuple[str, ...] = ()
    heading_level: NonNegativeInt = 0
    intervening_heading_level: NonNegativeInt | None = None
    bbox: Geometry | None = None
    page_width: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    page_height: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    required_unit_ids: Annotated[frozenset[Identifier], Field(min_length=1)]
    locators: tuple[DocumentLocator, ...]
    asset_ids: tuple[Identifier, ...] = ()

    @field_serializer("required_unit_ids", when_used="json")
    def ordered_supports(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @model_validator(mode="after")
    def grid_and_support(self) -> Self:
        if (
            any(len(row) != self.column_count for row in self.grid)
            or sum(len(row) for row in self.grid) > 100_000
            or len(set(self.header_rows)) != len(self.header_rows)
            or any(row >= len(self.grid) for row in self.header_rows)
            or self.unit_id not in self.required_unit_ids
        ):
            raise ValueError("Table grid, headers or original grounding differ")
        return self


class ContinuationDecision(Contract):
    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    status: Literal["derived", "proposed", "separate"]
    reasons: tuple[NonEmptyStr, ...]
    omit_repeated_header: bool = False


class TableRowReference(Contract):
    fragment_id: Identifier
    row_index: NonNegativeInt
    cells: tuple[str, ...]
