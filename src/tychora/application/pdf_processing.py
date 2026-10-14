"""Owner-resolved PDF policy preflight shared by intake and retained replay.

Task 2 qualifies explicit policy execution here; current preflight never reports
that the legacy adapter can execute a policy it does not yet consume.
"""

from importlib.metadata import PackageNotFoundError, version

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.pdf_policy import PdfProcessingPolicy

PDF_LIMITS = FrozenJsonMap(
    {
        "max_pages": 1000,
        "max_image_pixels": 40000000,
        "max_objects": 100000,
        "analysis_timeout_seconds": 60,
        "max_child_output_bytes": 268435456,
        "max_metadata_bytes": 268435456,
        "max_extraction_bytes": 16777216,
    }
)


def resolve_pdf_policy(owner, services, descriptor, facts, *, max_retained_bytes):
    blockers = []
    registry = owner.store.registry
    kind = registry.kinds[descriptor.kind]
    if kind["normalizer"] not in {
        "structured_sections@1",
        "rule_records@1",
        "template_slots@1",
        "feedback_records@1",
        "numbered_provisions@1",
    }:
        blockers.append("CAPABILITY_MISSING: Document normalizer is not qualified.")
    if facts["unknown_pages"]:
        blockers.append("INVALID_REQUEST: PDF has pages of unknown content.")
    if not facts["native_text_pages"] and not facts["image_only_pages"]:
        blockers.append("INDEX_INCOMPLETE: PDF has no usable text or scanned pages.")
    runtime = {}
    for package in (
        "docling",
        "docling-core",
        "docling-ibm-models",
        "pypdf",
        "pypdfium2",
        "rapidocr",
        "onnxruntime",
    ):
        try:
            runtime[package] = version(package)
        except PackageNotFoundError:
            blockers.append(
                "CAPABILITY_MISSING: PDF runtime dependency is unavailable."
            )
    model_sha = None
    try:
        from tychora.adapters.docling.parser import validate_models

        if services.sources.docling_models is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Local model bundle is unavailable."
            )
        model_sha = digest(
            "pdf-model-inventory-v1", validate_models(services.sources.docling_models)
        )
    except TychoraError:
        blockers.append(
            "CAPABILITY_MISSING: Verified local PDF model inventory is unavailable."
        )
    # Explicit policy execution and OCR provenance are implemented and qualified by
    # the runtime adapter task. Preparing/analyzing never starts that adapter.
    blockers.append(
        "CAPABILITY_MISSING: Explicit PDF policy execution is not yet qualified."
    )
    policy = None
    if model_sha is not None and not facts["unknown_pages"]:
        scan = bool(facts["image_only_pages"])
        policy = PdfProcessingPolicy(
            mode="local_ocr" if scan else "native",
            native_pages=tuple(facts["native_text_pages"]),
            ocr_pages=tuple(facts["image_only_pages"]),
            blank_pages=tuple(facts["blank_pages"]),
            backend_identity=FrozenJsonMap(
                {"parser": "docling", "native": "default", "ocr": "pdfium"}
            ),
            runtime_inventory=FrozenJsonMap(runtime),
            model_inventory_sha256=model_sha,
            options=FrozenJsonMap(
                {
                    "device": "cpu",
                    "threads": 2,
                    "native_image_scale": 1.0,
                    "ocr_scale": 3.0,
                    "ocr_mode": "full_page",
                    "ocr_backend": "onnxruntime",
                    "remote_services": False,
                    "plugins": False,
                }
            ),
            limits=FrozenJsonMap(
                {**PDF_LIMITS, "max_retained_bytes": max_retained_bytes}
            ),
        )
    return policy, tuple(dict.fromkeys(blockers))


def policy_sha256(policy):
    return digest("pdf-processing-policy-v1", policy.model_dump(mode="json"))


def revalidate_pdf_policy(owner, services, descriptor, facts, policy):
    captured_limit = policy.limits.get("max_retained_bytes")
    current_limit = (
        owner.pdf_intakes.max_retained_bytes if owner.pdf_intakes else 1073741824
    )
    if (
        not isinstance(captured_limit, int)
        or isinstance(captured_limit, bool)
        or not 0 < captured_limit <= current_limit
    ):
        raise TychoraError("SCOPE_CHANGED", "PDF processing budget changed.")
    resolved, blockers = resolve_pdf_policy(
        owner,
        services,
        descriptor,
        facts,
        max_retained_bytes=captured_limit,
    )
    if blockers:
        raise TychoraError(
            "CAPABILITY_MISSING", "PDF processing prerequisites are unavailable."
        )
    if resolved != policy:
        raise TychoraError("SCOPE_CHANGED", "PDF processing runtime or policy changed.")
