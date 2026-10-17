"""Detached, bounded collection administration and inventory contracts."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from .operations import Cursor, SourceChannelCoverage


class CollectionPolicyV2(Contract):
    schema_version: Literal[2] = 2
    upload_providers: tuple[NonEmptyStr, ...] = ()
    owner_upload_providers: tuple[NonEmptyStr, ...] = ()
    archived_at: AwareDatetime | None = None
    default_kind_id: Identifier | None = None


class CollectionCreate(Contract):
    default_kind_id: Identifier | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class CollectionEdit(CollectionCreate):
    pass


class CollectionSummary(Contract):
    collection_id: Identifier
    workspace_id: Identifier
    slug: NonEmptyStr
    title: NonEmptyStr
    archived_at: AwareDatetime | None = None
    default_kind_id: Identifier | None = None
    effective_default_kind_id: Identifier | None = None
    access_floor: NonNegativeInt
    policy_epoch: NonNegativeInt
    document_count: NonNegativeInt | None = None
    summary_complete: bool = False


class CollectionPage(Contract):
    collections: tuple[CollectionSummary, ...]
    next_cursor: Cursor | None = None
    policy_epoch: NonNegativeInt


class CollectionConsentView(Contract):
    collection_id: Identifier
    collection_upload_providers: tuple[NonEmptyStr, ...]
    owner_upload_providers: tuple[NonEmptyStr, ...]
    caller_upload_providers: tuple[NonEmptyStr, ...]
    enabled_upload_providers: tuple[NonEmptyStr, ...]
    policy_epoch: NonNegativeInt
    managed_grant_refresh_available: bool


class DocumentSummary(Contract):
    source_id: Identifier
    source_key: NonEmptyStr
    title: NonEmptyStr
    collection_id: Identifier
    collection_title: NonEmptyStr
    source_type: NonEmptyStr
    revision_id: Identifier | None = None
    extraction_id: Identifier | None = None
    original_media_type: NonEmptyStr | None = None
    original_size_bytes: NonNegativeInt | None = None
    created_at: AwareDatetime | None = None
    captured_at: AwareDatetime | None = None
    processing_state: Literal["unknown"] = "unknown"
    published_content_state: Literal["published", "not_observed"] = "not_observed"
    channel_readiness: tuple[NonEmptyStr, ...] = ()
    channel_observations: tuple[SourceChannelCoverage, ...] = ()
    channel_observations_complete: bool = False
    latest_job: None = None
    job_observations_complete: bool = False
    unit_count: NonNegativeInt | None = None
    window_count: NonNegativeInt | None = None
    counts_complete: bool = False
    counts_unavailable_reason: Literal["bounded_exact_authorization_unavailable"] = (
        "bounded_exact_authorization_unavailable"
    )
    jobs_unavailable_reason: Literal["bounded_subject_lineage_unavailable"] = (
        "bounded_subject_lineage_unavailable"
    )


class DocumentPage(Contract):
    documents: tuple[DocumentSummary, ...]
    next_cursor: Cursor | None = None
    policy_epoch: NonNegativeInt
    candidate_record_limit: Literal[5000] = 5000
    channel_record_limit: Literal[400] = 400
    job_record_limit: Literal[100] = 100
    job_metadata_byte_limit: Literal[1048576] = 1048576
