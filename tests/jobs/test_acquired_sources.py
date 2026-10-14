"""Trusted receipt capture without inline/base64 source reconstruction."""

import hashlib

import pytest

from tests.transports.test_http import Counter
from tests.transports.test_pdf_intake import app_for, client_for, descriptor, make_pdf
from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def synthetic_policy_resolver(
    owner, services, descriptor, facts, *, max_retained_bytes
):
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    return PdfProcessingPolicy(
        mode="native",
        native_pages=tuple(facts["native_text_pages"]),
        ocr_pages=(),
        blank_pages=tuple(facts["blank_pages"]),
        backend_identity=FrozenJsonMap({"parser": "synthetic-test"}),
        runtime_inventory=FrozenJsonMap({"fixture": "1"}),
        model_inventory_sha256="0" * 64,
        options=FrozenJsonMap(),
        limits=FrozenJsonMap({"max_retained_bytes": max_retained_bytes}),
    ), ()


@pytest.mark.asyncio
async def test_public_upload_descriptor_cannot_open_arbitrary_cas(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            value = descriptor(kb, 128)
            value.pop("expected_size_bytes")
            value.update(type="upload", content_sha256="0" * 64, size_bytes=128)
            response = await client.post("/v1/sources", json=value)
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "FORBIDDEN_SCOPE"


def test_absent_policy_preserves_source_serialization():
    from tychora.application.jobs import source_intent
    from tychora.contracts.requests import SourceInput

    source = SourceInput.from_mapping(
        {
            "type": "text",
            "text": "synthetic",
            "source_key": "test",
            "collection_id": "00000000-0000-0000-0000-000000000001",
            "kind": "manual@1",
        }
    )
    assert "pdf_processing" not in source.root.model_dump(mode="json")
    assert "pdf_processing" not in source_intent(source)


@pytest.mark.asyncio
async def test_capture_precedes_parser_and_retry_uses_same_immutable_original(
    tmp_path, monkeypatch
):
    import tychora.application.pdf_processing as processing

    monkeypatch.setattr(processing, "resolve_pdf_policy", synthetic_policy_resolver)
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        sources = kb._workspace.services.sources
        seen = []
        registrations = []

        async def unavailable_parser(registration, capture, ctx, source, scope):
            # The real repository must own the capture before the parser is called.
            def durable():
                row = kb._workspace.owner.db.execute(
                    "SELECT captured_inputs_sha FROM jobs WHERE captured_inputs_sha=?",
                    (capture.manifest_ref.sha256,),
                ).fetchone()
                assert row is not None

            await kb._workspace.owner.run_blocking(durable, ctx)
            seen.append(capture)
            registrations.append(registration)
            raise TychoraError(
                "DEADLINE_EXCEEDED",
                "Synthetic parser failure for retained retry.",
                retryable=True,
            )

        monkeypatch.setattr(sources, "_extract", unavailable_parser)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            response = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            path = f"/v1/pdf-intakes/{response.json()['intake_id']}"
            await client.put(path + "/content", content=data)
            response = await client.post(path + "/analyse")
            analysis = response.json()["analysis"]
            from tychora.contracts.identity import canonical_bytes
            from tychora.contracts.pdf_policy import PdfProcessingPolicy

            policy = PdfProcessingPolicy.model_validate_json(
                canonical_bytes(analysis["proposed_policy"])
            )
            submission = {
                "analysis_sha256": analysis["analysis_sha256"],
                "policy_sha256": processing.policy_sha256(policy),
            }
            response = await client.post(
                path + "/submit",
                json=submission,
                headers={"Idempotency-Key": "synthetic-upload"},
            )
            assert response.status_code == 202, response.text
            accepted = response.json()
            status = await kb._workspace.services.jobs.wait(
                accepted["id"], ExecutionContext()
            )
            assert status.state == "failed"
            assert status.captured_inputs is not None
            assert seen[0].original_sha == hashlib.sha256(data).hexdigest()
            assert seen[0].source["type"] == "upload"
            assert canonical_bytes(seen[0].source["pdf_processing"]) == canonical_bytes(
                policy.model_dump(mode="json")
            )
            repeated = await client.post(
                path + "/submit",
                json=submission,
                headers={"Idempotency-Key": "synthetic-upload"},
            )
            assert repeated.json()["id"] == accepted["id"]
            changed = await client.post(
                path + "/submit",
                json=submission,
                headers={"Idempotency-Key": "changed"},
            )
            assert changed.status_code == 409
            retried = await kb._workspace.services.source_jobs.retry_async(
                accepted["id"], kb.scope
            )
            assert retried.id == accepted["id"]
            status = await kb._workspace.services.jobs.wait(
                accepted["id"], ExecutionContext()
            )
            assert status.state == "failed"
            assert len(seen) == 2
            assert seen[1].manifest_ref == seen[0].manifest_ref
            assert "data" not in seen[1].source and "path" not in seen[1].source

            # Seed a registered revision with the real registration produced above;
            # sync must resolve its retained original rather than accept a caller CAS ref.
            def register():
                from tychora.adapters.sqlite.corpus import _row

                registration = registrations[0]
                owner = kb._workspace.owner
                owner.store._insert(
                    "sources", _row(registration.source), ("source_id",)
                )
                envelope = owner.store.registry.historical_definitions()[
                    registration.revision.kind_id
                ]
                kind = envelope["kind"]
                owner.store._insert(
                    "kinds",
                    {
                        "kind_id": kind["id"],
                        "definition_hash": envelope["envelope_digest"],
                        "label": kind["label"],
                        "description": kind["description"],
                        "role": kind["role"],
                        "definition_json": canonical_bytes(envelope).decode(),
                    },
                    ("kind_id",),
                )
                owner.store._insert(
                    "revisions", _row(registration.revision), ("revision_id",)
                )
                owner.db.execute(
                    "INSERT INTO revision_artifacts (revision_id,role,source_ref,artifact_sha) VALUES (?, 'original', 'original', ?)",
                    (registration.revision.revision_id, seen[0].original_sha),
                )

            await kb._workspace.owner.run_blocking(register, ExecutionContext())
            synced = await kb._workspace.services.source_jobs.sync_async(
                registrations[0].source.source_id, kb.scope
            )
            sync_status = await kb._workspace.services.jobs.wait(
                synced.id, ExecutionContext()
            )
            assert sync_status.captured_inputs is not None
            assert len(seen) == 3
            assert seen[2].original_ref == seen[0].original_ref
            # A different original with the same logical key must not silently update it.
            changed_data = data + b"\n"
            duplicate = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(changed_data))
            )
            duplicate_path = "/v1/pdf-intakes/" + duplicate.json()["intake_id"]
            await client.put(duplicate_path + "/content", content=changed_data)
            duplicate_analysis = (
                await client.post(duplicate_path + "/analyse")
            ).json()["analysis"]
            duplicate_policy = PdfProcessingPolicy.model_validate_json(
                canonical_bytes(duplicate_analysis["proposed_policy"])
            )
            rejected = await client.post(
                duplicate_path + "/submit",
                json={
                    "analysis_sha256": duplicate_analysis["analysis_sha256"],
                    "policy_sha256": processing.policy_sha256(duplicate_policy),
                },
                headers={"Idempotency-Key": "different-original"},
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "IDEMPOTENCY_CONFLICT"
        await kb._workspace.services.source_jobs.aclose()


@pytest.mark.asyncio
async def test_restart_reconciles_acceptance_before_response_even_after_ttl(
    tmp_path, monkeypatch
):
    import time

    import tychora.application.pdf_processing as processing
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    monkeypatch.setattr(processing, "resolve_pdf_policy", synthetic_policy_resolver)
    workspace = tmp_path / "workspace"
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        service = app.state.pdf_intakes

        async def parser(*args):
            raise TychoraError(
                "CAPABILITY_MISSING", "Synthetic runtime intentionally unavailable."
            )

        monkeypatch.setattr(kb._workspace.services.sources, "_extract", parser)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            response = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            receipt = response.json()["intake_id"]
            path = "/v1/pdf-intakes/" + receipt
            await client.put(path + "/content", content=data)
            analysis = (await client.post(path + "/analyse")).json()["analysis"]
            policy = PdfProcessingPolicy.model_validate_json(
                canonical_bytes(analysis["proposed_policy"])
            )
            submission = {
                "analysis_sha256": analysis["analysis_sha256"],
                "policy_sha256": processing.policy_sha256(policy),
            }
            write = service._write

            def lose_response(record):
                if record["state"] == "submitted":
                    raise TychoraError(
                        "WORKSPACE_BUSY", "Synthetic interrupted handoff."
                    )
                write(record)

            monkeypatch.setattr(service, "_write", lose_response)
            response = await client.post(
                path + "/submit",
                json=submission,
                headers={"Idempotency-Key": "restart"},
            )
            assert response.status_code == 409
            monkeypatch.setattr(service, "_write", write)

            def expire():
                record = service._read(receipt)
                record["expires"] = time.time() - 1
                write(record)
                return kb._workspace.owner.db.execute(
                    "SELECT job_id FROM jobs WHERE idempotency_key='restart'"
                ).fetchone()[0]

            job_id = await kb._workspace.owner.run_blocking(expire, ExecutionContext())
            await kb._workspace.services.jobs.wait(job_id, ExecutionContext())
        await kb._workspace.services.source_jobs.aclose()
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        app = app_for(kb)
        async with client_for(app) as client:
            inspected = await client.get(path)
            assert inspected.json()["state"] == "submitted"
            response = await client.post(
                path + "/submit",
                json=submission,
                headers={"Idempotency-Key": "restart"},
            )
            assert response.status_code == 202, response.text
            assert response.json()["id"] == job_id
            count = await kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT COUNT(*) FROM jobs"
                ).fetchone()[0],
                ExecutionContext(),
            )
            assert count == 1


@pytest.mark.asyncio
async def test_concurrent_submit_preserves_each_inflight_trusted_binding(
    tmp_path, monkeypatch
):
    import asyncio

    import tychora.application.pdf_processing as processing
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    monkeypatch.setattr(processing, "resolve_pdf_policy", synthetic_policy_resolver)
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = app_for(kb)
        scheduler = kb._workspace.services.source_jobs
        original = scheduler.add_acquired_async
        both_promoted, first_finished = asyncio.Event(), asyncio.Event()
        calls = 0

        async def pause_second(acquired, scope, key=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                await both_promoted.wait()
            else:
                both_promoted.set()
                await first_finished.wait()
            return await original(acquired, scope, key)

        monkeypatch.setattr(scheduler, "add_acquired_async", pause_second)
        async with client_for(app) as client:
            data = make_pdf(tmp_path)
            response = await client.post(
                "/v1/pdf-intakes", json=descriptor(kb, len(data))
            )
            path = "/v1/pdf-intakes/" + response.json()["intake_id"]
            await client.put(path + "/content", content=data)
            analysis = (await client.post(path + "/analyse")).json()["analysis"]
            policy = PdfProcessingPolicy.model_validate_json(
                canonical_bytes(analysis["proposed_policy"])
            )
            submission = {
                "analysis_sha256": analysis["analysis_sha256"],
                "policy_sha256": processing.policy_sha256(policy),
            }

            async def submit(first=False):
                response = await client.post(
                    path + "/submit",
                    json=submission,
                    headers={"Idempotency-Key": "concurrent"},
                )
                if first:
                    first_finished.set()
                return response

            first, second = await asyncio.wait_for(
                asyncio.gather(submit(True), submit()), timeout=20
            )
            assert first.status_code == second.status_code == 202, (
                first.text,
                second.text,
            )
            assert first.json()["id"] == second.json()["id"]
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_standard_source_scheduler_preserves_existing_execution_interceptors(
    tmp_path, monkeypatch
):
    from tychora.contracts.requests import SourceInput

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        sources = kb._workspace.services.sources
        original = sources.execute_accepted

        async def existing_interceptor(handle, source, scope, *, ctx=None):
            return await original(handle, source, scope, ctx=ctx)

        monkeypatch.setattr(sources, "execute_accepted", existing_interceptor)
        value = descriptor(kb, 128)
        value.pop("expected_size_bytes")
        value.update(type="text", format="text", text="Synthetic ordinary source.")
        handle = await kb._workspace.services.source_jobs.add_async(
            SourceInput.from_mapping(value), kb.scope
        )
        status = await kb._workspace.services.jobs.wait(handle.id, ExecutionContext())
        assert status.state == "succeeded"
        await kb._workspace.services.source_jobs.aclose()


def test_replay_policy_cannot_raise_current_owner_processing_budget(
    tmp_path, monkeypatch
):
    import tychora.application.pdf_processing as processing
    from tychora.application.pdf_intake import PdfIntakeService
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_intake import PdfIntakeDescriptor

    monkeypatch.setattr(processing, "resolve_pdf_policy", synthetic_policy_resolver)
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        PdfIntakeService(
            kb._workspace.owner, kb._workspace.services, max_retained_bytes=128
        )
        value = PdfIntakeDescriptor.model_validate_json(
            canonical_bytes(descriptor(kb, 128))
        )
        facts = {"native_text_pages": (1,), "blank_pages": ()}
        policy, _ = synthetic_policy_resolver(
            kb._workspace.owner,
            kb._workspace.services,
            value,
            facts,
            max_retained_bytes=1073741824,
        )
        with pytest.raises(TychoraError) as error:
            processing.revalidate_pdf_policy(
                kb._workspace.owner, kb._workspace.services, value, facts, policy
            )
        assert error.value.code == "SCOPE_CHANGED"
