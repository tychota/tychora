"""Explicit offline Docling conversion in an owner-managed private process."""

import base64
import io
import json
import os
import warnings
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from math import ceil, isfinite
from pathlib import Path
from threading import Event
from typing import IO, Any, BinaryIO, Literal, NoReturn, cast

from tychora.application.owner import (
    MAX_PARSER_OUTPUT_BYTES,
    StagedOutput,
    WorkspaceOwner,
)
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef, physical_identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.documents import (
    DocumentAsset,
    DocumentImageBinding,
    DocumentRegistration,
    OriginalImage,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Geometry, ImageLocator
from tychora.contracts.extraction import (
    Diagnostic,
    ExtractionArtifact,
    extraction_document,
)
from tychora.contracts.identity import canonical_bytes, digest
from tychora.ports.execution import ExecutionContext
from tychora.ports.vision import pixel_identity

from .mapping import MAX_ITEMS, map_document

MAX_IMAGE_PIXELS: int = 40_000_000
MAX_EXTRACTION_BYTES = 16 * 1024 * 1024
INVENTORY = Path(__file__).with_name("model-inventory.json")


def validate_models(root: Path) -> FrozenJsonMap:
    inventory = json.loads(INVENTORY.read_text())
    for item in inventory["files"]:
        path = root / item["path"]
        try:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != item["size_bytes"]
            ):
                raise ValueError
            hasher = sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    hasher.update(block)
            if hasher.hexdigest() != item["sha256"]:
                raise ValueError
        except (OSError, ValueError):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Docling model bundle missing or corrupt. Explicitly provision the pinned model inventory and pass its local path; ingestion never downloads models.",
            ) from None
    return FrozenJsonMap(inventory)


@contextmanager
def _image_decode_guard() -> Iterator[None]:
    """Enforce image header limits in the isolated child, before SDK decoding."""
    from PIL import Image, ImageFile

    original_open, original_limit = Image.open, Image.MAX_IMAGE_PIXELS
    exceeded = Event()

    def fail() -> NoReturn:
        exceeded.set()
        raise TychoraError(
            "INVALID_REQUEST",
            "Document image exceeds the 40 million pixel predecode limit",
        )

    def checked_open(
        fp: str | bytes | os.PathLike[str] | os.PathLike[bytes] | IO[bytes],
        mode: Literal["r"] = "r",
        formats: list[str] | tuple[str, ...] | None = None,
    ) -> ImageFile.ImageFile:
        with warnings.catch_warnings():
            # Translate Pillow's warning-only interval into our hard limit.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            try:
                opened = original_open(fp, mode, formats)
            except Image.DecompressionBombError:
                fail()
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            opened.close()
            fail()
        return opened

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    # This scoped SDK module-function replacement is intentionally dynamic.
    cast(Any, Image).open = checked_open
    try:
        yield
    finally:
        Image.open, Image.MAX_IMAGE_PIXELS = original_open, original_limit
        # SDK code can catch the first error and return a partial document.
        # That does not authorize silently dropping an over-capacity image.
        if exceeded.is_set():
            fail()


def _check_pdf_image_capacity(path: Path) -> None:
    """Inspect canvases, image objects and separately referenced masks predecode.

    This is a per-image pixel preflight, not a total native-memory bound.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    def check_size(width: float, height: float) -> None:
        if (
            not isfinite(width)
            or not isfinite(height)
            or width <= 0
            or height <= 0
            or ceil(width) * ceil(height) > MAX_IMAGE_PIXELS
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "PDF image or canvas exceeds the predecode pixel limit",
            )

    _check_pdf_mask_capacity(path, check_size)
    work = 0
    try:
        with pdfium.PdfDocument(path) as document:
            for index in range(len(document)):
                with closing(document[index]) as page:
                    check_size(*page.get_size())
                    pending = [(None, 0)]
                    while pending:
                        form, depth = pending.pop()
                        work += 1
                        if work > MAX_ITEMS or depth > 128:
                            raise TychoraError(
                                "INVALID_REQUEST",
                                "PDF metadata exceeds structural capacity",
                            )
                        for item in page.get_objects(max_depth=1, form=form):
                            work += 1
                            if work > MAX_ITEMS:
                                raise TychoraError(
                                    "INVALID_REQUEST",
                                    "PDF metadata exceeds structural capacity",
                                )
                            if item.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
                                check_size(*item.get_px_size())
                            elif item.type == pdfium_raw.FPDF_PAGEOBJ_FORM:
                                pending.append((item, depth + 1))
    except (pdfium.PdfiumError, OSError, ValueError, OverflowError) as exc:
        raise TychoraError(
            "INVALID_REQUEST", "Cannot inspect PDF image dimensions before decoding"
        ) from exc


def _check_pdf_mask_capacity(path: Path, check_size) -> None:
    """Walk PDF XObject dictionaries without decoding image streams."""
    try:
        from pypdf import PdfReader, apply_configuration
        from pypdf.errors import PyPdfError
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject
    except ImportError as exc:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "PDF mask preflight requires the documents extra (pypdf==6.19.0).",
        ) from exc

    work = 0
    active: set[tuple[int, int] | int] = set()
    complete: set[tuple[int, int] | int] = set()

    def malformed() -> NoReturn:
        raise TychoraError(
            "INVALID_REQUEST", "Cannot verify PDF image mask metadata before decoding"
        )

    def resolve(value):
        nonlocal work
        work += 1
        if work > MAX_ITEMS:
            malformed()
        if isinstance(value, IndirectObject):
            return value.get_object()
        return value

    def walk(value, depth: int) -> None:
        if depth > 128:
            malformed()
        key = (
            (value.idnum, value.generation)
            if isinstance(value, IndirectObject)
            else id(value)
        )
        if key in active:
            malformed()
        if key in complete:
            return
        active.add(key)
        try:
            obj = resolve(value)
            if not isinstance(obj, DictionaryObject):
                malformed()
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                for name in ("/SMask", "/Mask"):
                    mask = obj.get(name)
                    if mask is None or mask == "/None":
                        continue
                    mask = resolve(mask)
                    if name == "/Mask" and isinstance(mask, ArrayObject):
                        continue  # Color-key mask has no independent image size.
                    if (
                        not isinstance(mask, DictionaryObject)
                        or mask.get("/Subtype") != "/Image"
                    ):
                        malformed()
                    width, height = (
                        resolve(mask.get("/Width")),
                        resolve(mask.get("/Height")),
                    )
                    if not isinstance(width, int) or not isinstance(height, int):
                        malformed()
                    check_size(width, height)
                    # Follow mask-of-mask references too, including malformed cycles.
                    walk(obj.get(name), depth + 1)
            elif subtype == "/Form":
                if "/Resources" in obj:
                    resources = resolve(obj.get("/Resources"))
                    if not isinstance(resources, DictionaryObject):
                        malformed()
                    xobjects = resolve(resources.get("/XObject", DictionaryObject()))
                    if not isinstance(xobjects, DictionaryObject):
                        malformed()
                    for child in xobjects.values():
                        walk(child, depth + 1)
                # A Form with no resource dictionary uses its caller's context.
                # That dictionary is already walked in full, including images
                # that the Form may name; recursing it again could visit this
                # Form's own binding and falsely report a cycle.
            else:
                malformed()
        finally:
            active.remove(key)
        complete.add(key)

    try:
        with (
            path.open("rb") as pdf_stream,
            apply_configuration(
                maximum_declared_stream_length=64 * 1024 * 1024,
                zlib_maximum_output_length=64 * 1024 * 1024,
                array_based_stream_maximum_output_length=64 * 1024 * 1024,
                page_tree_maximum_entries=MAX_ITEMS,
                page_tree_maximum_depth=128,
            ),
        ):
            reader = PdfReader(pdf_stream, strict=True)
            if len(reader.pages) > MAX_ITEMS:
                malformed()
            for page in reader.pages:
                resources = resolve(page.get("/Resources"))
                if resources is None:
                    continue
                if not isinstance(resources, DictionaryObject):
                    malformed()
                xobjects = resolve(resources.get("/XObject", DictionaryObject()))
                if not isinstance(xobjects, DictionaryObject):
                    malformed()
                for obj in xobjects.values():
                    walk(obj, 0)
    except TychoraError:
        raise
    except (
        PyPdfError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RecursionError,
    ) as exc:
        raise TychoraError(
            "INVALID_REQUEST", "Cannot verify PDF image mask metadata before decoding"
        ) from exc


@dataclass(frozen=True)
class _Convert:
    model_path: str
    format: str
    policy: Any = None

    def run(self, paths, report):
        from .pdf_batches import run_pdf_batches

        try:
            return run_pdf_batches(self, paths, report)
        except RuntimeError as exc:
            # The SDK wraps assembly failures with an explicit cause. Preserve
            # our typed failure for the bounded worker's code-only sanitizer.
            if isinstance(exc.__cause__, TychoraError):
                raise exc.__cause__ from None
            raise

    def __call__(self, paths: tuple[Path, ...]) -> tuple[StagedOutput, ...]:
        # This callable is the entire child payload: no canonical handles.
        os.environ.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_DATASETS_OFFLINE="1",
            TOKENIZERS_PARALLELISM="false",
        )
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            LayoutObjectDetectionOptions,
            PdfPipelineOptions,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import ImageRef

        original = paths[0].with_name("input." + self.format)
        paths[0].rename(original)
        if self.format == "pdf":
            _check_pdf_image_capacity(original)
        layout = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
        layout.model_spec.revision = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
        options = PdfPipelineOptions(
            artifacts_path=Path(self.model_path),
            do_ocr=False,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(),
            layout_options=layout,
            generate_page_images=True,
            generate_picture_images=True,
            images_scale=1.0,
            enable_remote_services=False,
            allow_external_plugins=False,
            accelerator_options=AcceleratorOptions(
                device=AcceleratorDevice.CPU, num_threads=2
            ),
        )
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.IMAGE],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=options,
                    backend_options=ThreadedDoclingParseBackendOptions(
                        render_scale=1.0
                    ),
                ),
                InputFormat.IMAGE: PdfFormatOption(
                    pipeline_options=options,
                    backend_options=ThreadedDoclingParseBackendOptions(
                        render_scale=1.0
                    ),
                ),
            },
        )
        with _image_decode_guard():
            converted = converter.convert(str(original))
            document = converted.document
            for table in document.tables:
                picture = table.get_image(document)
                if picture is not None:
                    table.image = ImageRef.from_pil(picture, dpi=72)
            document_json = document.export_to_dict()
            payload = canonical_bytes(document_json)
            if len(payload) > MAX_PARSER_OUTPUT_BYTES:
                raise ValueError("Docling serialized output exceeds256MiB")
        path = original.with_name("document.json")
        with path.open("xb") as out:
            out.write(payload)
        path.chmod(0o600)
        return (StagedOutput(path.name, "application/json", len(payload)),)


class DoclingParser:
    def __init__(
        self,
        owner: WorkspaceOwner,
        registration: DocumentRegistration,
        model_path: Path,
        policy=None,
        progress=None,
    ):
        self.owner = owner
        self.registration = DocumentRegistration.model_validate(
            registration.model_dump()
        )
        self.model_path = model_path.resolve()
        self.policy = policy
        self.progress = progress
        self._exports = []
        self._evidence = []
        self._lineages = []
        self._lineage = None
        self._external_refs = {}
        self._metadata_bytes = 0
        self.retention_budget = None
        if self.policy is not None:
            from tychora.application.pdf_processing import PdfRetentionBudget

            self.retention_budget = PdfRetentionBudget(
                self.policy.limits["max_retained_bytes"]
            )
            self.retention_budget.reserve_refs(
                (self.registration.capture.original_ref,)
            )

    def _prepare(
        self, original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext
    ):
        ctx.checkpoint()
        if self.policy is None and config.values.extraction.parser != "docling":
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured parser requires its own adapter"
            )
        if physical_identity(original) != physical_identity(
            self.registration.capture.original_ref
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Parser original differs from capture binding"
            )
        if original.size_bytes > config.values.acquisition.limits.max_file_bytes:
            raise TychoraError(
                "INVALID_REQUEST", "Document exceeds acquisition byte limit"
            )
        registry = Registry.from_config(config)
        kind = cast(
            Mapping[str, Any], registry.kinds[self.registration.revision.kind_id]
        )
        registry.validate_metadata(
            self.registration.revision.kind_id, self.registration.revision.metadata_json
        )
        if kind["normalizer"] not in (
            "structured_sections@1",
            "rule_records@1",
            "template_slots@1",
            "feedback_records@1",
            "numbered_provisions@1",
        ):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "The requested rich-document normalizer is not qualified",
            )
        if self.policy is not None:
            from tychora.application.pdf_processing import (
                PDF_LIMITS,
                qualified_pdf_runtime,
            )
            from tychora.contracts.pdf_policy import PdfProcessingPolicy

            captured = self.registration.capture.source.get("pdf_processing")
            if (
                self.registration.format != "pdf"
                or captured is None
                or PdfProcessingPolicy.model_validate_json(canonical_bytes(captured))
                != self.policy
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Explicit PDF policy differs from capture"
                )
            runtime, models, backend, options = qualified_pdf_runtime(
                self.model_path, bool(self.policy.ocr_pages)
            )
            if (
                self.policy.runtime_inventory != runtime
                or self.policy.model_inventory_sha256 != models
                or self.policy.backend_identity != backend
                or self.policy.options != options
                or any(
                    self.policy.limits.get(key) != value
                    for key, value in PDF_LIMITS.items()
                )
            ):
                raise TychoraError("SCOPE_CHANGED", "Qualified PDF policy changed")
            current = (
                self.owner.pdf_intakes.max_retained_bytes
                if self.owner.pdf_intakes
                else 1073741824
            )
            if (
                not 0
                < self.policy.limits["max_retained_bytes"]
                <= min(current, 1073741824)
            ):
                raise TychoraError("SCOPE_CHANGED", "PDF processing allowance changed")
        inventory = validate_models(self.model_path)
        image = self._original_image(original)
        return registry, kind, inventory, image

    def _original_image(self, original: ArtifactRef) -> OriginalImage | None:
        if self.registration.format not in ("png", "jpeg"):
            return None
        from PIL import Image, ImageOps

        with (
            self.owner.artifacts.open_verified(original) as stream,
            Image.open(stream) as encoded,
        ):
            expected = "PNG" if self.registration.format == "png" else "JPEG"
            if (
                encoded.format != expected
                or encoded.width * encoded.height > MAX_IMAGE_PIXELS
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Image MIME, format or pixel capacity differs"
                )
            oriented = ImageOps.exif_transpose(encoded).convert("RGB")
            oriented.load()
            width, height = oriented.size
            pixels = oriented.tobytes()
        transform = "exif-transpose-rgb-v1"
        native_ref = "image-original:" + self.registration.source.source_key
        return OriginalImage(
            native_ref=native_ref,
            artifact_ref=original,
            encoded_sha256=original.sha256,
            pixel_sha256=pixel_identity(pixels, width, height, "RGB", transform),
            width=width,
            height=height,
            color_mode="RGB",
            pixel_transform=transform,
            locator=ImageLocator(
                artifact_ref=original.sha256,
                native_ref=self.registration.source.source_key,
                geometry=Geometry(domain="pixels", x0=0, y0=0, x1=width, y1=height),
            ),
        )

    def extract(
        self, original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext
    ) -> ExtractionArtifact:
        self.owner._sync_thread()
        registry, kind, inventory, image = self._prepare(original, config, ctx)
        if self.policy is not None:
            self.owner.run_parser_sync(
                _Convert(str(self.model_path), self.registration.format, self.policy),
                (original,),
                ctx,
                limits=self._limits(ctx),
                batch_collector=self._accept_batch,
            )
            return self.owner.run_blocking_sync(
                lambda: self._finish_pdf(original, config, registry, kind, inventory),
                ctx,
            )
        return self.owner.run_parser_sync(
            _Convert(str(self.model_path), self.registration.format),
            (original,),
            ctx,
            collector=lambda outputs: self._retain(
                outputs, original, config, registry, kind, inventory, image
            ),
        )

    async def aextract(
        self, original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext
    ) -> ExtractionArtifact:
        registry, kind, inventory, image = await self.owner.run_blocking(
            lambda: self._prepare(original, config, ctx), ctx
        )
        if self.policy is not None:
            await self.owner.run_parser(
                _Convert(str(self.model_path), self.registration.format, self.policy),
                (original,),
                ctx,
                limits=self._limits(ctx),
                batch_collector=self._accept_batch,
            )
            return await self.owner.run_blocking(
                lambda: self._finish_pdf(original, config, registry, kind, inventory),
                ctx,
            )
        return await self.owner.run_parser(
            _Convert(str(self.model_path), self.registration.format),
            (original,),
            ctx,
            collector=lambda outputs: self._retain(
                outputs, original, config, registry, kind, inventory, image
            ),
        )

    def _limits(self, ctx):
        assert self.policy is not None
        from tychora.application.runtime import ParserExecutionLimits

        if (
            self.policy.limits["max_retained_bytes"]
            <= self.registration.capture.original_ref.size_bytes
        ):
            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF original leaves no processing capacity"
            )
        return ParserExecutionLimits(
            page_count=len(self.policy.native_pages)
            + len(self.policy.ocr_pages)
            + len(self.policy.blank_pages),
            wall_seconds=5400,
            rss_bytes=4294967296,
            output_bytes=self.policy.limits["max_retained_bytes"]
            - self.registration.capture.original_ref.size_bytes,
            context=ctx,
        )

    def _accept_batch(self, outputs, pages):
        assert self.policy is not None
        metadata = None
        for descriptor, stream in outputs:
            if descriptor.media_type == "application/json":
                if metadata is not None:
                    raise TychoraError("INTEGRITY_ERROR", "Repeated PDF batch metadata")
                self._metadata_bytes += descriptor.size_bytes
                if self._metadata_bytes > MAX_PARSER_OUTPUT_BYTES:
                    raise TychoraError(
                        "LIMIT_EXCEEDED", "Merged PDF metadata exceeds capacity"
                    )
                payload = stream.read(MAX_PARSER_OUTPUT_BYTES + 1)
                metadata = json.loads(payload)
                if canonical_bytes(metadata) != payload:
                    raise TychoraError("INTEGRITY_ERROR", "Noncanonical PDF batch")
            elif descriptor.media_type == "image/png":
                # Count before retention; deduplicate durable closure by exact hash.
                payload = stream.read(MAX_PARSER_OUTPUT_BYTES + 1)
                hashed = sha256(payload).hexdigest()
                if hashed not in self._external_refs:
                    assert self.retention_budget is not None
                    self.retention_budget.reserve_payload(payload, "image/png")
                    self._external_refs[hashed] = self.owner.artifacts.put_stream(
                        io.BytesIO(payload), "image/png"
                    )
            else:
                raise TychoraError("INTEGRITY_ERROR", "Unexpected PDF batch media")
        if (
            metadata is None
            or tuple(sorted(int(page) for page in metadata["document"]["pages"]))
            != pages
        ):
            raise TychoraError("INDEX_INCOMPLETE", "PDF batch page coverage differs")
        self._exports.append(FrozenJsonMap(metadata["document"]))
        self._evidence.extend(metadata["evidence"])
        if self.policy.origin_proof:
            if "lineage" not in metadata or not isinstance(metadata["lineage"], list):
                raise TychoraError("INTEGRITY_ERROR", "Missing PDF OCR lineage batch")
            self._lineages.append(metadata["lineage"])
        if self.progress:
            self.progress(pages)

    def _reserve_metadata(self, payload):
        assert self.policy is not None
        assert self.retention_budget is not None
        self.retention_budget.reserve_payload(payload, "application/json")

    def _finish_pdf(self, original, config, registry, kind, inventory):
        from .pdf_batches import merge_pdf_page_exports

        assert self.policy is not None
        if self.policy.origin_proof:
            from .ocr_lineage import merge_lineage

            try:
                self._lineage = merge_lineage(self._exports, self._lineages)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Invalid PDF OCR lineage references"
                ) from exc
            self._lineages.clear()
        payload = canonical_bytes(merge_pdf_page_exports(tuple(self._exports)))
        self._exports.clear()
        result = self._retain(
            (
                (
                    StagedOutput("document.json", "application/json", len(payload)),
                    io.BytesIO(payload),
                ),
            ),
            original,
            config,
            registry,
            kind,
            inventory,
        )
        from .ocr_evidence import validate_external_images

        def read(ref):
            with self.owner.artifacts.open_verified(ref) as stream:
                return stream.read(ref.size_bytes + 1)

        validate_external_images(result, read)
        if not any(unit.text.strip() for unit in result.units) and not any(
            page.transcript.strip() for page in result.ocr_pages
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE", "PDF contains no usable transcription"
            )
        return result

    def _retain(
        self,
        outputs: tuple[tuple[StagedOutput, BinaryIO], ...],
        original: ArtifactRef,
        config: ResolvedConfig,
        registry: Registry,
        kind: Mapping[str, Any],
        inventory: FrozenJsonMap,
        original_image: OriginalImage | None = None,
    ) -> ExtractionArtifact:
        if len(outputs) != 1 or outputs[0][0].name != "document.json":
            raise TychoraError("INTEGRITY_ERROR", "Unexpected Docling output")
        payload = outputs[0][1].read(MAX_PARSER_OUTPUT_BYTES + 1)
        document_json = json.loads(payload)
        if canonical_bytes(document_json) != payload:
            raise TychoraError(
                "INTEGRITY_ERROR", "Docling output is not canonical JSON"
            )
        mapped = map_document(document_json, original)
        if self.registration.format == "docx" and any(
            unit.provenance for unit in mapped.units
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Unrendered DOCX unexpectedly supplied physical pages",
            )
        if original_image is not None and mapped.units:
            raise TychoraError(
                "INTEGRITY_ERROR", "Standalone image conversion invented source text"
            )
        if self.policy is not None:
            mapped = mapped.model_copy(
                update={
                    "units": tuple(
                        unit.model_copy(
                            update={
                                "text_origin": "ocr"
                                if any(
                                    prov.physical_page in self.policy.ocr_pages
                                    for prov in unit.provenance
                                )
                                else "source"
                            }
                        )
                        for unit in mapped.units
                    )
                }
            )
        artifacts = self.owner.artifacts
        if self.policy is not None:
            self._reserve_metadata(payload)
        raw_ref = artifacts.put_stream(
            io.BytesIO(canonical_bytes(document_json)), "application/json"
        )
        assets = []
        for native_ref in mapped.image_items:
            node: Any = document_json
            for segment in native_ref[2:].split("/"):
                node = node[int(segment)] if isinstance(node, list) else node[segment]
            image = node["image"]
            uri = image["uri"]
            if self.policy is not None:
                if not isinstance(uri, str) or not uri.startswith(
                    "urn:tychora:artifact:sha256:"
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Explicit PDF image is not declared"
                    )
                image_ref = self._external_refs.get(
                    uri.removeprefix("urn:tychora:artifact:sha256:")
                )
                if image_ref is None:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Undeclared PDF image artifact"
                    )
                width, height = (
                    int(image["size"]["width"]),
                    int(image["size"]["height"]),
                )
            else:
                if (
                    not isinstance(uri, str)
                    or not uri.startswith("data:image/")
                    or ";base64," not in uri
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR",
                        "Docling images must be complete embedded payloads",
                    )
                header, encoded = uri.split(",", 1)
                pixels = base64.b64decode(encoded, validate=True)
                from PIL import Image

                with Image.open(io.BytesIO(pixels)) as decoded:
                    width, height = decoded.size
                    if width * height > MAX_IMAGE_PIXELS or (width, height) != (
                        image["size"]["width"],
                        image["size"]["height"],
                    ):
                        raise TychoraError(
                            "INVALID_REQUEST",
                            "Document image exceeds40million pixels or has inconsistent dimensions",
                        )
                    decoded.verify()
                image_ref = artifacts.put_stream(
                    io.BytesIO(pixels), header[5:].split(";", 1)[0]
                )
            page = (
                int(native_ref.split("/")[-1])
                if native_ref.startswith("#/pages/")
                else None
            )
            unit = next((u for u in mapped.units if u.native_ref == native_ref), None)
            geometry = (
                unit.locators[0].geometry
                if unit
                else Geometry(
                    domain="points",
                    x0=0,
                    y0=0,
                    x1=node["size"]["width"],
                    y1=node["size"]["height"],
                )
            )
            page = unit.locators[0].physical_page if unit else page
            if original_image is not None and native_ref.startswith("#/pages/"):
                page = None
                geometry = original_image.locator.geometry
            captions = [r["$ref"] for r in node.get("captions", [])]
            caption = "\n".join(
                u.text for u in mapped.units if u.native_ref in captions
            )
            assets.append(
                DocumentAsset(
                    native_ref=native_ref,
                    asset_type="page"
                    if native_ref.startswith("#/pages/")
                    else "table"
                    if native_ref.startswith("#/tables/")
                    else "picture",
                    artifact_ref=image_ref,
                    locator=ImageLocator(
                        artifact_ref=original.sha256,
                        native_ref=self.registration.source.source_key,
                        physical_page=page,
                        geometry=geometry,
                    ),
                    width=width,
                    height=height,
                    caption=caption,
                    source_units=tuple(
                        (
                            [original_image.native_ref]
                            if original_image is not None
                            else []
                        )
                        + ([unit.native_ref] if unit else [])
                        + captions
                    ),
                )
            )
        diagnostics = [Diagnostic.model_validate(d) for d in mapped.diagnostics]
        diagnostics.append(
            Diagnostic(
                code="OCR_DISABLED",
                native_ref="#/body",
                message="OCR was explicitly disabled; native text and image pixels are retained.",
            )
        )
        diagnostics.append(
            Diagnostic(
                code="NATIVE_ANNOTATIONS_UNAVAILABLE",
                native_ref="#/body",
                message="This conversion does not qualify native PDF annotation extraction; all available SDK annotation objects remain retained.",
            )
        )
        if self.registration.format == "docx":
            diagnostics.append(
                Diagnostic(
                    code="PAGINATION_UNAVAILABLE",
                    native_ref="#/body",
                    message="DOCX physical pagination requires a separately qualified renderer.",
                )
            )
        parser = digest(
            "docling-parser-v2",
            {
                name: version(name)
                for name in ("docling", "docling-core", "docling-ibm-models")
            },
            inventory,
            self.registration.format,
            config.values.extraction.model_dump(mode="json"),
            {"ocr": False, "image_scale": 1.0, "tables": "accurate", "device": "cpu"},
        )
        if self.policy is not None:
            from tychora.application.pdf_processing import policy_sha256

            parser = digest("docling-pdf-policy-parser-v1", policy_sha256(self.policy))
        from tychora.ingestion.document_records import document_normalizer_fingerprint

        normalizer = document_normalizer_fingerprint(kind, registry)
        data = {
            "registration": self.registration.model_dump(mode="json"),
            "original_ref": original.model_dump(mode="json"),
            "source_text": None,
            "parser_fingerprint": parser,
            "normalizer_fingerprint": normalizer,
            "registry_digest": registry.registry_digest,
            "units": [u.model_dump(mode="json") for u in mapped.units],
            "blocks": [],
            "assets": [a.model_dump(mode="json") for a in assets],
            "assertions": [],
            "records": [],
            "diagnostics": [d.model_dump(mode="json") for d in diagnostics],
            "docling_json_ref": raw_ref.model_dump(mode="json"),
            "original_image": original_image.model_dump(mode="json")
            if original_image is not None
            else None,
        }
        if self.policy is not None:
            from .ocr_evidence import collect_ocr_page_evidence

            evidence = collect_ocr_page_evidence(self._evidence, original, self.policy)
            from .ocr_lineage import evidence_document, validate_parser_lineage

            if self.policy.origin_proof:
                validate_parser_lineage(
                    mapped.units,
                    {page.physical_page: page for page in evidence},
                    self._lineage,
                )
            bindings = tuple(
                DocumentImageBinding(
                    native_ref=asset.native_ref, artifact_ref=asset.artifact_ref
                )
                for asset in assets
            )
            evidence_payload = canonical_bytes(
                evidence_document(evidence, self._lineage, self.policy.origin_proof)
            )
            bindings_payload = canonical_bytes(
                {
                    "version": "scan-external-images@1",
                    "bindings": [
                        binding.model_dump(mode="json") for binding in bindings
                    ],
                }
            )
            self._reserve_metadata(evidence_payload)
            self._reserve_metadata(bindings_payload)
            evidence_ref = artifacts.put_stream(
                io.BytesIO(evidence_payload), "application/json"
            )
            bindings_ref = artifacts.put_stream(
                io.BytesIO(bindings_payload), "application/json"
            )
            diagnostics = [
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.code != "OCR_DISABLED"
            ]
            for page in evidence:
                diagnostics.append(
                    Diagnostic(
                        code="OCR_TRANSCRIBED"
                        if page.transcript
                        else "OCR_NO_TEXT_DETECTED",
                        native_ref=f"#/pages/{page.physical_page}",
                        message="OCR is derived and requires review."
                        if page.transcript
                        else "The qualified detector returned no text; this is not a blank-page assertion.",
                    )
                )
            for page in self.policy.blank_pages:
                diagnostics.append(
                    Diagnostic(
                        code="KNOWN_BLANK_PAGE",
                        native_ref=f"#/pages/{page}",
                        message="Structural analysis found a known blank page.",
                    )
                )
            data.update(
                pdf_processing=self.policy.model_dump(mode="json"),
                image_representation="scan-external-images@1",
                image_bindings=[
                    binding.model_dump(mode="json") for binding in bindings
                ],
                ocr_pages=[page.model_dump(mode="json") for page in evidence],
                ocr_evidence_ref=evidence_ref.model_dump(mode="json"),
                image_bindings_ref=bindings_ref.model_dump(mode="json"),
                diagnostics=[
                    diagnostic.model_dump(mode="json") for diagnostic in diagnostics
                ],
            )
            if self.policy.origin_proof:
                data["ocr_lineage"] = self._lineage
        encoded = extraction_document(data)
        if self.policy is not None:
            self._reserve_metadata(encoded)
        if len(encoded) > MAX_EXTRACTION_BYTES:
            raise TychoraError(
                "LIMIT_EXCEEDED" if self.policy is not None else "INVALID_REQUEST",
                "Extraction manifest exceeds16MiB; no document content was truncated",
            )
        retained = artifacts.put_stream(io.BytesIO(encoded), "application/json")
        from pydantic import ValidationError

        from .ocr_evidence import OcrOriginError

        try:
            return ExtractionArtifact.model_validate_json(
                canonical_bytes(
                    {
                        **data,
                        "document_json": document_json,
                        "raw_document_ref": retained.model_dump(mode="json"),
                    }
                )
            )
        except ValidationError as exc:
            if self.policy is not None and any(
                isinstance(error.get("ctx", {}).get("error"), OcrOriginError)
                for error in exc.errors()
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "PDF OCR origin proof is invalid"
                ) from exc
            raise
