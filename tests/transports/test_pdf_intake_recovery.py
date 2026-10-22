"""Receipt expiry, partial handoff recovery, and authorization boundaries."""

import asyncio
import time

import pytest

from tests.helpers.timing import scaled
from tests.jobs.test_acquired_sources import synthetic_policy_resolver
from tests.transports.test_http import Counter
from tests.transports.test_pdf_intake import app_for, client_for, descriptor, make_pdf
from tychora import Tychora
from tychora.application.pdf_intake import PdfIntakeService
from tychora.application.pdf_processing import policy_sha256
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.pdf_intake import PdfIntakeDescriptor, PdfIntakeSubmission
from tychora.contracts.pdf_policy import PdfProcessingPolicy
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize("next_chunk", [b"", b"b"])
async def test_receive_rejects_expiry_between_chunks(tmp_path, next_chunk):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        service = PdfIntakeService(kb._workspace.owner, kb._workspace.services)
        receipt = await service.prepare(
            PdfIntakeDescriptor.model_validate_json(canonical_bytes(descriptor(kb, 3))),
            kb.scope,
            ExecutionContext(),
        )

        async def chunks():
            yield b"a"

            def expire():
                record = service._read(receipt.intake_id)
                record["expires"] = time.time() - 1
                service._write(record)

            await kb._workspace.owner.run_blocking(expire, ExecutionContext())
            yield next_chunk
            pytest.fail("Expired upload consumed another frame")

        with pytest.raises(TychoraError) as failure:
            await service.receive(
                receipt.intake_id, chunks(), kb.scope, ExecutionContext()
            )
        assert failure.value.code == "SCOPE_CHANGED"
        assert (
            await service.inspect(receipt.intake_id, kb.scope, ExecutionContext())
        ).state == "aborted"
        assert not (service.directory / (receipt.intake_id + ".pdf")).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["idle", "empty", "trickle"])
async def test_receive_absolute_lifetime_bounds_waits_and_trickle(
    tmp_path, monkeypatch, stream
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        service = PdfIntakeService(kb._workspace.owner, kb._workspace.services)
        receipt = await service.prepare(
            PdfIntakeDescriptor.model_validate_json(
                canonical_bytes(descriptor(kb, 100))
            ),
            kb.scope,
            ExecutionContext(),
        )

        # Start the 0.2 s lifetime when the receive begins, not before it: a
        # deadline set ahead of receive() can lapse on a slow runner before
        # begin() reads the receipt, which then legitimately reports the
        # already-expired receipt as IDEMPOTENCY_CONFLICT instead of exercising
        # the in-flight expiry that this test is about.
        write = service._write

        def shorten_on_begin(record):
            if record["state"] == "receiving":
                record["expires"] = time.time() + 0.2
            write(record)

        monkeypatch.setattr(service, "_write", shorten_on_begin)

        async def chunks():
            yield b"a"
            if stream == "idle":
                await asyncio.Event().wait()
            while True:
                await asyncio.sleep(0.01)
                yield b"" if stream == "empty" else b"b"

        with pytest.raises(TychoraError) as failure:
            await asyncio.wait_for(
                service.receive(
                    receipt.intake_id, chunks(), kb.scope, ExecutionContext()
                ),
                # Only guards against a hang; the 0.2 s lifetime is what is under test.
                timeout=scaled(5),
            )
        assert failure.value.code == "SCOPE_CHANGED"
        assert (
            await service.inspect(receipt.intake_id, kb.scope, ExecutionContext())
        ).state == "aborted"
        assert not (service.directory / (receipt.intake_id + ".pdf")).exists()


@pytest.fixture
def qualified(monkeypatch):
    monkeypatch.setattr(
        "tychora.application.pdf_processing.resolve_pdf_policy",
        synthetic_policy_resolver,
    )
    monkeypatch.setattr(
        "tychora.application.pdf_intake._isolated_analysis",
        lambda *args: {
            "page_count": 1,
            "native_text_pages": [1],
            "image_only_pages": [],
            "blank_pages": [],
            "unknown_pages": [],
            "requires_ocr": False,
        },
    )


async def analysed(client, kb, data):
    prepared = await client.post("/v1/pdf-intakes", json=descriptor(kb, len(data)))
    assert prepared.status_code == 201, prepared.text
    path = "/v1/pdf-intakes/" + prepared.json()["intake_id"]
    assert (await client.put(path + "/content", content=data)).status_code == 200
    analysis = (await client.post(path + "/analyse")).json()["analysis"]
    policy = PdfProcessingPolicy.model_validate_json(
        canonical_bytes(analysis["proposed_policy"])
    )
    return path, {
        "analysis_sha256": analysis["analysis_sha256"],
        "policy_sha256": policy_sha256(policy),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False])
@pytest.mark.parametrize("restart", [True, False])
async def test_intent_only_handoff_with_existing_job_is_recoverable(
    tmp_path, monkeypatch, qualified, matching, restart
):
    workspace = tmp_path / "workspace"
    headers = {"Idempotency-Key": "existing-job"}

    async def parser(*args):
        raise TychoraError("CAPABILITY_MISSING", "Synthetic unavailable parser.")

    async def verify(client, kb, path, submission, job_id):
        # A bad handoff must not poison initialization or unrelated admissions.
        assert (
            await client.post("/v1/pdf-intakes", json=descriptor(kb, 128))
        ).status_code == 201
        inspected = await client.get(path)
        assert inspected.status_code == 200, inspected.text
        response = await client.post(path + "/submit", json=submission, headers=headers)
        if matching:
            assert inspected.json()["state"] == "submitted"
            assert response.status_code == 202, response.text
            assert response.json()["id"] == job_id
        else:
            assert inspected.json()["state"] == "analysed"
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
            assert (await client.delete(path)).json()["state"] == "aborted"

    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        service = app.state.pdf_intakes
        monkeypatch.setattr(kb._workspace.services.sources, "_extract", parser)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            path, submission = await analysed(client, kb, data)
            accepted = await client.post(
                path + "/submit", json=submission, headers=headers
            )
            assert accepted.status_code == 202, accepted.text
            job_id = accepted.json()["id"]
            await kb._workspace.services.jobs.wait(job_id, ExecutionContext())
            path, submission = await analysed(
                client, kb, data if matching else data + b"\n"
            )

            def interrupted(record):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Synthetic interrupted promotion."
                )

            with monkeypatch.context() as patch:
                patch.setattr(service, "_verified_original", interrupted)
                failed = await client.post(
                    path + "/submit", json=submission, headers=headers
                )
                assert failed.status_code == 409, failed.text
            if not restart:
                await verify(client, kb, path, submission, job_id)
        await kb._workspace.services.source_jobs.aclose()
    if restart:
        with Tychora.open(workspace, token_counter=Counter()) as kb:
            async with client_for(app_for(kb)) as client:
                await verify(client, kb, path, submission, job_id)
            await kb._workspace.services.source_jobs.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["collections", "grant"])
async def test_receipt_rejects_foreign_or_changed_effective_grant(
    tmp_path, qualified, change
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        changes = {
            "collections": {"collection_ids": frozenset()},
            "grant": {"capabilities": frozenset({"write"})},
        }
        altered = kb.scope.model_copy(update=changes[change])
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth(
                {"writer": kb.scope, "altered": altered}, authority="127.0.0.1:8765"
            ),
        )
        async with client_for(app) as client:
            path, submission = await analysed(client, kb, make_pdf(tmp_path))
            headers = {"Authorization": "Bearer altered"}
            for method, suffix, body in (
                ("GET", "", None),
                ("POST", "/analyse", None),
                ("POST", "/submit", submission),
                ("DELETE", "", None),
            ):
                denied = await client.request(
                    method, path + suffix, headers=headers, json=body
                )
                assert denied.status_code == 403, denied.text
                assert denied.json()["code"] == "FORBIDDEN_SCOPE"
                assert "analysis" not in denied.json()
            assert (await client.get(path)).json()["state"] == "analysed"


@pytest.mark.asyncio
async def test_receipt_service_rejects_foreign_principal(tmp_path, qualified):
    # HTTP app construction already forbids foreign-principal token grants.
    # Exercise the private receipt entry points that still require this check.
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            path, submission = await analysed(client, kb, make_pdf(tmp_path))
        intake_id = path.rsplit("/", 1)[1]
        foreign = kb.scope.model_copy(update={"principal_id": "foreign-principal"})
        service = app.state.pdf_intakes
        for method in (service.inspect, service.analyse, service.abort):
            with pytest.raises(TychoraError) as denied:
                await method(intake_id, foreign, ExecutionContext())
            assert denied.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as denied:
            await service.submit(
                intake_id,
                PdfIntakeSubmission.model_validate_json(canonical_bytes(submission)),
                foreign,
                None,
                ExecutionContext(),
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_foreign_workspace_cannot_inspect_or_submit_receipt(tmp_path, qualified):
    with (
        Tychora.open(tmp_path / "first", token_counter=Counter()) as first,
        Tychora.open(tmp_path / "second", token_counter=Counter()) as second,
    ):
        async with client_for(app_for(first)) as client:
            path, submission = await analysed(client, first, make_pdf(tmp_path))
        async with client_for(app_for(second)) as foreign:
            denied = await foreign.get(path)
            assert denied.status_code == 403
            assert "analysis" not in denied.json()
            denied = await foreign.post(path + "/submit", json=submission)
            assert denied.status_code == 403
            assert denied.json()["code"] == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["analysis", "policy", "config"])
async def test_submit_rejects_stale_analysis_policy_or_configuration(
    tmp_path, qualified, change
):
    workspace = tmp_path / "workspace"
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            path, submission = await analysed(client, kb, make_pdf(tmp_path))
            if change == "config":
                candidate = (
                    (workspace / "workspace.toml").read_text()
                    + '\n[configuration.output]\ntitle = "Changed synthetic configuration"\n'
                )
                activated = await client.put(
                    "/v1/settings",
                    json={"profile": "local", "workspace_toml": candidate},
                )
                assert activated.status_code == 200, activated.text
            else:
                submission[change + "_sha256"] = "0" * 64
            denied = await client.post(path + "/submit", json=submission)
            assert denied.status_code == 409, denied.text
            assert denied.json()["code"] == (
                "SCOPE_CHANGED" if change == "config" else "IDEMPOTENCY_CONFLICT"
            )
            inspected = (await client.get(path)).json()
            assert inspected["state"] == "analysed"
            assert inspected["job"] is None
        await app.state.runtime.services.source_jobs.aclose()
