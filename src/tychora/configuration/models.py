"""Strict, immutable configuration values; definitions are preserved losslessly."""

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from tychora.contracts.base import (
    Contract,
    FrozenJsonMap,
    NonEmptyStr,
    PositiveInt,
    Sha256,
)
from tychora.contracts.requests import FilterExpr, QueryInput


class GraphSettings(Contract):
    structural: Literal["none", "code_context@1"] = "none"
    semantic: bool = False
    predicates: tuple[NonEmptyStr, ...] = ()
    directions: tuple[Literal["outgoing", "incoming"], ...] = ("outgoing",)
    statuses: tuple[NonEmptyStr, ...] = ("recorded", "confirmed")


class RetrievalSettings(Contract):
    table_continuations: Literal["derived", "confirmed_only", "none"] = "derived"
    graph: GraphSettings = Field(default_factory=GraphSettings)
    scoped_rrf_bonus: Annotated[float, Field(ge=0, le=0.01)] = 0.0
    allow_degraded: bool = False


class RelationChecks(Contract):
    enabled: bool = False
    required: bool = False
    max_assertions: Annotated[int, Field(ge=0, le=100)] = 100


class ChecksSettings(Contract):
    relations: RelationChecks = Field(default_factory=RelationChecks)


class SelectionSettings(Contract):
    lane_reservation: Literal[
        "none", "one_per_available_lane", "one_per_required_lane"
    ] = "none"


class OutputSettings(Contract):
    renderer: NonEmptyStr = "hierarchical-evidence@1"
    lane_order: tuple[NonEmptyStr, ...] = ()
    include_empty_lanes: bool = True
    locale: NonEmptyStr = "en"
    title: str = "Evidence pack"
    envelope_tag: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = (
        "evidence_context"
    )


class ResourceLimits(Contract):
    limit: Annotated[int, Field(ge=1, le=1000)] = 8
    max_tokens: PositiveInt = 6000
    max_response_bytes: PositiveInt = 1048576
    graph_seed_limit: Annotated[int, Field(ge=0, le=5)] = 5
    graph_candidate_limit: Annotated[int, Field(ge=0, le=32)] = 32
    graph_work_limit: Annotated[int, Field(ge=0, le=2000)] = 2000
    semantic_hops: Annotated[int, Field(ge=0, le=1)] = 1
    inspection_hops: Annotated[int, Field(ge=0, le=2)] = 2
    inspection_edges: Annotated[int, Field(ge=0, le=100)] = 100


class EmbeddingSettings(Contract):
    representation_version: NonEmptyStr = "1"
    provider: Literal["disabled", "voyage"] = "disabled"
    model: NonEmptyStr | None = None
    dimensions: PositiveInt = 1024
    dtype: Literal["float32"] = "float32"
    credential_ref: (
        Annotated[str, Field(pattern=r"^(env|secret):[A-Za-z_][A-Za-z0-9_.-]*$")] | None
    ) = None
    enable_auto_chunking: Literal[False] = False
    truncation: Literal[False] = False
    max_group_tokens: Annotated[int, Field(gt=0, le=24000)] = 24000
    max_request_tokens: Annotated[int, Field(gt=0, le=28000)] = 28000
    max_assets: Annotated[int, Field(gt=0, le=32)] = 32
    max_multimodal_tokens: Annotated[int, Field(gt=0, le=100000)] = 100000


class ProviderSettings(Contract):
    text: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    image: EmbeddingSettings = Field(default_factory=EmbeddingSettings)


class VectorSettings(Contract):
    backend: Literal["disabled", "chroma"] = "disabled"
    metric: Literal["cosine"] = "cosine"
    ef_search: PositiveInt = 200


class IndexSettings(Contract):
    text: VectorSettings = Field(default_factory=VectorSettings)
    image: VectorSettings = Field(default_factory=VectorSettings)
    graph: Literal["disabled", "ladybug"] = "disabled"


class ExtractionSettings(Contract):
    parser: Literal["markdown", "docling", "code"] = "markdown"
    representation_version: NonEmptyStr = "1"


class AcquisitionCeilings(Contract):
    max_file_bytes: PositiveInt = 104857600
    max_files: Annotated[int, Field(gt=0, le=90000)] = 10000
    max_total_bytes: PositiveInt = 1073741824
    max_inline_bytes: PositiveInt = 10485760
    max_redirects: Annotated[int, Field(ge=0, le=100)] = 3
    download_timeout_seconds: Annotated[
        float, Field(gt=0, le=3600, allow_inf_nan=False)
    ] = 30.0


class UrlAcquisitionSettings(Contract):
    enabled: bool = False
    allowed_hosts: Annotated[tuple[NonEmptyStr, ...], Field(max_length=1000)] = ()
    allowed_schemes: tuple[Literal["https"], ...] = ("https",)

    @field_validator("allowed_hosts")
    @classmethod
    def exact_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        for host in hosts:
            try:
                address = ipaddress.ip_address(host)
                if str(address) != host or not address.is_global:
                    raise ValueError("Allowed address must be canonical and public")
            except ValueError:
                if not re.fullmatch(
                    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    host,
                ):
                    raise ValueError(
                        "Allowed hosts must be exact normalized public hosts"
                    ) from None
        if len(set(hosts)) != len(hosts):
            raise ValueError("Duplicate allowed host")
        return hosts


class AcquisitionSettings(Contract):
    roots: Annotated[tuple[NonEmptyStr, ...], Field(max_length=100)] = ()
    limits: AcquisitionCeilings = Field(default_factory=AcquisitionCeilings)
    url: UrlAcquisitionSettings = Field(default_factory=UrlAcquisitionSettings)

    @field_validator("roots")
    @classmethod
    def absolute_roots(cls, roots: tuple[str, ...]) -> tuple[str, ...]:
        if any(not Path(root).is_absolute() for root in roots):
            raise ValueError("Acquisition roots must be absolute")
        return roots


class ConfigurationValues(Contract):
    offline: bool = True
    default_mode: NonEmptyStr | None = None
    query: QueryInput | None = None
    filters: FilterExpr | None = None
    mode: NonEmptyStr | None = None
    output_profile: NonEmptyStr | None = None
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    checks: ChecksSettings = Field(default_factory=ChecksSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    index: IndexSettings = Field(default_factory=IndexSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    acquisition: AcquisitionSettings = Field(default_factory=AcquisitionSettings)


class Dependency(Contract):
    id: NonEmptyStr
    version: NonEmptyStr
    digest: Sha256


class ProfileDocument(ConfigurationValues):
    schema_version: Literal[1]
    id: NonEmptyStr
    version: NonEmptyStr
    preset: NonEmptyStr | None = None
    extends: tuple[Dependency | NonEmptyStr, ...] = ()
    kind_definitions: tuple[NonEmptyStr, ...] = ()
    relation_definitions: tuple[NonEmptyStr, ...] = ()
    schema_definitions: tuple[NonEmptyStr, ...] = ()
    roles: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    lanes: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    facets: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    modes: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    renderers: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    output_profiles: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    compatibility: FrozenJsonMap = Field(default_factory=FrozenJsonMap)


class RootBinding(Contract):
    path: NonEmptyStr | None = None
    builtin: NonEmptyStr | None = None

    @model_validator(mode="after")
    def exclusive_binding(self) -> Self:
        if (self.path is None) == (self.builtin is None):
            raise ValueError("Register exactly one path or builtin")
        return self


class WorkspaceDefaults(Contract):
    profile: NonEmptyStr | None = None


class WorkspaceDocument(Contract):
    schema_version: Literal[1]
    profiles: dict[str, RootBinding]
    defaults: WorkspaceDefaults = Field(default_factory=WorkspaceDefaults)
    configuration: FrozenJsonMap = Field(default_factory=FrozenJsonMap)


class PackIdentity(Contract):
    id: NonEmptyStr
    version: NonEmptyStr
    digest: Sha256


class ResolvedConfig(Contract):
    schema_version: Literal[1] = 1
    profile: NonEmptyStr
    values: ConfigurationValues
    registry: FrozenJsonMap
    origins: FrozenJsonMap
    packs: tuple[PackIdentity, ...]
    registry_digest: Sha256
    config_sha: Sha256
    acquire_fingerprint: Sha256
    extraction_fingerprint: Sha256
    index_fingerprint: Sha256
    eligibility_fingerprint: Sha256
    query_fingerprint: Sha256
    output_fingerprint: Sha256

    def inspect(self, *, owner_authorized: bool = False) -> FrozenJsonMap:
        """Expose effective configuration, redacting private origins and credentials."""
        result = self.model_dump(mode="json")
        if not owner_authorized:
            result["values"]["acquisition"]["roots"] = ["[redacted]"] * len(
                self.values.acquisition.roots
            )
            for origin in result["origins"].values():
                if origin["source"] not in ("engine", "request"):
                    origin["source"] = "[redacted]"
            for provider in result["values"]["providers"].values():
                if provider["credential_ref"] is not None:
                    provider["credential_ref"] = "[redacted]"
        return FrozenJsonMap(result)
