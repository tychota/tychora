"""TyChora evidence discovery and retrieval."""

__version__ = "0.1.0a1"

from importlib import import_module

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


def __getattr__(name: str):
    """Keep command help and completion clear of runtime and provider imports."""
    if name not in __all__:
        raise AttributeError(name)
    if name in {"LocalTextBinding", "ProviderBindings"}:
        module = ".api"
    elif name == "Tychora":
        module = ".clients.sync"
    elif name == "AsyncTychora":
        module = ".clients.async_"
    elif name in {"OutputProfile", "format_result"}:
        module = ".formatting.render"
    else:
        module = ".contracts"
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
