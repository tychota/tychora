"""Selected vision through the configured public source workflow."""

import asyncio
import base64
import hashlib
import json
import os
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from typer.testing import CliRunner

from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.identity import source_id as canonical_source_id
from tychora.interfaces.cli import app as cli_app
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.interfaces.mcp.server import _current, build_mcp
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text):
        return len(text)


def test_configured_public_add_uses_only_selected_crop_after_grant(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic

    cache = os.environ["TYCHORA_TEST_MODEL_CACHE"]
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setenv("SYNTHETIC_VISION_KEY", "synthetic-key")
    picture = tmp_path / "diagram.png"
    Image.new("RGB", (8, 6), "blue").save(picture)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.extraction]\nparser="docling"\n'
        '[configuration.acquisition]\nroots=["' + str(tmp_path) + '"]\n'
        '[configuration.providers.vision]\nprovider="anthropic"\n'
        'model="synthetic-qualified-model"\ncredential_ref="env:SYNTHETIC_VISION_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    workspace = tmp_path / "knowledge"
    source = {
        "type": "file",
        "path": str(picture),
        "format": "png",
        "source_key": "diagram",
        "collection_id": "default",
        "kind": "manual@1",
        "metadata": {"title": "Diagram", "publisher": "Example Team", "edition": "1"},
    }
    sent = []

    class MockMessages:
        def create(self, **kwargs):
            content = kwargs["messages"][0]["content"]
            encoded = base64.b64decode(content[0]["source"]["data"])
            with Image.open(BytesIO(encoded)) as crop:
                sent.append((crop.width, crop.height, len(encoded), content[1]["text"]))
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"target_id":null,"literal_transcription":null,"interpretation":{"label":"blue"},"uncertainties":["synthetic"]}',
                    )
                ],
            )

    class MockClient:
        messages = MockMessages()

        def close(self):
            pass

    actual_create = anthropic.create_provider
    monkeypatch.setattr(
        "tychora.adapters.vision.anthropic.create_provider",
        lambda **kwargs: actual_create(
            **{**kwargs, "client_factory": lambda **client_kwargs: MockClient()}
        ),
    )
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        first = kb.sources.add(source)
        assert kb.jobs.wait(first.id).state == "succeeded"
        source_identity = canonical_source_id(
            UUID(str(kb.default_collection_id)), "diagram"
        )
        revisions = kb.sources.revisions(source_identity)
        revision = revisions.revisions[0].revision_id
        extraction = (
            kb.sources.extractions(source_identity, revision)
            .extractions[0]
            .extraction_id
        )
        selections = kb.sources.selections(source_identity, revision, extraction)
        image_choice = next(
            choice for choice in selections.candidates if choice.kind == "image"
        )
        assert image_choice.source_native_ref == "image-original:diagram"
        assert (image_choice.width, image_choice.height) == (8, 6)
        assert (
            selections.captured_original_sha256
            == hashlib.sha256(picture.read_bytes()).hexdigest()
        )
        selected = {
            **source,
            "vision": [
                {
                    "captured_original_sha256": selections.captured_original_sha256,
                    "retained_extraction_sha256": selections.retained_extraction_sha256,
                    "source_native_ref": image_choice.source_native_ref,
                    "task": "describe",
                    "geometry": {
                        "domain": "pixels",
                        "x0": 1,
                        "y0": 2,
                        "x1": 5,
                        "y1": 4,
                    },
                    "task_schema": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
        denied = kb.sources.add(selected)
        assert kb.jobs.wait(denied.id).state == "failed"
        assert sent == []
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "anthropic",
                "action": "grant",
            }
        )
        still_denied = kb.sources.add(selected)
        assert kb.jobs.wait(still_denied.id).state == "failed"
        assert sent == []
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        second = kb.sources.add(selected, idempotency_key="selected-once")
        status = kb.jobs.wait(second.id)
        assert status.state == "succeeded", status
        assert len(sent) == 1
        assert kb.sources.add(selected, idempotency_key="selected-once").id == second.id
        assert len(sent) == 1

        async def transport_parity():
            app = create_app(
                kb._workspace.owner,
                kb._workspace.services,
                LocalAuth(
                    {"synthetic-token": kb._workspace.scope}, authority="127.0.0.1:8765"
                ),
            )
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer synthetic-token"},
            ) as client:
                path = f"/v1/sources/{source_identity}/revisions/{revision}/extractions/{extraction}/selections"
                listed = await client.get(path)
                assert listed.status_code == 200
                assert (
                    listed.json()["captured_original_sha256"]
                    == selections.captured_original_sha256
                )
                accepted = await client.post(
                    "/v1/sources",
                    json=selected,
                    headers={"Idempotency-Key": "selected-once"},
                )
                assert accepted.status_code == 202
                assert accepted.json()["id"] == second.id
            server = build_mcp(kb._workspace.services, kb._workspace.scope)
            marker = _current.set((kb._workspace.services, kb._workspace.scope))
            try:
                inspected = await server.call_tool(
                    "inspect",
                    {
                        "request": {
                            "subject": "source_selections",
                            "source_id": source_identity,
                            "revision_id": revision,
                            "extraction_id": extraction,
                            "limit": 20,
                        }
                    },
                )
                assert not inspected.is_error
                assert (
                    inspected.structured_content["result"]["captured_original_sha256"]
                    == selections.captured_original_sha256
                )
                accepted_tool = await server.call_tool(
                    "add_sources",
                    {
                        "action": "add",
                        "source": selected,
                        "idempotency_key": "selected-once",
                    },
                )
                assert not accepted_tool.is_error
                assert accepted_tool.structured_content["id"] == second.id
            finally:
                _current.reset(marker)

        asyncio.run(transport_parity())
        assert len(sent) == 1
        assert sent[0][:2] == (4, 2)
        rows = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM asset_proxies"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert rows == 1
        changed_schema = {
            **selected,
            "vision": [
                {
                    **selected["vision"][0],
                    "task_schema": {
                        "type": "object",
                        "properties": {"label": {"type": "string", "enum": ["blue"]}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
        variant = kb.sources.add(changed_schema)
        assert kb.jobs.wait(variant.id).state == "succeeded"
        assert len(sent) == 2
        count = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM extractions"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 3
        synced = kb.sources.sync(source_identity)
        assert kb.jobs.wait(synced.id).state == "succeeded"
        Image.new("RGB", (8, 6), "green").save(picture)
        stale = kb.sources.add(selected)
        assert kb.jobs.wait(stale.id).state == "failed"
        assert len(sent) == 2


def test_offline_cli_selected_ocr_uses_local_models_without_remote_call(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic

    cache = os.environ["TYCHORA_TEST_MODEL_CACHE"]
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setattr(
        anthropic,
        "create_provider",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("remote client constructed")
        ),
    )
    picture = tmp_path / "ocr-image.png"
    image = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(image).text(
        (35, 55), "SOURCE IMAGE 123", fill="black", font=ImageFont.load_default(size=42)
    )
    image.save(picture)
    workspace = tmp_path / "offline-knowledge"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="local"\n'
        '[configuration.extraction]\nparser="docling"\n'
        '[configuration.acquisition]\nroots=["' + str(tmp_path) + '"]\n'
    )
    source = {
        "type": "file",
        "path": str(picture),
        "format": "png",
        "media_type": "image/png",
        "source_key": "ocr-image.png",
        "collection_id": "default",
        "kind": "manual@1",
        "metadata": {"title": "OCR image", "publisher": "Example Team", "edition": "1"},
    }
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        original = kb.sources.add(source)
        assert kb.jobs.wait(original.id).state == "succeeded"
        sid = canonical_source_id(
            UUID(str(kb.default_collection_id)), source["source_key"]
        )
        revision = kb.sources.revisions(sid).revisions[0].revision_id
        extraction = kb.sources.extractions(sid, revision).extractions[0].extraction_id
        options = kb.sources.selections(sid, revision, extraction)
        choice = next(item for item in options.candidates if item.kind == "image")
    request = tmp_path / "selected-ocr.json"
    request.write_text(
        json.dumps(
            {
                **source,
                "vision": [
                    {
                        "captured_original_sha256": options.captured_original_sha256,
                        "retained_extraction_sha256": options.retained_extraction_sha256,
                        "source_native_ref": choice.source_native_ref,
                        "geometry": {
                            "domain": "pixels",
                            "x0": 0,
                            "y0": 0,
                            "x1": choice.width,
                            "y1": choice.height,
                        },
                        "task": "ocr",
                        "task_schema": {"type": "null"},
                    }
                ],
            }
        )
    )
    result = CliRunner().invoke(
        cli_app,
        [
            "--workspace",
            str(workspace),
            "add",
            "--request-file",
            str(request),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["state"] == "succeeded"
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        count = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM asset_proxies"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 1


def test_public_pdf_crop_binds_verified_native_context_and_rejects_invented_target(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic

    monkeypatch.setenv("TYCHORA_MODEL_CACHE", os.environ["TYCHORA_TEST_MODEL_CACHE"])
    monkeypatch.setenv("SYNTHETIC_VISION_KEY", "synthetic-key")
    pdf = tmp_path / "mixed.pdf"
    page = canvas.Canvas(str(pdf), pagesize=(612, 792), invariant=1)
    page.setFont("Helvetica", 15)
    page.drawString(72, 700, "Native source paragraph remains authoritative.")
    page.showPage()
    pixels = Image.new("RGB", (600, 140), "white")
    ImageDraw.Draw(pixels).rectangle((50, 30, 170, 100), fill="blue")
    encoded = BytesIO()
    pixels.save(encoded, format="PNG")
    page.drawImage(
        ImageReader(BytesIO(encoded.getvalue())), 36, 300, width=540, height=126
    )
    page.showPage()
    page.save()
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.extraction]\nparser="docling"\n'
        '[configuration.acquisition]\nroots=["' + str(tmp_path) + '"]\n'
        '[configuration.providers.vision]\nprovider="anthropic"\n'
        'model="synthetic-qualified-model"\ncredential_ref="env:SYNTHETIC_VISION_KEY"\n'
    )
    config = resolve_config(config_path, "test", {})
    source = {
        "type": "file",
        "path": str(pdf),
        "format": "pdf",
        "source_key": "mixed",
        "collection_id": "default",
        "kind": "manual@1",
        "metadata": {"title": "Mixed", "publisher": "Example Team", "edition": "1"},
    }
    prompts = []
    response_mode = ["valid"]

    class MockMessages:
        def create(self, **kwargs):
            content = kwargs["messages"][0]["content"]
            with Image.open(
                BytesIO(base64.b64decode(content[0]["source"]["data"]))
            ) as crop:
                crop_size = crop.size
            candidates = json.loads(
                content[1]["text"].split("Candidate context: ", 1)[1]
            )
            prompts.append((crop_size, candidates))
            assert crop_size == (40, 30)
            assert len(candidates) == 1
            assert candidates[0]["literal_text"] == "Native source"
            target = (
                "invented-target"
                if response_mode[0] == "invented"
                else candidates[0]["target_id"]
            )
            if response_mode[0] == "malformed":
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(type="text", text="not-json")],
                )
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "target_id": target,
                                "literal_transcription": None,
                                "interpretation": {"label": "selected"},
                                "uncertainties": ["synthetic"],
                            }
                        ),
                    )
                ],
            )

    class MockClient:
        messages = MockMessages()

        def close(self):
            pass

    actual_create = anthropic.create_provider
    monkeypatch.setattr(
        anthropic,
        "create_provider",
        lambda **kwargs: actual_create(
            **{**kwargs, "client_factory": lambda **_: MockClient()}
        ),
    )
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        first = kb.sources.add(source)
        assert kb.jobs.wait(first.id).state == "succeeded"
        sid = canonical_source_id(UUID(str(kb.default_collection_id)), "mixed")
        revision = kb.sources.revisions(sid).revisions[0].revision_id
        extraction = kb.sources.extractions(sid, revision).extractions[0].extraction_id
        options = kb.sources.selections(sid, revision, extraction, limit=50)
        image = next(
            item
            for item in options.candidates
            if item.kind == "image" and item.width >= 40
        )
        context = next(
            item
            for item in options.candidates
            if item.kind == "context"
            and item.field == "text"
            and item.preview
            and "Native source paragraph" in item.preview
        )
        selected = {
            **source,
            "vision": [
                {
                    "captured_original_sha256": options.captured_original_sha256,
                    "retained_extraction_sha256": options.retained_extraction_sha256,
                    "source_native_ref": image.source_native_ref,
                    "geometry": {
                        "domain": "pixels",
                        "x0": 0,
                        "y0": 0,
                        "x1": 40,
                        "y1": 30,
                    },
                    "task": "describe",
                    "task_schema": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                    "context": [
                        {
                            "native_ref": context.source_native_ref,
                            "field": "text",
                            "source_start": 0,
                            "source_end": 13,
                        }
                    ],
                }
            ],
        }
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "anthropic",
                "action": "grant",
            }
        )
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        accepted = kb.sources.add(selected)
        status = kb.jobs.wait(accepted.id)
        assert status.state == "succeeded", (status, prompts)
        assert len(prompts) == 1
        response_mode[0] = "invented"
        altered = {
            **selected,
            "vision": [
                {
                    **selected["vision"][0],
                    "task_schema": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "enum": ["selected"]}
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
        bad = kb.sources.add(altered)
        assert kb.jobs.wait(bad.id).state == "failed"
        assert len(prompts) == 2
        response_mode[0] = "malformed"
        malformed = {
            **selected,
            "vision": [{**selected["vision"][0], "task": "extract"}],
        }
        bad_json = kb.sources.add(malformed)
        assert kb.jobs.wait(bad_json.id).state == "failed"
        assert len(prompts) == 3
        stale_context = {
            **selected,
            "vision": [
                {
                    **selected["vision"][0],
                    "context": [
                        {
                            **selected["vision"][0]["context"][0],
                            "native_ref": "#/invented-context",
                        }
                    ],
                }
            ],
        }
        refused = kb.sources.add(stale_context)
        assert kb.jobs.wait(refused.id).state == "failed"
        assert len(prompts) == 3
        count = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM asset_proxies"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 1
