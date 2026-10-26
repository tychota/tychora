"""Synthetic PDF intake through the real authenticated ASGI boundary."""

import hashlib
import io
import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


def make_pdf(tmp_path, scanned=False, blank=False):
    stream = io.BytesIO()
    pdf = Canvas(stream, pagesize=(200, 200))
    if scanned:
        raster = Image.new("RGB", (100, 100), "white")
        for x in range(10, 90):
            for y in range(40, 55):
                raster.putpixel((x, y), (0, 0, 0))
        pdf.drawImage(ImageReader(raster), 10, 10, 180, 180)
    elif not blank:
        pdf.drawString(10, 100, "Synthetic local document")
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def descriptor(kb, size):
    return {
        "source_key": "synthetic.pdf",
        "collection_id": str(kb.default_collection_id),
        "kind": "manual@1",
        "expected_size_bytes": size,
        "metadata": {"title": "Synthetic", "publisher": "Example", "edition": "1"},
    }


def app_for(kb):
    reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
    return create_app(
        kb._workspace.owner,
        kb._workspace.services,
        LocalAuth(
            {"writer": kb.scope, "rotated": kb.scope, "reader": reader},
            authority="127.0.0.1:8765",
        ),
    )


def client_for(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer writer"},
    )


def test_pdf_contracts_import_without_cycles():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tychora.contracts.requests; import tychora.contracts.pdf_policy; import tychora.contracts.pdf_intake; import tychora.application.services",
        ],
        capture_output=True,
        check=True,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.asyncio
async def test_binary_intake_preserves_json_gate(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            rejected = await client.post("/v1/sources", content=b" " * (1048576 + 1))
            assert rejected.status_code == 400
            data = make_pdf(tmp_path)
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            assert prepared.status_code == 201, prepared.text
            receipt = prepared.json()
            assert receipt["state"] == "prepared"
            assert "path" not in receipt
            uploaded = await client.put(
                f"/v1/pdf-intakes/{receipt['intake_id']}/content", content=data
            )
            assert uploaded.status_code == 200, uploaded.text
            assert uploaded.json()["state"] == "staged"
            assert uploaded.json()["received_bytes"] == len(data)
            again = await client.put(
                f"/v1/pdf-intakes/{receipt['intake_id']}/content", content=data
            )
            assert again.status_code == 409
            assert uploaded.headers["cache-control"] == "no-store"


async def raw_request(app, path, headers, receive):
    messages = []

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 1),
        },
        receive,
        send,
    )
    return messages[0]["status"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        [(b"authorization", b"Bearer bad")],
        [(b"authorization", b"Bearer reader")],
        [(b"origin", b"http://hostile")],
        [(b"host", b"hostile:8765")],
        [(b"content-length", b"104857601")],
        [(b"content-length", b"1"), (b"content-length", b"2")],
        [(b"content-length", b"1"), (b"content-length", b"1")],
    ],
)
async def test_binary_denied_before_receive(tmp_path, extra):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            prepared = await client.post("/v1/pdf-intakes", json=descriptor(kb, 2048))
            assert prepared.status_code == 201, prepared.text
            path = f"/v1/pdf-intakes/{prepared.json()['intake_id']}/content"
            headers = []
            if not any(k == b"host" for k, _ in extra):
                headers.append((b"host", b"127.0.0.1:8765"))
            if not any(k == b"authorization" for k, _ in extra):
                headers.append((b"authorization", b"Bearer writer"))

            async def forbidden_read():
                pytest.fail("denied admission read the body")

            assert await raw_request(app, path, headers + extra, forbidden_read) in {
                400,
                403,
                413,
            }


@pytest.mark.asyncio
async def test_analysis_is_local_all_page_and_does_not_mutate_configuration(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        original_config = kb._workspace.owner.config.config_sha
        monkeypatch.setattr(kb._workspace.services.sources, "docling_models", None)
        async with client_for(app) as client:
            data = make_pdf(tmp_path, scanned=True)
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            path = f"/v1/pdf-intakes/{prepared.json()['intake_id']}"
            await client.put(path + "/content", content=data)
            response = await client.post(path + "/analyse")
            assert response.status_code == 200, response.text
            analysis = response.json()["analysis"]
            assert analysis["page_count"] == 1
            assert analysis["image_only_pages"] == [1]
            assert analysis["requires_ocr"] is True
            assert analysis["original_sha256"] == hashlib.sha256(data).hexdigest()
            assert response.json()["job"] is None
            assert analysis["blockers"]  # Explicitly unavailable verified local bundle.
            assert kb._workspace.owner.config.config_sha == original_config
            denied = await client.get(path, headers={"Authorization": "Bearer reader"})
            assert denied.status_code == 403
            rotated = await client.get(
                path, headers={"Authorization": "Bearer rotated"}
            )
            assert rotated.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual_delta,declared_delta", [(1, None), (-1, None), (0, -1)]
)
async def test_streamed_actual_reserved_and_declared_lengths(
    tmp_path, actual_delta, declared_delta
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            prepared = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            path = f"/v1/pdf-intakes/{prepared.json()['intake_id']}"

            async def chunks():
                yield b"x" * (128 + actual_delta)

            headers = (
                {}
                if declared_delta is None
                else {"Content-Length": str(128 + declared_delta)}
            )
            result = await client.put(
                path + "/content", content=chunks(), headers=headers
            )
            assert result.status_code in {400, 413}
            inspected = await client.get(path)
            assert inspected.json()["state"] == "aborted"
            assert not (
                app.state.pdf_intakes.directory
                / (prepared.json()["intake_id"] + ".pdf")
            ).exists()


@pytest.mark.asyncio
async def test_receipt_inode_replacement_and_expiry_fail_without_census(tmp_path):
    import os
    import time

    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            receipt = prepared.json()["intake_id"]
            path = f"/v1/pdf-intakes/{receipt}"
            await client.put(path + "/content", content=data)
            replacement = tmp_path / "replacement.pdf"
            replacement.write_bytes(data)
            os.replace(
                replacement, app.state.pdf_intakes.directory / (receipt + ".pdf")
            )
            response = await client.post(path + "/analyse")
            assert response.status_code == 409
            assert response.json()["code"] == "INTEGRITY_ERROR"
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            receipt = prepared.json()["intake_id"]

            def expire():
                record = app.state.pdf_intakes._read(receipt)
                record["expires"] = time.time() - 1
                app.state.pdf_intakes._write(record)

            await kb._workspace.owner.run_write(expire, ExecutionContext())
            response = await client.get(f"/v1/pdf-intakes/{receipt}")
            assert response.json()["state"] == "expired"
            assert response.json()["analysis"] is None


@pytest.mark.asyncio
async def test_reservations_abort_and_cancelled_receive_release_capacity(tmp_path):
    import asyncio

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            paths = []
            for _ in range(4):
                response = await client.post(
                    "/v1/pdf-intakes", json=descriptor(kb, 128)
                )
                assert response.status_code == 201
                paths.append("/v1/pdf-intakes/" + response.json()["intake_id"])
            rejected = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            assert rejected.status_code == 413

            async def disconnected():
                yield b"first chunk"
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                await client.put(paths[0] + "/content", content=disconnected())
            assert (await client.get(paths[0])).json()["state"] == "aborted"
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            assert response.status_code == 201
            assert (await client.delete(paths[1])).json()["state"] == "aborted"
            assert not list(app.state.pdf_intakes.directory.glob("*.pdf"))


@pytest.mark.asyncio
async def test_exact_configured_cap_and_missing_content_length(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        config = owner.config
        limits = config.values.acquisition.limits.model_copy(
            update={"max_file_bytes": 128, "max_total_bytes": 128}
        )
        acquisition = config.values.acquisition.model_copy(update={"limits": limits})
        owner.config = config.model_copy(
            update={
                "values": config.values.model_copy(update={"acquisition": acquisition})
            }
        )
        app = app_for(kb)
        async with client_for(app) as client:
            rejected = await client.post("/v1/pdf-intakes", json=descriptor(kb, 129))
            assert rejected.status_code == 413
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            path = "/v1/pdf-intakes/" + response.json()["intake_id"]

            async def chunks():
                yield b"a" * 64
                yield b"b" * 64

            response = await client.put(path + "/content", content=chunks())
            assert response.status_code == 200
            assert response.json()["received_bytes"] == 128


@pytest.mark.asyncio
async def test_restart_aborts_incomplete_receive(tmp_path):
    from tychora.ports.execution import ExecutionContext

    workspace = tmp_path / "workspace"
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            receipt = response.json()["intake_id"]

            def interrupted():
                service = app.state.pdf_intakes
                record = service._read(receipt)
                record["state"] = "receiving"
                service._write(record)
                (service.directory / (receipt + ".pdf")).write_bytes(b"partial")

            await kb._workspace.owner.run_write(interrupted, ExecutionContext())
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            response = await client.get("/v1/pdf-intakes/" + receipt)
            assert response.json()["state"] == "aborted"
            assert not list(app.state.pdf_intakes.directory.glob("*.pdf"))


@pytest.mark.asyncio
async def test_cancellation_at_receive_handoff_closes_staging(tmp_path, monkeypatch):
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_intake import PdfIntakeDescriptor
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        service = app_for(kb).state.pdf_intakes
        status = await service.prepare(
            PdfIntakeDescriptor.model_validate_json(
                canonical_bytes(descriptor(kb, 128))
            ),
            kb.scope,
            ExecutionContext(),
        )
        ctx = ExecutionContext()
        write = service._write

        def cancel_after_open(record):
            write(record)
            if record["state"] == "receiving":
                ctx.cancelled.set()

        monkeypatch.setattr(service, "_write", cancel_after_open)

        async def chunks():
            yield b"x" * 128

        with pytest.raises(TychoraError):
            await service.receive(status.intake_id, chunks(), kb.scope, ctx)
        inspected = await service.inspect(
            status.intake_id, kb.scope, ExecutionContext()
        )
        assert inspected.state == "aborted"
        assert not list(service.directory.glob("*.pdf"))


@pytest.mark.asyncio
async def test_direct_intake_calls_share_activation_gate(tmp_path):
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_intake import PdfIntakeDescriptor
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        runtime = app.state.runtime
        runtime.admit(activation=True)
        try:
            with pytest.raises(TychoraError) as error:
                await app.state.pdf_intakes.prepare(
                    PdfIntakeDescriptor.model_validate_json(
                        canonical_bytes(descriptor(kb, 128))
                    ),
                    kb.scope,
                    ExecutionContext(),
                )
            assert error.value.code == "WORKSPACE_BUSY"
        finally:
            runtime.release(activation=True)


@pytest.mark.asyncio
async def test_invalid_credential_precedes_binary_length_admission(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)

        async def no_read():
            pytest.fail("unauthenticated body was read")

        status = await raw_request(
            app,
            "/v1/pdf-intakes/" + "0" * 64 + "/content",
            [
                (b"host", b"127.0.0.1:8765"),
                (b"authorization", b"Bearer wrong"),
                (b"content-length", b"104857601"),
            ],
            no_read,
        )
        assert status == 403


@pytest.mark.asyncio
async def test_new_reservations_do_not_read_completed_receipt_history(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            old_id = response.json()["intake_id"]
            await client.delete("/v1/pdf-intakes/" + old_id)
            read = app.state.pdf_intakes._read

            def bounded_read(intake_id):
                assert intake_id != old_id, (
                    "new reservation read terminal receipt history"
                )
                return read(intake_id)

            monkeypatch.setattr(app.state.pdf_intakes, "_read", bounded_read)
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            assert response.status_code == 201


@pytest.mark.asyncio
async def test_direct_duplicate_receive_preserves_completed_original(tmp_path):
    from tychora.contracts.errors import TychoraError
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        service = app.state.pdf_intakes
        async with client_for(app) as client:
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
            receipt = response.json()["intake_id"]
            await client.put(
                "/v1/pdf-intakes/" + receipt + "/content", content=b"a" * 128
            )

            async def repeated():
                pytest.fail("duplicate receive consumed new bytes")
                yield b""

            with pytest.raises(TychoraError):
                await service.receive(receipt, repeated(), kb.scope, ExecutionContext())
            assert (
                await service.inspect(receipt, kb.scope, ExecutionContext())
            ).state == "staged"


@pytest.mark.asyncio
async def test_owner_processing_budget_also_bounds_original_reservations(tmp_path):
    from tychora.application.pdf_intake import PdfIntakeService

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        PdfIntakeService(
            kb._workspace.owner, kb._workspace.services, max_retained_bytes=128
        )
        async with client_for(app_for(kb)) as client:
            response = await client.post("/v1/pdf-intakes", json=descriptor(kb, 129))
            assert response.status_code == 413


@pytest.mark.asyncio
async def test_restart_with_unknown_analysis_copy_blocks_new_admission(tmp_path):
    workspace = tmp_path / "workspace"
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            prepared = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            path = "/v1/pdf-intakes/" + prepared.json()["intake_id"]
            await client.put(path + "/content", content=data)
            orphan = app.state.pdf_intakes.directory / ".analysis-synthetic-orphan"
            orphan.mkdir()
            (orphan / "original.pdf").write_bytes(data)
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            assert (await client.get(path)).json()["state"] == "staged"
            response = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "WORKSPACE_BUSY"
            assert (await client.post(path + "/analyse")).status_code == 409
            assert orphan.is_dir()


@pytest.mark.asyncio
async def test_unsupported_content_patch_uses_ordinary_body_limit(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            # A nonexistent receipt must not be inspected before the ordinary
            # one-MiB boundary rejects this unsupported binary method.
            response = await client.patch(
                "/v1/pdf-intakes/" + "0" * 64 + "/content",
                content=b" " * (1048576 + 1),
            )
            assert response.status_code == 400
            assert response.json()["code"] == "BUDGET_TOO_SMALL"
