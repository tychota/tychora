"""Public request DTOs; normalization is explicit at primitive boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from .acquisition import AcquisitionLimits
from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
)
from .errors import TychoraError
from .identity import canonical_bytes, normalize_source_key


class RequestScope(Contract):
    principal_id: Identifier
    collection_ids: frozenset[Identifier] = frozenset()
    access_ceiling: NonNegativeInt = 0
    uses: frozenset[NonEmptyStr] = frozenset()
    capabilities: frozenset[NonEmptyStr] = frozenset()
    upload_providers: frozenset[NonEmptyStr] = frozenset()

    def narrow(self, other: Self) -> Self:
        if self.principal_id != other.principal_id:
            raise TychoraError("FORBIDDEN_SCOPE", "Cannot change principal identity")
        return type(self)(
            principal_id=self.principal_id,
            collection_ids=self.collection_ids & other.collection_ids,
            access_ceiling=min(self.access_ceiling, other.access_ceiling),
            uses=self.uses & other.uses,
            capabilities=self.capabilities & other.capabilities,
            upload_providers=self.upload_providers & other.upload_providers,
        )


type FilterValue = str | int | float | bool | None


class EqFilter(Contract):
    op: Literal["eq"] = "eq"
    field: NonEmptyStr
    value: FilterValue


class InFilter(Contract):
    op: Literal["in"] = "in"
    field: NonEmptyStr
    values: Annotated[tuple[FilterValue, ...], Field(min_length=1)]


class RangeFilter(Contract):
    op: Literal["range"] = "range"
    field: NonEmptyStr
    lower: str | int | float | None = None
    upper: str | int | float | None = None
    include_lower: bool = True
    include_upper: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("A range needs at least one bound")
        lower, upper = self.lower, self.upper
        if lower is not None and upper is not None:
            if isinstance(lower, str):
                if not isinstance(upper, str):
                    raise ValueError("Range bounds must have compatible types")  # noqa: TRY004 - Pydantic validation error
                if lower > upper:
                    raise ValueError("Range bounds are reversed")
            else:
                if isinstance(upper, str):
                    raise ValueError("Range bounds must have compatible types")  # noqa: TRY004 - Pydantic validation error
                if lower > upper:
                    raise ValueError("Range bounds are reversed")
        return self


class AndFilter(Contract):
    op: Literal["and"] = "and"
    children: Annotated[tuple[FilterExpr, ...], Field(min_length=1)]


class OrFilter(Contract):
    op: Literal["or"] = "or"
    children: Annotated[tuple[FilterExpr, ...], Field(min_length=1)]


type FilterExpr = Annotated[
    EqFilter | InFilter | RangeFilter | AndFilter | OrFilter, Field(discriminator="op")
]
AndFilter.model_rebuild()
OrFilter.model_rebuild()


class SourceBase(Contract):
    source_key: NonEmptyStr
    collection_id: UUID
    kind: NonEmptyStr
    metadata: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    format: NonEmptyStr | None = None
    encoding: NonEmptyStr | None = None
    media_type: NonEmptyStr | None = None
    sidecars: Annotated[tuple[NonEmptyStr, ...], Field(max_length=10000)] = ()
    acquisition_limits: AcquisitionLimits | None = None

    @field_validator("source_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return normalize_source_key(value)


class FileSource(SourceBase):
    type: Literal["file"] = "file"
    path: NonEmptyStr


class FolderSource(SourceBase):
    type: Literal["folder"] = "folder"
    path: NonEmptyStr


class RepositorySource(SourceBase):
    type: Literal["repository"] = "repository"
    path: NonEmptyStr
    excludes: Annotated[tuple[NonEmptyStr, ...], Field(max_length=1000)] = ()

    @field_validator("excludes")
    @classmethod
    def safe_excludes(cls, excludes: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(excludes)) != len(excludes):
            raise ValueError("Duplicate repository exclude")
        if any(
            "\\" in item
            or "\x00" in item
            or item.startswith("/")
            or ".." in item.split("/")
            for item in excludes
        ):
            raise ValueError("Repository excludes must be relative POSIX globs")
        return excludes


class TextSource(SourceBase):
    type: Literal["text"] = "text"
    text: str


class BytesSource(SourceBase):
    type: Literal["bytes"] = "bytes"
    data: bytes


class UrlSource(SourceBase):
    type: Literal["url"] = "url"
    url: Annotated[str, Field(pattern=r"^https?://[^/\s]+(?:/[^\s]*)?$")]


type SourceVariant = Annotated[
    FileSource | FolderSource | RepositorySource | TextSource | BytesSource | UrlSource,
    Field(discriminator="type"),
]


class SourceInput(RootModel[SourceVariant]):
    model_config = ConfigDict(frozen=True, strict=True)

    # RootModel's keyword constructor already passes the supplied mapping as root.
    # Inheriting it also preserves Pydantic's strict JSON UUID/bytes normalization.

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        # Validate the JSON union first to normalize UUIDs/bytes, then wrap it.
        from pydantic import TypeAdapter

        return cls(TypeAdapter(SourceVariant).validate_json(canonical_bytes(value)))


class QuestionInput(Contract):
    type: Literal["question"] = "question"
    text: NonEmptyStr


class PassageInput(Contract):
    type: Literal["passage"] = "passage"
    text: NonEmptyStr


class ImageInput(Contract):
    type: Literal["image"] = "image"
    asset_ref: Identifier


type QueryInput = Annotated[
    QuestionInput | PassageInput | ImageInput, Field(discriminator="type")
]


class RenderTarget(Contract):
    renderer: NonEmptyStr
    profile_digest: Identifier
    schema_digest: Identifier
    serialization: NonEmptyStr


class BudgetContract(Contract):
    max_tokens: PositiveInt = 6000
    tokenizer_id: NonEmptyStr = "cl100k_base"
    render_targets: tuple[RenderTarget, ...] = ()
    max_response_bytes: PositiveInt = 1048576


class QueryRequest(Contract):
    input: QueryInput
    profile: NonEmptyStr | None = None
    mode: NonEmptyStr | None = None
    filters: FilterExpr | None = None
    hints: tuple[NonEmptyStr, ...] = ()
    revision_ids: tuple[Identifier, ...] = ()
    revision_selector: Literal["current", "all"] = "current"
    as_of: date | None = None
    cutoff: date | None = None
    unknown_dates: Literal["strict", "permissive"] = "strict"
    include_global: bool = False
    modalities: frozenset[NonEmptyStr] = frozenset({"text"})
    previous_unit_ids: tuple[Identifier, ...] = ()
    exact_identifiers: tuple[NonEmptyStr, ...] = ()
    exact_only: bool = False
    limit: Annotated[int, Field(ge=1, le=1000)] = 8
    budgets: BudgetContract = Field(default_factory=BudgetContract)
    render_targets: tuple[RenderTarget, ...] = ()
    trace: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        from pydantic import TypeAdapter

        normalized = _normalize_question(value)
        return cls.model_validate(
            {
                key: TypeAdapter(
                    cls.model_fields[key].rebuild_annotation()
                ).validate_json(canonical_bytes(item), strict=True)
                if key in cls.model_fields
                else item
                for key, item in normalized.items()
            }
        )

    @model_validator(mode="before")
    @classmethod
    def question_convenience(cls, value: Any) -> Any:
        return _normalize_question(value)


def _normalize_question(value: Any) -> Any:
    if isinstance(value, Mapping) and "question" in value:
        if "input" in value:
            raise ValueError("Supply question or input, not both")
        value = dict(value)
        value["input"] = {"type": "question", "text": value.pop("question")}
    return value
