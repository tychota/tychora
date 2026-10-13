import json
import os
import shutil
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tychora.adapters.docling.parser import DoclingParser
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.extraction import DocumentRegistration
from tychora.contracts.requests import RequestScope
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


@pytest.fixture(params=["pdf", "docx"])
def execution(request, tmp_path):
    path = tmp_path / "workspace.toml"
    pack = tmp_path / "document-pack"
    shutil.copytree(str(files("tychora") / "profiles/documents"), pack)
    with (pack / "profile.toml").open("a") as out:
        out.write('\n[extraction]\nparser="docling"\n')
    path.write_text('schema_version=1\n[profiles.test]\npath="document-pack"\n')
    with path.open("a") as out:
        out.write(
            "\n[configuration.acquisition]\nroots="
            + json.dumps(
                [str((Path(__file__).parents[1] / "fixtures/documents").resolve())]
            )
            + "\n"
        )
    config = resolve_config(path, "test", {})
    with WorkspaceOwner.open(
        tmp_path / "data", config, RequestScope(principal_id="fixture")
    ) as owner:
        yield SimpleNamespace(
            config=config, ctx=ExecutionContext(), owner=owner, format=request.param
        )


@pytest.fixture
def rich_capture(execution):
    owner = execution.owner
    metadata = json.loads(
        (
            Path(__file__).parents[1] / "configuration/fixtures/generic-records.json"
        ).read_text()
    )["manual@1"]["metadata"]
    collection = CollectionRow(
        collection_id="00000000-0000-4000-8000-000000000010",
        workspace_id=owner.workspace_id,
        slug="synthetic",
        title="Synthetic documents",
    )
    source_key = "rich." + execution.format
    source_path = Path(__file__).parents[1] / "fixtures/documents" / source_key
    captured = owner.run_blocking_sync(
        lambda: Acquirer(owner.artifacts, execution.config).capture(
            SourceInput.model_validate(
                {
                    "type": "file",
                    "path": str(source_path),
                    "kind": "manual@1",
                    "metadata": metadata,
                    "collection_id": UUID(collection.collection_id),
                    "source_key": source_key,
                }
            ),
            execution.ctx,
        ),
        execution.ctx,
    )
    sid = identity.source_id(UUID(collection.collection_id), source_key)
    msha = identity.digest("metadata-v1", metadata)
    revision = RevisionRow(
        revision_id=identity.revision_id(sid, captured.original_sha, "1", msha),
        source_id=sid,
        kind_id="manual@1",
        original_sha=captured.original_sha,
        edition_key="1",
        metadata_sha=msha,
        title=metadata["title"],
        metadata_json=FrozenJsonMap(metadata),
        captured_at=captured.captured_at.isoformat(),
        allowed_uses_json=("lookup",),
    )
    execution.registration = DocumentRegistration(
        capture=captured,
        workspace=WorkspaceRow(
            workspace_id=owner.workspace_id,
            created_at=owner.run_blocking_sync(
                lambda: owner.db.execute(
                    "SELECT created_at FROM workspaces"
                ).fetchone()[0],
                execution.ctx,
            ),
        ),
        collection=collection,
        source=SourceRow(
            source_id=sid,
            collection_id=collection.collection_id,
            source_key=source_key,
            created_at=captured.captured_at.isoformat(),
        ),
        revision=revision,
        format=execution.format,
    )
    return captured.original_ref


@pytest.fixture
def docling_parser(execution, rich_capture):
    bundle = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    if not bundle:
        pytest.fail(
            "Set TYCHORA_TEST_DOCLING_MODELS to an explicitly provisioned, checksum-verified Docling model bundle; ingestion never downloads models."
        )
    return DoclingParser(execution.owner, execution.registration, Path(bundle))


@pytest.mark.qualification
def test_real_docling_retains_document_and_locators(
    rich_capture, docling_parser, execution
):
    artifact = docling_parser.extract(rich_capture, execution.config, execution.ctx)
    assert artifact.parser_fingerprint
    assert artifact.raw_document_ref is not None
    if execution.format == "pdf":
        assert any(
            u.locators and u.locators[0].physical_page == 2 for u in artifact.units
        )
    else:
        assert all(
            loc.physical_page is None for u in artifact.units for loc in u.locators
        )
    assert artifact.assets

    from tychora.configuration.registry import Registry
    from tychora.ingestion.normalize import normalize

    registry = Registry.from_config(execution.config)
    bundle = normalize(artifact, execution.registration.revision.revision_id, registry)
    assert bundle.assets
    assert len(bundle.units) == len(artifact.units)
    owner = execution.owner
    owner.run_blocking_sync(
        lambda: owner.store.commit_extraction(bundle), execution.ctx
    )

    from tychora.adapters.files.references import required_refs
    from tychora.ingestion.documents import replay_document

    def retained_bytes(ref):
        with owner.artifacts.open_verified(ref) as stream:
            return stream.read()

    manifest = owner.run_blocking_sync(
        lambda: retained_bytes(artifact.raw_document_ref), execution.ctx
    )
    document = owner.run_blocking_sync(
        lambda: retained_bytes(artifact.docling_json_ref), execution.ctx
    )
    replayed = replay_document(manifest, document, artifact.raw_document_ref)
    assert (
        normalize(replayed, execution.registration.revision.revision_id, registry)
        == bundle
    )
    assert artifact.original_ref.sha256 not in required_refs(
        json.loads(manifest), capture_published=True
    )
    assert {
        artifact.docling_json_ref.sha256,
        *(a.artifact_ref.sha256 for a in artifact.assets),
    } <= required_refs(json.loads(manifest), capture_published=True)
    assert b'"raw_item"' not in manifest
    expected = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/documents/rich-manifest.json"
        ).read_text()
    )
    for passage in expected["pdf"]["passages"]:
        found = [
            unit
            for unit in artifact.units
            if passage["text"] in unit.text
            and (
                execution.format != "pdf"
                or (
                    unit.locators
                    and all(
                        locator.physical_page == passage["page"]
                        for locator in unit.locators
                    )
                )
            )
        ]
        if not found:
            for unit in artifact.units:
                if unit.text:
                    pages = [locator.physical_page for locator in unit.locators]
                    print(unit.native_ref, unit.unit_type, pages, repr(unit.text))
        assert found, passage
    if execution.format == "pdf":
        tables = [u for u in artifact.units if u.unit_type == "table"]
        assert len(tables) == 2
        for table, reference in zip(tables, expected["pdf"]["tables"]):
            assert table.cells == tuple(tuple(row) for row in reference["rows"])
            geometry = table.locators[0].geometry
            assert table.locators[0].physical_page == reference["page"]
            assert (
                max(
                    abs(value - gold)
                    for value, gold in zip(
                        (geometry.x0, geometry.y0, geometry.x1, geometry.y1),
                        reference["bbox"],
                    )
                )
                < expected["pdf"]["table_bbox_tolerance_points"]
            )
        figure = next(a for a in artifact.assets if a.asset_type == "picture")
        g = figure.locator.geometry
        x0, y0, x1, y1 = expected["pdf"]["figure"]["image_box"]
        assert x0 <= g.x0 <= g.x1 <= x1 and y0 <= g.y0 <= g.y1 <= y1
        tolerance = expected["pdf"]["figure"]["raster_rounding_tolerance_points"]
        for region in expected["pdf"]["figure"]["visible_regions"].values():
            assert g.x0 <= region[0] + tolerance and g.y0 <= region[1] + tolerance
            assert g.x1 >= region[2] - tolerance and g.y1 >= region[3] - tolerance
        assert figure.caption == expected["pdf"]["figure"]["caption"]
    else:
        assert len(next(u for u in artifact.units if u.unit_type == "table").cells) == 5


def _docx_capacity_probe(source, staged, ceiling, sender):
    """Run controlled instrumentation inside an isolated conversion process."""
    from shutil import copyfile

    from docling.backend.msword_backend import MsWordDocumentBackend
    from docling.document_converter import DocumentConverter
    from PIL import Image, PngImagePlugin

    from tychora.adapters.docling import parser
    from tychora.contracts.errors import TychoraError

    parser.MAX_IMAGE_PIXELS = ceiling
    original_open = Image.open
    original_load = PngImagePlugin.PngImageFile.load
    original_save = Image.Image.save
    original_pictures = MsWordDocumentBackend._handle_pictures
    original_convert = DocumentConverter.convert
    counts = {"load": 0, "save": 0, "caught": 0, "sdk_returned": False}

    def observed_load(self, *args, **kwargs):
        if self.size == (640, 320):
            counts["load"] += 1
        return original_load(self, *args, **kwargs)

    def observed_save(self, *args, **kwargs):
        if self.size == (640, 320):
            counts["save"] += 1
        return original_save(self, *args, **kwargs)

    def catching_backend(self, *args, **kwargs):
        try:
            return original_pictures(self, *args, **kwargs)
        except (TychoraError, Image.DecompressionBombError):
            # Model an SDK fallback that drops the offending object and returns
            # a usable document: the application must still reject this result.
            counts["caught"] += 1
            return []

    def observed_convert(self, *args, **kwargs):
        result = original_convert(self, *args, **kwargs)
        counts["sdk_returned"] = True
        return result

    PngImagePlugin.PngImageFile.load = observed_load
    Image.Image.save = observed_save
    MsWordDocumentBackend._handle_pictures = catching_backend
    DocumentConverter.convert = observed_convert
    staged.mkdir()
    original = staged / "0"
    copyfile(source, original)
    try:
        parser._Convert("/unused-docx-model-path", "docx")((original,))
        rejected = False
    except TychoraError as exc:
        rejected = exc.code == "INVALID_REQUEST"
    sender.send(
        {**counts, "rejected": rejected, "restored": Image.open is original_open}
    )
    sender.close()


@pytest.mark.qualification
@pytest.mark.parametrize("ceiling", [150_000, 80_000, 204_800])
def test_docx_pixel_limit_precedes_decode_and_survives_sdk_fallback(tmp_path, ceiling):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    source = Path(__file__).parents[1] / "fixtures/documents/rich.docx"
    child = context.Process(
        target=_docx_capacity_probe, args=(source, tmp_path / "staged", ceiling, sender)
    )
    try:
        child.start()
        sender.close()
        assert receiver.poll(40), "Capacity qualification process did not finish"
        result = receiver.recv()
        # SDK interpreter teardown can exceed two seconds after sending results.
        child.join(10)
        assert child.exitcode == 0
        assert result["restored"]
        assert result["sdk_returned"]
        if ceiling < 204_800:
            assert result["rejected"], result
            assert result["caught"] == 1
            assert result["load"] == result["save"] == 0
        else:
            assert not result["rejected"]
            assert result["caught"] == 0
            assert result["load"] and result["save"]
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()
        receiver.close()
        sender.close()


def _pdf_capacity_probe(source, staged, sender):
    from shutil import copyfile

    from docling.document_converter import DocumentConverter

    from tychora.adapters.docling import parser
    from tychora.contracts.errors import TychoraError

    parser.MAX_IMAGE_PIXELS = 1000
    entered = False

    def unexpected_conversion(*args, **kwargs):
        nonlocal entered
        entered = True
        raise RuntimeError("conversion sentinel")

    DocumentConverter.convert = unexpected_conversion
    staged.mkdir()
    original = staged / "0"
    copyfile(source, original)
    rejected = False
    try:
        parser._Convert("/unused-pdf-model-path", "pdf")((original,))
    except TychoraError as exc:
        rejected = exc.code == "INVALID_REQUEST"
    except RuntimeError as exc:
        assert str(exc) == "conversion sentinel"
    sender.send({"entered": entered, "rejected": rejected})
    sender.close()


@pytest.mark.qualification
@pytest.mark.parametrize("kind", ["page", "image", "nested_image", "boundary"])
def test_pdf_pixel_metadata_is_checked_before_conversion(tmp_path, kind):
    import multiprocessing

    from reportlab.pdfgen import canvas

    source = tmp_path / "source.pdf"
    size = (41, 25) if kind == "page" else (20, 50) if kind == "boundary" else (25, 25)
    pdf = canvas.Canvas(str(source), pagesize=size, invariant=1)
    if kind in ("image", "nested_image"):
        picture = Path(__file__).parents[1] / "fixtures/documents/rich-figure.png"
        if kind == "nested_image":
            pdf.beginForm("inner")
        pdf.drawImage(str(picture), 1, 1, width=20, height=10)
        if kind == "nested_image":
            pdf.endForm()
            pdf.beginForm("outer")
            pdf.doForm("inner")
            pdf.endForm()
            pdf.doForm("outer")
    pdf.showPage()
    pdf.save()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_pdf_capacity_probe, args=(source, tmp_path / "staged", sender)
    )
    try:
        child.start()
        sender.close()
        assert receiver.poll(40), "PDF capacity qualification process did not finish"
        result = receiver.recv()
        # SDK interpreter teardown can exceed two seconds after sending results.
        child.join(10)
        assert child.exitcode == 0
        assert result == (
            {"entered": True, "rejected": False}
            if kind == "boundary"
            else {"entered": False, "rejected": True}
        )
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()
        receiver.close()
        sender.close()
