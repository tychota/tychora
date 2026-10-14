"""Synthetic, explicitly enabled local-model PDF OCR qualification."""

import os
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from tychora.adapters.docling.parser import DoclingParser
from tychora.application.owner import WorkspaceOwner
from tychora.application.pdf_processing import PDF_LIMITS, qualified_pdf_runtime
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.pdf_policy import PdfProcessingPolicy
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


def generated_scan(mode="scan"):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    raster = Image.new("RGB", (1200, 1500), "white")
    draw = ImageDraw.Draw(raster)
    font = ImageFont.load_default(size=36)
    small = ImageFont.load_default(size=24)
    for y in range(0, 1500, 40):
        draw.rectangle((0, y, 1199, y + 19), fill=(248, 248, 248))
    for i, line in enumerate(
        (
            "Synthetic archive",
            "The violet engine preserves evidence.",
            "Every page retains its original pixels.",
            "Review all machine transcriptions.",
        )
    ):
        draw.text((100, 160 + i * 100), line, font=font, fill="black")
    draw.text((900, 1200), "Margin note", font=small, fill="black")
    for y in (700, 820, 940, 1060):
        draw.line((100, y, 800, y), fill="black", width=3)
    for x in (100, 500, 800):
        draw.line((x, 700, x, 1060), fill="black", width=3)
    for row, (left, right) in enumerate(
        (("Item", "Count"), ("Violet", "7"), ("Amber", "9"))
    ):
        draw.text((130, 740 + row * 120), left, font=font, fill="black")
        draw.text((550, 740 + row * 120), right, font=font, fill="black")
    if mode in ("gap", "empty"):
        draw.rectangle((0, 0, 1199, 1499), fill="white")
        for y in range(0, 1500, 40):
            draw.rectangle((0, y, 1199, y + 19), fill=(248, 248, 248))
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(400, 500), invariant=1)
    if mode == "native":
        pdf.drawString(40, 400, "Native first page keeps source text.")
    else:
        pdf.drawImage(ImageReader(raster), 0, 0, width=400, height=500)
    pdf.showPage()
    if mode == "empty":
        pdf.save()
        return output.getvalue()
    pdf.setFont("Helvetica", 14)
    pdf.drawString(40, 400, "Native page remains native.")
    pdf.showPage()
    pdf.showPage()
    for page in range(4, 18):
        pdf.drawString(40, 400, f"Native continuation page {page}.")
        pdf.showPage()
    pdf.save()
    return output.getvalue()


@pytest.mark.qualification
@pytest.mark.parametrize("mode", ["scan", "native", "gap", "empty"])
def test_scanned_mixed_pdf_retains_exact_inputs_and_replays(tmp_path, mode):
    model_root = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    if not model_root:
        pytest.skip("Explicit verified local-model bundle required")
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="general-documents@1"\n'
    )
    config = resolve_config(config_path, "test", {})
    assert config.values.extraction.parser == "markdown"
    runtime, models, backend, options = qualified_pdf_runtime(
        Path(model_root), mode != "native"
    )
    policy = PdfProcessingPolicy(
        mode="native" if mode == "native" else "local_ocr",
        native_pages=(
            (1, 2, *range(4, 18))
            if mode == "native"
            else (() if mode == "empty" else (2, *range(4, 18)))
        ),
        ocr_pages=() if mode == "native" else (1,),
        blank_pages=() if mode == "empty" else (3,),
        backend_identity=backend,
        runtime_inventory=runtime,
        model_inventory_sha256=models,
        options=options,
        limits=FrozenJsonMap({**PDF_LIMITS, "max_retained_bytes": 1073741824}),
    )
    ctx = ExecutionContext()
    with WorkspaceOwner.open(
        tmp_path / "data", config, RequestScope(principal_id="synthetic-ocr")
    ) as owner:
        collection = CollectionRow(
            collection_id="00000000-0000-4000-8000-000000000010",
            workspace_id=owner.workspace_id,
            slug="synthetic",
            title="Synthetic",
        )
        source = SourceInput.model_validate(
            {
                "type": "bytes",
                "data": generated_scan(mode),
                "format": "pdf",
                "source_key": "synthetic.pdf",
                "collection_id": UUID(collection.collection_id),
                "kind": "document@1",
                "metadata": {"title": "Synthetic"},
                "pdf_processing": policy,
            }
        )
        capture = owner.run_blocking_sync(
            lambda: Acquirer(owner.artifacts, config).capture(source, ctx), ctx
        )
        sid = identity.source_id(UUID(collection.collection_id), "synthetic.pdf")
        msha = identity.digest("metadata-v1", {"title": "Synthetic"})
        registration = DocumentRegistration(
            capture=capture,
            format="pdf",
            collection=collection,
            workspace=WorkspaceRow(
                workspace_id=owner.workspace_id,
                created_at=owner.run_blocking_sync(
                    lambda: owner.db.execute(
                        "SELECT created_at FROM workspaces"
                    ).fetchone()[0],
                    ctx,
                ),
            ),
            source=SourceRow(
                source_id=sid,
                collection_id=collection.collection_id,
                source_key="synthetic.pdf",
                created_at=capture.captured_at.isoformat(),
            ),
            revision=RevisionRow(
                revision_id=identity.revision_id(sid, capture.original_sha, "1", msha),
                source_id=sid,
                kind_id="document@1",
                original_sha=capture.original_sha,
                edition_key="1",
                metadata_sha=msha,
                title="Synthetic",
                metadata_json=FrozenJsonMap({"title": "Synthetic"}),
                captured_at=capture.captured_at.isoformat(),
                allowed_uses_json=("lookup",),
            ),
        )
        accepted = []
        parser = DoclingParser(
            owner, registration, Path(model_root), policy, accepted.extend
        )
        if mode == "empty":
            from tychora.contracts.errors import TychoraError

            with pytest.raises(TychoraError) as error:
                parser.extract(capture.original_ref, config, ctx)
            assert error.value.code == "INDEX_INCOMPLETE"
            assert accepted == [1]
            assert (
                owner.run_blocking_sync(
                    lambda: owner.db.execute(
                        "SELECT count(*) FROM extractions"
                    ).fetchone()[0],
                    ExecutionContext(),
                )
                == 0
            )
            return
        parsed = parser.extract(capture.original_ref, config, ctx)
        assert accepted == list(range(1, 18))
        assert owner.config.config_sha == config.config_sha
        assert parsed.original_ref == capture.original_ref
        if mode == "scan":
            assert "violet engine" in parsed.ocr_pages[0].transcript.lower()
            assert "margin note" in parsed.ocr_pages[0].transcript.lower()
            assert any(
                unit.unit_type == "table"
                and any(cell for row in unit.cells for cell in row)
                for unit in parsed.units
            )
            assert any(unit.text_origin == "ocr" for unit in parsed.units)
        elif mode in ("gap", "empty"):
            assert parsed.ocr_pages[0].diagnostic == "no-text-detected"
            assert parsed.ocr_pages[0].detected_boxes == 0
        else:
            assert parsed.ocr_pages == ()
        assert any(
            unit.text_origin == "source" and "Native page" in unit.text
            for unit in parsed.units
        )
        assert parsed.image_representation == "scan-external-images@1"
        import json

        from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
        from tychora.contracts.composition import DocumentCompositionPlan
        from tychora.ingestion.composition import (
            DeclaredPayloads,
            compose_document,
            replay_composed_document,
        )
        from tychora.ingestion.documents import replay_document

        def read(ref):
            with owner.artifacts.open_verified(ref) as stream:
                return stream.read(ref.size_bytes + 1)

        def compose():
            proxies, regions = plan_pdf_ocr(owner, parsed, ctx)
            return compose_document(
                parsed,
                Registry.from_config(config),
                DocumentCompositionPlan(
                    base_normalizer_fingerprint=parsed.normalizer_fingerprint,
                    proxies=proxies,
                    pixel_regions=regions,
                ),
                owner.artifacts,
                ctx,
            )

        def verify_clean_raster():
            import pypdfium2 as pdfium
            from PIL import Image

            with pdfium.PdfDocument(generated_scan(mode)) as pdf:
                page = pdf[0]
                bitmap = page.render(scale=4.5, rotation=0, crop=(0, 0, 0, 0))
                clean = bitmap.to_pil().copy().resize((1200, 1500)).convert("RGB")
                bitmap.close()
                page.close()
            with Image.open(
                BytesIO(read(parsed.ocr_pages[0].model_input_image))
            ) as retained:
                assert retained.convert("RGB").tobytes() == clean.tobytes()

        if mode != "native":
            owner.run_blocking_sync(verify_clean_raster, ctx)
        composed = owner.run_blocking_sync(compose, ctx)
        ocr = [unit for unit in composed.bundle.units if unit.text_origin == "ocr"]
        assert bool(ocr) == (mode == "scan")
        assert all(
            not unit.citable and unit.review_state == "needs_review" for unit in ocr
        )
        assert all(
            unit.extraction_id == composed.bundle.extraction.extraction_id
            for unit in composed.bundle.units
        )

        def replay():
            raw = read(parsed.raw_document_ref)
            restored = replay_document(
                raw,
                read(parsed.docling_json_ref),
                parsed.raw_document_ref,
                read_declared=read,
            )
            manifest = read(composed.manifest_ref)
            from tychora.contracts.artifacts import ArtifactRef

            refs = {
                item["sha256"]: ArtifactRef.model_validate(item)
                for item in json.loads(manifest)["required_artifacts"]
            }
            return replay_composed_document(
                manifest,
                composed.manifest_ref,
                restored,
                Registry.from_config(config),
                DeclaredPayloads(refs, read, 512 * 1024 * 1024),
            )

        replayed = owner.run_blocking_sync(replay, ctx)
        assert replayed.bundle == composed.bundle

        def verify_restore_boundary():
            from tychora.application.restore import _validate_retained_documents

            owner.store.commit_extraction(composed.bundle)
            _validate_retained_documents(owner.db, owner.artifacts, config)

        owner.run_blocking_sync(verify_restore_boundary, ctx)

        def verify_selected_responses():
            from types import SimpleNamespace

            from tychora.application.discovery import DiscoveryService
            from tychora.application.inspection import InspectionService
            from tychora.application.vision_selection import (
                _verified_selected_extraction,
            )
            from tychora.contracts.identity import canonical_bytes

            extraction = composed.bundle.extraction.extraction_id
            scope = RequestScope(principal_id="synthetic-ocr")
            coverage = DiscoveryService(
                config, Registry.from_config(config), owner
            )._extraction_coverage(extraction, "document@1", scope)
            if mode != "native":
                assert coverage.stages["ocr"] == "needs_review"
            else:
                assert "ocr" not in coverage.stages
            assert coverage.stages["pdf_processing"] == (
                "indexed_with_gaps" if mode == "gap" else "processed"
            )
            assert coverage.stages["pdf_coverage"]["page_count"] == 17
            inspection = InspectionService(owner)._inspect_diagnostics(
                SimpleNamespace(id=extraction),
                SimpleNamespace(
                    members=(
                        SimpleNamespace(
                            extraction_id=extraction,
                            revision_id=registration.revision.revision_id,
                        ),
                    )
                ),
                SimpleNamespace(
                    view=SimpleNamespace(
                        revision_ids={registration.revision.revision_id}
                    )
                ),
            )
            if mode != "native":
                assert inspection.records[0].value["ocr_review_state"] == "needs_review"
            else:
                assert "ocr_review_state" not in inspection.records[0].value
            assert _verified_selected_extraction(
                owner, parsed, parsed.raw_document_ref.sha256
            )
            response_dir = (
                Path(os.environ.get("TYCHORA_TEST_PDF_RESPONSES", tmp_path)) / mode
            )
            response_dir.mkdir(parents=True, exist_ok=True)
            (response_dir / "coverage-response.json").write_bytes(
                canonical_bytes(coverage.model_dump(mode="json"))
            )
            (response_dir / "inspection-response.json").write_bytes(
                canonical_bytes(inspection.model_dump(mode="json"))
            )

        owner.run_blocking_sync(verify_selected_responses, ctx)
