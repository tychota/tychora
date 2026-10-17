"""Owner-resolved qualified PDF policy shared by intake and retained replay."""

from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Self

from tychora.contracts.artifacts import ArtifactRef, physical_identity

if TYPE_CHECKING:
    from tychora.contracts.extraction import ExtractionArtifact

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
        "wall_seconds": 5400,
        "sdk_batch_seconds": 120,
        "batch_seconds": 180,
        "rss_bytes": 4294967296,
        "rss_poll_milliseconds": 250,
        "batch_pages": 16,
        "stage_batch_size": 1,
        "queue_size": 2,
    }
)


def qualified_pdf_runtime(model_root, scan):
    """Verify local inventories; durable identities never contain cache paths."""
    from hashlib import sha256
    from importlib.resources import files
    from pathlib import Path

    from tychora.adapters.docling.parser import validate_models
    from tychora.adapters.vision.local import (
        _MODELS,
        _qualified_runtime_versions,
        _verify_models,
    )

    root = Path(model_root)
    runtime = {
        name: version(name)
        for name in (
            "docling",
            "docling-core",
            "docling-ibm-models",
            "pypdf",
            "pypdfium2",
            "psutil",
        )
    }
    if runtime != {
        "docling": "2.132.0",
        "docling-core": "2.101.1",
        "docling-ibm-models": "4.0.2",
        "pypdf": "6.19.0",
        "pypdfium2": "5.13.0",
        "psutil": "7.2.2",
    }:
        raise TychoraError(
            "CAPABILITY_MISSING", "PDF runtime versions are not qualified"
        )
    inventory = {"docling": validate_models(root)}
    options = {
        "device": "cpu",
        "threads": 2,
        "native_image_scale": 1.0,
        "remote_services": False,
        "plugins": False,
        "telemetry": False,
        "stage_batch_size": 1,
        "queue_size": 2,
    }
    if scan:
        runtime.update(_qualified_runtime_versions())
        _verify_models(root / "rapidocr/3.9.2")
        inventory["ocr"] = [
            {"path": name, "size_bytes": size, "sha256": hashed}
            for name, size, hashed in _MODELS
        ]
        options.update(
            ocr_scale=3.0,
            ocr_mode="full_page",
            ocr_backend="onnxruntime",
            use_det=True,
            use_cls=True,
            use_rec=True,
            text_score=0.5,
            inter_op_threads=1,
            rapidocr_config_sha256=sha256(
                files("rapidocr").joinpath("config.yaml").read_bytes()
            ).hexdigest(),
            input_channels="pdfium-rgb-array-rapidocr-ndarray-no-channel-swap",
            renderer_intermediate_scale=4.5,
            renderer_resize="rounded-crop-dimensions",
        )
    return (
        FrozenJsonMap(runtime),
        digest("pdf-model-inventory-v1", inventory),
        FrozenJsonMap(
            {
                "parser": "docling",
                "native": "ThreadedDoclingParseDocumentBackend",
                "ocr": "PyPdfiumDocumentBackend" if scan else None,
            }
        ),
        FrozenJsonMap(options),
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
    policy = None
    scan = bool(facts["image_only_pages"])
    try:
        if services.sources.docling_models is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Local model bundle is unavailable"
            )
        runtime, model_sha, backend, options = qualified_pdf_runtime(
            services.sources.docling_models, scan
        )
        if not facts["unknown_pages"]:
            policy = PdfProcessingPolicy(
                origin_proof="ordered-ocr-cells@1",
                text_coordinates="docling-assembly@1",
                mode="local_ocr" if scan else "native",
                native_pages=tuple(facts["native_text_pages"]),
                ocr_pages=tuple(facts["image_only_pages"]),
                blank_pages=tuple(facts["blank_pages"]),
                backend_identity=backend,
                runtime_inventory=runtime,
                model_inventory_sha256=model_sha,
                options=options,
                limits=FrozenJsonMap(
                    {
                        **PDF_LIMITS,
                        "max_retained_bytes": min(1073741824, max_retained_bytes),
                    }
                ),
            )
    except (TychoraError, PackageNotFoundError, ImportError):
        blockers.append(
            "CAPABILITY_MISSING: Verified local PDF runtime or models are unavailable."
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


class PdfRetentionBudget:
    """One execution's deduplicated original-plus-derived admission ledger."""

    def __init__(self, max_bytes: int) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= 1073741824:
            raise ValueError("Invalid PDF retention allowance")
        self.max_bytes = max_bytes
        self.retained_bytes = 0
        self._refs: dict[str, ArtifactRef] = {}

    def reserve_refs(self, refs: Iterable[ArtifactRef]) -> None:
        additions: dict[str, ArtifactRef] = {}
        for ref in refs:
            previous = self._refs.get(ref.sha256) or additions.get(ref.sha256)
            if previous is not None:
                if physical_identity(previous) != physical_identity(ref):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "PDF retained artifact declarations conflict"
                    )
            else:
                additions[ref.sha256] = ref
        total = self.retained_bytes + sum(ref.size_bytes for ref in additions.values())
        if total > self.max_bytes:
            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF retained closure exceeds capacity"
            )
        self._refs.update(additions)
        self.retained_bytes = total

    def reserve_payload(self, payload: bytes, media_type: str) -> None:
        from hashlib import sha256

        hashed = sha256(payload).hexdigest()
        self.reserve_refs(
            (
                ArtifactRef(
                    sha256=hashed,
                    storage_key=hashed,
                    size_bytes=len(payload),
                    media_type=media_type,
                ),
            )
        )

    @classmethod
    def for_extraction(
        cls, parsed: "ExtractionArtifact", budget: Self | None = None
    ) -> Self:
        """Seed retained/replay callers, or extend the live parser's same ledger."""
        assert parsed.pdf_processing is not None
        limits = parsed.pdf_processing["limits"]
        assert isinstance(limits, FrozenJsonMap)
        maximum = limits["max_retained_bytes"]
        assert isinstance(maximum, int)
        if budget is None:
            budget = cls(maximum)
        elif budget.max_bytes != maximum:
            raise TychoraError(
                "INTEGRITY_ERROR", "PDF retention allowance differs from policy"
            )
        budget.reserve_refs(
            ref
            for ref in (
                parsed.original_ref,
                parsed.docling_json_ref,
                parsed.raw_document_ref,
                parsed.ocr_evidence_ref,
                parsed.image_bindings_ref,
                *(asset.artifact_ref for asset in parsed.assets),
            )
            if ref is not None
        )
        return budget
