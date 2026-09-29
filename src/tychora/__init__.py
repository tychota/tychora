"""TyChora evidence discovery and retrieval."""

__version__ = "0.1.0a1"

from .api import LocalTextBinding, ProviderBindings
from .clients.async_ import AsyncTychora
from .clients.sync import Tychora
from .contracts import (
    DiscoverRequest,
    DiscoveryResult,
    EvaluationRequest,
    InspectionResult,
    JobEvent,
    JobHandle,
    MaintenanceRequest,
    MaintenanceResult,
    QueryRequest,
    QueryResult,
    RelationRequest,
    RelationResult,
    RequestScope,
    SourceInput,
    TychoraError,
)
from .formatting.render import OutputProfile, format_result

__all__ = [
    "AsyncTychora",
    "DiscoverRequest",
    "DiscoveryResult",
    "EvaluationRequest",
    "InspectionResult",
    "JobEvent",
    "JobHandle",
    "LocalTextBinding",
    "MaintenanceRequest",
    "MaintenanceResult",
    "OutputProfile",
    "ProviderBindings",
    "QueryRequest",
    "QueryResult",
    "RelationRequest",
    "RelationResult",
    "RequestScope",
    "SourceInput",
    "Tychora",
    "TychoraError",
    "__version__",
    "format_result",
]
