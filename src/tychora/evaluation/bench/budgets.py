"""Performance budgets of the engine core design (section 4.12), in one place."""

from collections.abc import Mapping
from types import MappingProxyType

QUERY_STAGE_BUDGETS_MS: Mapping[str, int] = MappingProxyType(
    {
        "validate_scope_catalog": 5,
        "eligibility": 40,
        "candidates": 70,
        "fusion_admission": 5,
        "survivors_rerank_facts": 30,
        "segments_assembly": 50,
        "record": 5,
        "local_total": 205,
        "query_embedding": 300,
        "rerank": 450,
        "total": 955,
    }
)
QUERY_LOCAL_CEILING_MS = 300
QUERY_TOTAL_CEILING_MS = 1000
LOCAL_QUERY_EMBEDDING_BUDGET_MS = 60

QUERY_BUDGET_TRACE_STAGES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "eligibility": ("eligibility",),
        "candidates": ("exact", "lexical", "text_ann", "image_ann", "graph"),
        "fusion_admission": ("fusion", "admission"),
        "survivors_rerank_facts": ("rerank_facts",),
        "segments_assembly": ("assembly",),
        "record": ("record",),
        "query_embedding": ("text_embed", "image_embed"),
        "rerank": ("rerank",),
    }
)

INGEST_STAGE_BUDGETS_S: Mapping[str, float] = MappingProxyType(
    {
        "acquire_register": 0.5,
        "parse": 15.0,
        "normalize_commit": 1.5,
        "windowing": 1.0,
        "embedding": 6.0,
        "batch_writes": 1.0,
        "publication": 0.5,
        "total": 25.5,
        "projection": 9.5,
    }
)
INGEST_TOTAL_CEILING_S = 30.0
PER_PAGE_BUDGETS_S: Mapping[str, float] = MappingProxyType(
    {"scanned_page": 2.5, "book_native_page": 0.25}
)

REFERENCE_TOLERANCE = 0.10
SHARED_RUNNER_REGRESSION = 0.20
