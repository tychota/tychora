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
    _schema_id,
)
from .evidence import EvidenceUnit, RelationPath
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


class NamedCount(Contract):
    name: NonEmptyStr
    count: NonNegativeInt


class DiscoveryResult(Contract):
    subject: DiscoverySubject
    definitions: tuple[DefinitionEntry, ...] = ()
    counts: tuple[NamedCount, ...] = ()
    capabilities: frozenset[NonEmptyStr] = frozenset()
    next_cursor: Cursor | None = None


class InspectRequest(Contract):
    subject: Literal["evidence", "assets", "revisions", "jobs", "runs", "diagnostics"]
    id: Identifier
    original: bool = False
    range: CodepointRange | None = None
    view: Literal["summary", "full", "original"] = "summary"


class InspectionItem(Contract):
    id: Identifier
    record_type: NonEmptyStr
    schema_id: Identifier
    value: FrozenJsonMap


class InspectionResult(Contract):
    subject: Literal["evidence", "assets", "revisions", "jobs", "runs", "diagnostics"]
    id: Identifier
    evidence: EvidenceUnit | None = None
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
    revision_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


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
    | MigrateMaintenance,
    Field(discriminator="operation"),
]


class MaintenanceRequest(RootModel[MaintenanceVariant]):
    model_config = ConfigDict(frozen=True, strict=True, json_schema_extra=_schema_id)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        return cls(
            TypeAdapter(MaintenanceVariant).validate_json(
                canonical_bytes(value), strict=True
            )
        )


class MaintenancePlan(Contract):
    operation: NonEmptyStr
    target_ids: tuple[Identifier, ...]
    actions: tuple[NonEmptyStr, ...]
    estimated_bytes: NonNegativeInt | None = None


class OperationReceipt(Contract):
    operation: NonEmptyStr
    receipt_id: Identifier
    target_ids: tuple[Identifier, ...]
    completed_at: AwareDatetime
    manifest_sha: Identifier | None = None


class MaintenanceResult(Contract):
    plan: MaintenancePlan
    job: JobHandle | None = None
    receipt: OperationReceipt | None = None


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
