"""Bounded discovery, inspection, jobs, maintenance and evaluation DTOs."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    model_validator,
)

from .base import (
    CodepointRange,
    Contract,
    FiniteFloat,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .evidence import EvidenceUnit, Excerpt, RelationPath
from .identity import canonical_bytes

PageLimit = Annotated[int, Field(ge=1, le=1000)]
Cursor = Annotated[str, Field(min_length=1, max_length=4096)]
DiscoverySubject = Literal[
    "kinds",
    "facets",
    "relations",
    "profiles",
    "languages",
    "sources",
    "settings",
    "runs",
    "models",
]


class DiscoverRequest(Contract):
    subject: DiscoverySubject
    cursor: Cursor | None = None
    limit: PageLimit = 100


class DefinitionEntry(Contract):
    id: Identifier
    version: NonEmptyStr
    label: str
    definition: FrozenJsonMap
    schema_digest: Identifier


class SourceExtractionCoverage(Contract):
    extraction_id: Identifier
    stages: FrozenJsonMap
    code_resolution: FrozenJsonMap | None = None
    channels_truncated: bool = False


class SourceRevisionCoverage(Contract):
    revision_id: Identifier
    kind_id: Identifier
    stages: FrozenJsonMap
    extractions: tuple[SourceExtractionCoverage, ...] = ()
    extractions_truncated: bool = False


class SourceRevisionPage(Contract):
    source_id: Identifier
    revisions: tuple[SourceRevisionCoverage, ...] = ()
    next_cursor: Cursor | None = None
    scope_limited: bool = False


class SourceExtractionPage(Contract):
    source_id: Identifier
    revision_id: Identifier
    extractions: tuple[SourceExtractionCoverage, ...] = ()
    next_cursor: Cursor | None = None


class SourceChannelCoverage(Contract):
    family_id: Identifier
    channel: NonEmptyStr
    state: NonEmptyStr


class SourceChannelPage(Contract):
    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    channels: tuple[SourceChannelCoverage, ...] = ()
    next_cursor: Cursor | None = None


class NamedCount(Contract):
    name: NonEmptyStr
    count: NonNegativeInt


class CapabilityEntry(Contract):
    name: NonEmptyStr
    installed: bool
    required: bool
    version: str | None = None


class DiscoveryResult(Contract):
    subject: DiscoverySubject
    definitions: tuple[DefinitionEntry, ...] = ()
    counts: tuple[NamedCount, ...] = ()
    capabilities: frozenset[NonEmptyStr] = frozenset()
    capability_status: tuple[CapabilityEntry, ...] = ()
    next_cursor: Cursor | None = None


class InspectRequest(Contract):
    subject: Literal["evidence", "assets", "revisions", "jobs", "runs", "diagnostics"]
    id: Identifier
    original: bool = False
    range: CodepointRange | None = None
    ranges: Annotated[tuple[CodepointRange, ...], Field(max_length=100)] = ()
    view: Literal["summary", "full", "original"] = "summary"


class SourceCoverageRequest(Contract):
    """Bounded continuation over one authorized source lineage."""

    subject: Literal["source_revisions", "source_extractions", "source_channels"]
    source_id: Identifier
    revision_id: Identifier | None = None
    extraction_id: Identifier | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if self.subject == "source_revisions":
            if self.revision_id is not None or self.extraction_id is not None:
                raise ValueError("Revision coverage needs only source ID")
        elif self.subject == "source_extractions":
            if (
                self.revision_id is None
                or self.extraction_id is not None
                or self.limit > 20
            ):
                raise ValueError(
                    "Extraction coverage needs revision ID and limit <= 20"
                )
        elif self.revision_id is None or self.extraction_id is None:
            raise ValueError("Channel coverage needs revision and extraction IDs")
        return self


class InspectionItem(Contract):
    id: Identifier
    record_type: NonEmptyStr
    schema_id: Identifier
    value: FrozenJsonMap


class PartialEvidenceMetadata(Contract):
    """Canonical identity; full-text facts are null unless the whole unit is readable."""

    unit_id: Identifier
    extraction_id: Identifier
    unit_type: NonEmptyStr
    canonical_text_sha: Sha256 | None = None
    canonical_text_length: NonNegativeInt | None = None


class InspectionResult(Contract):
    subject: Literal["evidence", "assets", "revisions", "jobs", "runs", "diagnostics"]
    id: Identifier
    evidence: EvidenceUnit | None = None
    partial_evidence: PartialEvidenceMetadata | None = None
    excerpts: tuple[Excerpt, ...] = ()
    records: tuple[InspectionItem, ...] = ()
    original_ref: Identifier | None = None
    payload: bytes | None = None
    media_type: NonEmptyStr | None = None
    truncated: bool = False


class RelationRequest(Contract):
    unit_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=1000)]
    predicates: frozenset[NonEmptyStr] = frozenset()
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    view: Literal["semantic", "structural"] = "semantic"
    max_hops: Annotated[int, Field(ge=0, le=2)] = 2
    max_edges: Annotated[int, Field(ge=0, le=100)] = 100
    cursor: Cursor | None = None


class RelationResult(Contract):
    paths: tuple[RelationPath, ...] = ()
    truncated: bool = False
    next_cursor: Cursor | None = None


JobState = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted"
]


class JobHandle(Contract):
    id: Identifier
    operation: NonEmptyStr
    state: JobState


class JobEvent(Contract):
    schema_version: Literal["1"] = "1"
    job_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    timestamp: AwareDatetime
    stage: NonEmptyStr
    state: JobState
    counters: tuple[NamedCount, ...] = ()
    diagnostic_refs: tuple[Identifier, ...] = ()


class SetupMaintenance(Contract):
    operation: Literal["setup"] = "setup"
    profile: Identifier | None = None
    offline: bool = False
    assets: tuple[NonEmptyStr, ...] = ()
    local_bundle: NonEmptyStr | None = None


class SyncMaintenance(Contract):
    operation: Literal["sync"] = "sync"
    source_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class RemoveMaintenance(Contract):
    operation: Literal["remove"] = "remove"
    source_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class RebuildMaintenance(Contract):
    operation: Literal["rebuild"] = "rebuild"
    channels: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    revision_ids: tuple[Identifier, ...] = ()


class ExportMaintenance(Contract):
    operation: Literal["export"] = "export"
    destination: NonEmptyStr
    include_originals: bool = True
    snapshot_ids: tuple[Identifier, ...] = ()


class ImportMaintenance(Contract):
    operation: Literal["import"] = "import"
    archive_path: NonEmptyStr


class HealthMaintenance(Contract):
    operation: Literal["health"] = "health"
    verify_payloads: bool = False


class EvaluateMaintenance(Contract):
    operation: Literal["evaluate"] = "evaluate"
    dataset_id: Identifier
    split: NonEmptyStr


class HistoryPurgeMaintenance(Contract):
    operation: Literal["history-purge"] = "history-purge"
    before: AwareDatetime


class CachePurgeMaintenance(Contract):
    operation: Literal["cache-purge"] = "cache-purge"
    namespaces: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]


class PurgeOriginalsMaintenance(Contract):
    operation: Literal["purge-originals"] = "purge-originals"
    source_id: Identifier
    revision_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    confirm_original_removal: bool = False


class ProviderUploadConsentMaintenance(Contract):
    """Explicit grant or revoke request for a registered provider and collection."""

    operation: Literal["provider-upload-consent"] = "provider-upload-consent"
    collection_id: Identifier
    provider: NonEmptyStr
    action: Literal["grant", "revoke"]


class MigrateMaintenance(Contract):
    operation: Literal["migrate"] = "migrate"
    target_version: NonEmptyStr


type MaintenanceVariant = Annotated[
    SetupMaintenance
    | SyncMaintenance
    | RemoveMaintenance
    | RebuildMaintenance
    | ExportMaintenance
    | ImportMaintenance
    | HealthMaintenance
    | EvaluateMaintenance
    | HistoryPurgeMaintenance
    | CachePurgeMaintenance
    | PurgeOriginalsMaintenance
    | ProviderUploadConsentMaintenance
    | MigrateMaintenance,
    Field(discriminator="operation"),
]


class MaintenanceRequest(RootModel[MaintenanceVariant]):
    model_config = ConfigDict(frozen=True, strict=True)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        return cls(
            TypeAdapter(MaintenanceVariant).validate_json(
                canonical_bytes(value), strict=True
            )
        )


class OriginalBindingCut(Contract):
    revision_id: Identifier
    artifact_sha: Sha256
    roles: tuple[Literal["revision_original", "original", "source_file"], ...]
    already_removed: bool = False


class OriginalReferenceCut(Contract):
    artifact_sha: Sha256
    size_bytes: NonNegativeInt
    disposition: Literal["delete", "shared", "pending_lease", "already_deleted"]


class MaintenancePlan(Contract):
    operation: NonEmptyStr
    target_ids: tuple[Identifier, ...]
    actions: tuple[NonEmptyStr, ...]
    estimated_bytes: NonNegativeInt | None = None
    original_bindings: tuple[OriginalBindingCut, ...] = ()
    reference_cut: tuple[OriginalReferenceCut, ...] = ()
    retained_derived_artifact_shas: tuple[Sha256, ...] = ()


class OperationReceipt(Contract):
    operation: NonEmptyStr
    receipt_id: Identifier
    target_ids: tuple[Identifier, ...]
    completed_at: AwareDatetime
    manifest_sha: Identifier | None = None


class ProviderUploadConsentState(Contract):
    collection_id: Identifier
    provider: Literal["voyage", "anthropic"]
    action: Literal["grant", "revoke"]
    collection_upload_providers: tuple[NonEmptyStr, ...]
    owner_upload_providers: tuple[NonEmptyStr, ...]
    policy_epoch: NonNegativeInt
    default_owner_requires_reopen: bool = True


class MaintenanceResult(Contract):
    plan: MaintenancePlan
    job: JobHandle | None = None
    receipt: OperationReceipt | None = None
    deleted_payloads: NonNegativeInt = 0
    shared_payloads: NonNegativeInt = 0
    pending_payloads: NonNegativeInt = 0
    upload_consent: ProviderUploadConsentState | None = None


class SettingsActivation(Contract):
    """Complete owner-supplied workspace configuration for validated activation."""

    profile: NonEmptyStr
    workspace_toml: Annotated[str, Field(min_length=1, max_length=1048576)]


class ConfigurationVariant(Contract):
    name: NonEmptyStr
    profile: Identifier
    overrides: FrozenJsonMap = Field(default_factory=FrozenJsonMap)


class EvaluationRequest(Contract):
    dataset_id: Identifier
    split: NonEmptyStr
    variants: Annotated[tuple[ConfigurationVariant, ...], Field(min_length=1)]
    snapshot_id: Identifier
    seed: NonNegativeInt = 0
    repetitions: Annotated[int, Field(ge=1, le=100)] = 1
    offline: bool = True


class MetricValue(Contract):
    name: NonEmptyStr
    value: FiniteFloat | None
    sample_count: NonNegativeInt
    denominator: NonNegativeInt | None = None
    not_applicable_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def consistent_na(self) -> Self:
        if self.value is None and self.not_applicable_reason is None:
            raise ValueError("N/A metrics require a reason")
        if self.value is not None and (
            self.denominator == 0 or self.not_applicable_reason is not None
        ):
            raise ValueError(
                "A scored metric requires a nonzero denominator and no N/A reason"
            )
        return self


class MetricSet(Contract):
    metrics: tuple[MetricValue, ...]


class CaseEvaluation(Contract):
    case_id: Identifier
    variant: NonEmptyStr
    run_id: Identifier
    metrics: MetricSet


class EvaluationReport(Contract):
    dataset_id: Identifier
    split: NonEmptyStr
    snapshot_id: Identifier
    cases: tuple[CaseEvaluation, ...]
    aggregate: MetricSet
    created_at: datetime
