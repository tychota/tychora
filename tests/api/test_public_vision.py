"""Selected vision through the configured public source workflow."""

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolResult
from PIL import Image, ImageDraw, ImageFont
from typer.testing import CliRunner

from tests.helpers.assets import asset_path
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.identity import source_id as canonical_source_id
from tychora.interfaces.cli import app as cli_app
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.interfaces.mcp.server import _current, build_mcp
from tychora.ports.execution import ExecutionContext

# Parser and OCR jobs are slow on loaded CI runners; the API caps one wait at 300 s.
JOB_WAIT = min(scaled(60), 300)


class Counter:
    model = "synthetic-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text):
        return len(text)


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_configured_public_add_uses_only_selected_crop_after_grant(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic
    from tychora.contracts.errors import TychoraError

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setenv("SYNTHETIC_VISION_KEY", "synthetic-key")
    picture = tmp_path / "diagram.png"
    Image.new("RGB", (8, 6), "blue").save(picture)
    original_picture = picture.read_bytes()
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
    fail_next = [False]

    class MockMessages:
        def create(self, **kwargs):
            content = kwargs["messages"][0]["content"]
            encoded = base64.b64decode(content[0]["source"]["data"])
            with Image.open(BytesIO(encoded)) as crop:
                sent.append((crop.width, crop.height, len(encoded), content[1]["text"]))
            if fail_next[0]:
                raise RuntimeError("synthetic selected-vision transport interruption")
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

    def create_with_mock(
        *,
        offline: bool,
        capabilities: frozenset[str],
        model: str,
        api_key: str,
        client_factory: Callable[..., object] | None = None,
    ):
        return actual_create(
            offline=offline,
            capabilities=capabilities,
            model=model,
            api_key=api_key,
            client_factory=lambda **_: MockClient(),
        )

    monkeypatch.setattr(
        "tychora.adapters.vision.anthropic.create_provider",
        create_with_mock,
    )
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        first = kb.sources.add(source)
        assert kb.jobs.wait(first.id, timeout=JOB_WAIT).state == "succeeded"
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
        captured_static_scope = kb.scope
        denied = kb.sources.add(selected)
        assert kb.jobs.wait(denied.id, timeout=JOB_WAIT).state == "failed"
        assert sent == []
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "anthropic",
                "action": "grant",
            }
        )
        # Managed future calls refresh immediately; a captured static grant stays capped.
        assert "anthropic" in kb.scope.upload_providers
        assert "anthropic" not in captured_static_scope.upload_providers
        from tychora.contracts.requests import SourceInput

        restricted_source = {**selected, "collection_id": str(kb.default_collection_id)}
        with pytest.raises(TychoraError) as restricted:
            kb._workspace.bridge.call(
                kb._workspace.services.sources.add(
                    SourceInput.from_mapping(restricted_source), captured_static_scope
                )
            )
        assert restricted.value.code == "FORBIDDEN_SCOPE"
        assert sent == []
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        fail_next[0] = True
        interrupted = kb.sources.add(selected)
        interrupted_status = kb.jobs.wait(interrupted.id, timeout=JOB_WAIT)
        assert interrupted_status.state == "succeeded", (interrupted_status, sent)
        failed_calls = len(sent)
        assert failed_calls >= 1
        assert (
            kb._workspace.bridge.call(
                kb._workspace.owner.run_write(
                    lambda: kb._workspace.owner.db.execute(
                        "SELECT count(*) FROM asset_proxies"
                    ).fetchone()[0],
                    ExecutionContext(),
                )
            )
            == 0
        )
    fail_next[0] = False
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        second = kb.sources.add(selected, idempotency_key="selected-once")
        status = kb.jobs.wait(second.id, timeout=JOB_WAIT)
        assert status.state == "succeeded", status
        assert len(sent) == failed_calls + 1
        assert kb.sources.add(selected, idempotency_key="selected-once").id == second.id
        assert len(sent) == failed_calls + 1

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
                assert isinstance(inspected, CallToolResult)
                assert not inspected.is_error
                assert inspected.structured_content is not None
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
                assert isinstance(accepted_tool, CallToolResult)
                assert not accepted_tool.is_error
                assert accepted_tool.structured_content is not None
                assert accepted_tool.structured_content["id"] == second.id
            finally:
                _current.reset(marker)

        asyncio.run(transport_parity())
        assert len(sent) == failed_calls + 1
        assert all(item[:2] == (4, 2) for item in sent)
        rows = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
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
        original_publish = kb._workspace.owner.store.publish_snapshot

        def interrupt_after_selected_cache(plan):
            monkeypatch.setattr(
                kb._workspace.owner.store, "publish_snapshot", original_publish
            )
            raise TychoraError(
                "PROVIDER_FAILURE",
                "Synthetic post-selection interruption",
                retryable=True,
            )

        monkeypatch.setattr(
            kb._workspace.owner.store,
            "publish_snapshot",
            interrupt_after_selected_cache,
        )
        variant = kb.sources.add(changed_schema, idempotency_key="selected-retry")
        failed_variant = kb.jobs.wait(variant.id, timeout=JOB_WAIT)
        assert failed_variant.state == "failed"
        assert failed_variant.error_code == "PROVIDER_FAILURE"
        assert len(sent) == failed_calls + 2

        async def retry_selected_job(job_id):
            app = create_app(
                kb._workspace.owner,
                kb._workspace.services,
                LocalAuth({"writer": kb.scope}, authority="127.0.0.1:8765"),
            )
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer writer"},
            ) as client:
                return await client.post(f"/v1/jobs/{job_id}/retry")

        retry_response = asyncio.run(retry_selected_job(variant.id))
        assert retry_response.status_code == 202, retry_response.text
        assert kb.jobs.wait(variant.id, timeout=JOB_WAIT).state == "succeeded"
        assert len(sent) == failed_calls + 2
        count = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM extractions"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 4  # Initial, capability-gap, recovered, changed-schema.
        synced = kb.sources.sync(source_identity)
        assert kb.jobs.wait(synced.id, timeout=JOB_WAIT).state == "succeeded"
        Image.new("RGB", (8, 6), "green").save(picture)
        stale = kb.sources.add(selected)
        stale_state = kb.jobs.wait(stale.id, timeout=JOB_WAIT)
        assert stale_state.state == "failed"
        assert stale_state.error_code == "INVALID_REQUEST"
        assert len(sent) == failed_calls + 2
        picture.write_bytes(original_picture)
        revoked_schema = {
            **changed_schema,
            "vision": [
                {
                    **changed_schema["vision"][0],
                    "task_schema": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "enum": ["blue", "green"]}
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
        original_selected = kb._workspace.services.sources._selected_vision_proxy

        async def interrupt_before_uncached_dispatch(*args):
            monkeypatch.setattr(
                kb._workspace.services.sources,
                "_selected_vision_proxy",
                original_selected,
            )
            raise TychoraError(
                "PROVIDER_FAILURE",
                "Synthetic pre-dispatch interruption",
                retryable=True,
            )

        monkeypatch.setattr(
            kb._workspace.services.sources,
            "_selected_vision_proxy",
            interrupt_before_uncached_dispatch,
        )
        pending = kb.sources.add(revoked_schema, idempotency_key="selected-revoke")
        pending_state = kb.jobs.wait(pending.id, timeout=JOB_WAIT)
        assert pending_state.state == "failed"
        assert pending_state.captured_inputs is not None
        assert len(sent) == failed_calls + 2
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "anthropic",
                "action": "revoke",
            }
        )
        denied_retry = asyncio.run(retry_selected_job(pending.id))
        assert denied_retry.status_code == 202, denied_retry.text
        retry_state = kb.jobs.wait(pending.id, timeout=JOB_WAIT)
        assert retry_state.state == "failed"
        assert retry_state.error_code in {"SCOPE_CHANGED", "FORBIDDEN_SCOPE"}
        assert len(sent) == failed_calls + 2
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        revoked = kb.sources.add(revoked_schema)
        assert kb.jobs.wait(revoked.id, timeout=JOB_WAIT).state == "failed"
        assert len(sent) == failed_calls + 2


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_offline_cli_selected_ocr_uses_local_models_without_remote_call(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
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
        assert kb.jobs.wait(original.id, timeout=JOB_WAIT).state == "succeeded"
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
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM asset_proxies"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 1
        proxy_id = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT proxy_unit_id FROM asset_proxies LIMIT 1"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        inspected = kb.evidence.get(proxy_id)
        assert inspected.evidence is not None
        assert inspected.evidence.text_origin == "ocr"
        assert inspected.evidence.review_state == "needs_review"
        assert not inspected.evidence.citable
        derivation = next(
            item
            for item in inspected.records
            if item.record_type == "vision_derivation"
        )
        assert "SOURCE IMAGE 123" in derivation.value["literal_transcription"]
        assert derivation.value["interpretation"] is None
        assert derivation.value["uncertainties"]


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_public_pdf_crop_binds_verified_native_context_and_rejects_invented_target(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic

    pytest.importorskip("reportlab", reason="PDF fixture requires optional reportlab")
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    monkeypatch.setenv("SYNTHETIC_VISION_KEY", "synthetic-key")
    pdf = tmp_path / "mixed.pdf"
    page = canvas.Canvas(str(pdf), pagesize=(612, 792), invariant=1)
    page.setFont("Helvetica", 15)
    page.drawString(72, 700, "Native source paragraph remains authoritative.")
    page.showPage()
    for index in range(21):
        page.drawString(72, 700, f"Additional native page {index:02d}.")
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

    def create_with_mock(
        *,
        offline: bool,
        capabilities: frozenset[str],
        model: str,
        api_key: str,
        client_factory: Callable[..., object] | None = None,
    ):
        return actual_create(
            offline=offline,
            capabilities=capabilities,
            model=model,
            api_key=api_key,
            client_factory=lambda **_: MockClient(),
        )

    monkeypatch.setattr(
        anthropic,
        "create_provider",
        create_with_mock,
    )

    def wait_for_pdf_job(kb, job_id, expected):
        status = kb.jobs.wait(job_id, timeout=180)
        assert status.state == expected, (status.state, status.error_code)
        return status

    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        first = kb.sources.add(source)
        wait_for_pdf_job(kb, first.id, "succeeded")
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
    (workspace / "workspace.toml").write_text(config_path.read_text())
    command = [
        "--workspace",
        str(workspace),
        "--profile",
        "test",
        "inspect",
        "selections",
        f"{sid}:{revision}:{extraction}",
        "--limit",
        "20",
        "--json",
    ]
    first_page = CliRunner().invoke(cli_app, command)
    assert first_page.exit_code == 0, first_page.stdout + first_page.stderr
    first_candidates = json.loads(first_page.stdout)["candidates"]
    assert len(first_candidates) == 20 and json.loads(first_page.stdout)["next_cursor"]
    continued = CliRunner().invoke(cli_app, [*command[:-1], "--page", "2", "--json"])
    assert continued.exit_code == 0, continued.stdout + continued.stderr
    page_data = json.loads(continued.stdout)
    later_image = next(
        (item for item in page_data["candidates"] if item["kind"] == "image"), None
    )
    assert later_image is not None
    assert later_image["source_native_ref"] not in {
        item["source_native_ref"] for item in first_candidates
    }
    selected["vision"][0]["source_native_ref"] = later_image["source_native_ref"]
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        accepted = kb.sources.add(selected)
        wait_for_pdf_job(kb, accepted.id, "succeeded")
        assert len(prompts) == 1
        proxy_id = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT proxy_unit_id FROM asset_proxies ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        inspected = kb.evidence.get(proxy_id)
        assert inspected.evidence is not None
        assert inspected.evidence.text_origin == "derived"
        assert inspected.evidence.review_state == "needs_review"
        assert not inspected.evidence.citable
        derivation = next(
            item
            for item in inspected.records
            if item.record_type == "vision_derivation"
        )
        assert derivation.value["literal_transcription"] is None
        assert dict(derivation.value["interpretation"]) == {"label": "selected"}
        assert derivation.value["uncertainties"] == ("synthetic",)
        assert derivation.value["target_id"] == prompts[0][1][0]["target_id"]
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
        wait_for_pdf_job(kb, bad.id, "succeeded")
        assert len(prompts) == 2
        response_mode[0] = "malformed"
        malformed = {
            **selected,
            "vision": [{**selected["vision"][0], "task": "extract"}],
        }
        bad_json = kb.sources.add(malformed)
        wait_for_pdf_job(kb, bad_json.id, "succeeded")
        assert len(prompts) == 3
        assert (
            sum(
                item.stages.get("vision") == "incomplete"
                for item in kb.sources.extractions(sid, revision).extractions
            )
            == 2
        )
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
        wait_for_pdf_job(kb, refused.id, "failed")
        assert len(prompts) == 3
        count = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT count(*) FROM asset_proxies"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert count == 1


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_first_selected_add_retains_original_and_reports_incomplete_vision(
    tmp_path, monkeypatch
):
    from tychora.adapters.vision import anthropic
    from tychora.contracts.artifacts import ArtifactRef
    from tychora.contracts.composition import VisionCapabilityDiagnostics

    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    monkeypatch.setenv("SYNTHETIC_VISION_KEY", "synthetic-key")
    image_path = tmp_path / "first.png"
    Image.new("RGB", (8, 6), "blue").save(image_path)
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
        "path": str(image_path),
        "format": "png",
        "source_key": "first",
        "collection_id": "default",
        "kind": "manual@1",
        "metadata": {"title": "First", "publisher": "Example Team", "edition": "1"},
    }
    selected = {
        **source,
        "vision": [
            {
                "captured_original_sha256": hashlib.sha256(
                    image_path.read_bytes()
                ).hexdigest(),
                "source_native_ref": "image-original:first",
                "geometry": {"domain": "pixels", "x0": 1, "y0": 1, "x1": 5, "y1": 4},
                "task": "describe",
                "task_schema": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                    "additionalProperties": False,
                },
            }
        ],
    }
    sent = []
    response_mode = ["malformed"]

    class MockMessages:
        def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text="not-json"
                        if response_mode[0] == "malformed"
                        else json.dumps(
                            {
                                "target_id": None,
                                "literal_transcription": None,
                                "interpretation": {"label": "blue"},
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

    def create_with_mock(
        *,
        offline: bool,
        capabilities: frozenset[str],
        model: str,
        api_key: str,
        client_factory: Callable[..., object] | None = None,
    ):
        return actual_create(
            offline=offline,
            capabilities=capabilities,
            model=model,
            api_key=api_key,
            client_factory=lambda **_: MockClient(),
        )

    monkeypatch.setattr(
        anthropic,
        "create_provider",
        create_with_mock,
    )
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
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
        assert (
            kb.jobs.wait(accepted.id, timeout=min(scaled(30), 300)).state == "succeeded"
        )
        assert len(sent) == 1
        sid = canonical_source_id(UUID(str(kb.default_collection_id)), "first")
        revision = kb.sources.revisions(sid).revisions[0].revision_id
        extraction = kb.sources.extractions(sid, revision).extractions[0]
        assert extraction.stages["vision"] == "incomplete"
        original = kb.inspect(
            {"subject": "revisions", "id": revision, "original": True}
        )
        assert original.payload == image_path.read_bytes()
        assert kb.sources.selections(sid, revision, extraction.extraction_id).candidates
        assert (
            kb._workspace.bridge.call(
                kb._workspace.owner.run_write(
                    lambda: kb._workspace.owner.db.execute(
                        "SELECT count(*) FROM asset_proxies"
                    ).fetchone()[0],
                    ExecutionContext(),
                )
            )
            == 0
        )
        omitted_again = kb.sources.add(selected)
        assert kb.jobs.wait(omitted_again.id, timeout=JOB_WAIT).state == "failed"
        assert len(sent) == 1
        inspected = kb.sources.selections(sid, revision, extraction.extraction_id)
        response_mode[0] = "valid"
        retry_intent = {
            **selected,
            "vision": [
                {
                    **selected["vision"][0],
                    "retained_extraction_sha256": inspected.retained_extraction_sha256,
                }
            ],
        }
        completed = kb.sources.add(retry_intent)
        assert kb.jobs.wait(completed.id, timeout=JOB_WAIT).state == "succeeded"
        assert len(sent) == 2
        outcomes = kb.sources.extractions(sid, revision).extractions
        assert len(outcomes) == 2
        assert {item.stages.get("vision") for item in outcomes} == {None, "incomplete"}
    with Tychora.open(workspace, config=config, token_counter=Counter()) as reopened:
        retained = next(
            item
            for item in reopened.sources.extractions(sid, revision).extractions
            if item.extraction_id == extraction.extraction_id
        )
        assert retained.stages["vision"] == "incomplete"
        assert (
            reopened.inspect(
                {"subject": "revisions", "id": revision, "original": True}
            ).payload
            == image_path.read_bytes()
        )

        def diagnostic_bytes():
            row = reopened._workspace.owner.db.execute(
                "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM extractions e JOIN artifacts a "
                "ON a.artifact_sha=e.diagnostics_artifact_sha "
                "WHERE e.extraction_id=?",
                (extraction.extraction_id,),
            ).fetchone()
            assert row is not None
            ref = ArtifactRef(
                sha256=row[0],
                size_bytes=row[1],
                media_type=row[2],
                storage_key=row[3],
            )
            with reopened._workspace.owner.artifacts.open_verified(ref) as stream:
                return stream.read()

        payload = reopened._workspace.bridge.call(
            reopened._workspace.owner.run_write(diagnostic_bytes, ExecutionContext())
        )
        diagnostic = VisionCapabilityDiagnostics.model_validate_json(payload)
        assert len(diagnostic.gaps) == 1
        assert diagnostic.gaps[0].reason == "provider_failure"
        assert len(sent) == 2
