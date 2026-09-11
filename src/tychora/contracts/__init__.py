"""Immutable public data contracts independent of adapters and SDKs."""

from typing import Any

from .errors import ErrorEnvelope, TychoraError
from .evidence import (
    AnchorRange,
    EvidenceUnit,
    Locator,
    QueryResult,
    Segment,
    VisualEvidence,
)
from .operations import (
    DiscoverRequest,
    DiscoveryResult,
    EvaluationReport,
    EvaluationRequest,
    InspectionResult,
    InspectRequest,
    JobEvent,
    JobHandle,
    MaintenanceRequest,
    MaintenanceResult,
    RelationRequest,
    RelationResult,
)
from .requests import (
    BudgetContract,
    FilterExpr,
    QueryRequest,
    RenderTarget,
    RequestScope,
    SourceInput,
)

__all__ = [
    "AnchorRange",
    "BudgetContract",
    "DiscoverRequest",
    "DiscoveryResult",
    "ErrorEnvelope",
    "EvaluationReport",
    "EvaluationRequest",
    "EvidenceUnit",
    "FilterExpr",
    "InspectRequest",
    "InspectionResult",
    "JobEvent",
    "JobHandle",
    "Locator",
    "MaintenanceRequest",
    "MaintenanceResult",
    "QueryRequest",
    "QueryResult",
    "RelationRequest",
    "RelationResult",
    "RenderTarget",
    "RequestScope",
    "Segment",
    "SourceInput",
    "TychoraError",
    "VisualEvidence",
    "public_schemas",
]


def public_schemas() -> dict[str, dict[str, Any]]:
    """Return independently resolvable versioned JSON schemas."""
    models = (
        SourceInput,
        QueryRequest,
        QueryResult,
        RequestScope,
        BudgetContract,
        RenderTarget,
        DiscoverRequest,
        DiscoveryResult,
        InspectRequest,
        InspectionResult,
        RelationRequest,
        RelationResult,
        JobHandle,
        JobEvent,
        MaintenanceRequest,
        MaintenanceResult,
        EvaluationRequest,
        EvaluationReport,
        ErrorEnvelope,
    )
    return {model.__name__: model.model_json_schema() for model in models}
