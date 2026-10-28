"""The browser echoes server hashes; it does not reproduce Python float JSON."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.ingestion import test_docling_mapping as mapping_fixtures
from tests.jobs.test_acquired_sources import synthetic_policy_resolver
from tests.transports.test_pdf_intake import app_for, client_for, descriptor, make_pdf
from tychora import Tychora
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.pdf_policy import PdfProcessingPolicy
from tychora.ports.execution import ExecutionContext

document_kind = mapping_fixtures.document_kind
retained_case = mapping_fixtures.retained_case


@pytest.mark.asyncio
async def test_http_analysis_issues_exact_policy_confirmation_hash(
    tmp_path, monkeypatch
):
    from tychora.application import pdf_processing

    def resolver(*args, **kwargs):
        policy, blockers = synthetic_policy_resolver(*args, **kwargs)
        return policy.model_copy(
            update={
                "options": FrozenJsonMap({"native_image_scale": 1.0, "ocr_scale": 3.0})
            }
        ), blockers

    monkeypatch.setattr(pdf_processing, "resolve_pdf_policy", resolver)
    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:

        async def unavailable(*args, **kwargs):
            raise TychoraError("CAPABILITY_MISSING", "Synthetic extraction stop")

        monkeypatch.setattr(kb._workspace.services.sources, "_extract", unavailable)
        async with client_for(app_for(kb)) as client:
            data = make_pdf(tmp_path)
            receipt = (
                await client.post("/v1/pdf-intakes", json=descriptor(kb, len(data)))
            ).json()
            path = "/v1/pdf-intakes/" + receipt["intake_id"]
            assert (
                await client.put(path + "/content", content=data)
            ).status_code == 200
            analysis = (await client.post(path + "/analyse")).json()["analysis"]
            policy = PdfProcessingPolicy.model_validate_json(
                canonical_bytes(analysis["proposed_policy"])
            )
            assert analysis["policy_sha256"] == pdf_processing.policy_sha256(policy)
            submitted = {
                "analysis_sha256": analysis["analysis_sha256"],
                "policy_sha256": "f" * 64,
            }
            assert (
                await client.post(
                    path + "/submit",
                    json=submitted,
                    headers={"Idempotency-Key": "wrong"},
                )
            ).status_code == 409
            submitted["policy_sha256"] = analysis["policy_sha256"]
            response = await client.post(
                path + "/submit", json=submitted, headers={"Idempotency-Key": "exact"}
            )
            assert response.status_code == 202, response.text
            status = await kb._workspace.services.jobs.wait(
                response.json()["id"], ExecutionContext()
            )
            assert status.captured_inputs is not None
            # Exact synthetic browser DTO evidence stays outside committed fixtures.
            (tmp_path / "analysis-response.json").write_bytes(canonical_bytes(analysis))


def test_legacy_analysis_dump_preserves_absent_hash():
    from tychora.contracts.pdf_intake import PdfAnalysis

    legacy = {
        "original_sha256": "a" * 64,
        "size_bytes": 1,
        "page_count": 1,
        "native_text_pages": [1],
        "image_only_pages": [],
        "blank_pages": [],
        "unknown_pages": [],
        "requires_ocr": False,
        "blockers": [],
        "proposed_policy": None,
        "analysis_sha256": "b" * 64,
    }
    parsed = PdfAnalysis.model_validate_json(canonical_bytes(legacy))
    assert parsed.model_dump(mode="json") == legacy


@pytest.mark.qualification
@pytest.mark.asyncio
async def test_qualified_http_pdf_submission_publishes_and_reports_batches(tmp_path):
    import os

    if not os.environ.get("TYCHORA_TEST_DOCLING_MODELS"):
        pytest.skip("Explicit verified local-model bundle required")
    from tests.qualification.test_scanned_pdf import generated_scan
    from tychora.configuration.indexed_representation import (
        indexed_representation_identity,
    )

    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:
        owner = kb._workspace.owner
        original_config = owner.config
        assert original_config.values.extraction.parser == "markdown"
        async with client_for(app_for(kb)) as client:
            data = generated_scan("columns")
            receipt = (
                await client.post("/v1/pdf-intakes", json=descriptor(kb, len(data)))
            ).json()
            path = "/v1/pdf-intakes/" + receipt["intake_id"]
            assert (
                await client.put(path + "/content", content=data)
            ).status_code == 200
            response = await client.post(path + "/analyse")
            assert response.status_code == 200, response.text
            analysis = response.json()["analysis"]
            assert analysis["blockers"] == []
            assert analysis["proposed_policy"]["origin_proof"] == "ordered-ocr-cells@1"
            submitted = {
                name: analysis[name] for name in ("analysis_sha256", "policy_sha256")
            }
            response = await client.post(
                path + "/submit",
                json=submitted,
                headers={"Idempotency-Key": "qualified"},
            )
            assert response.status_code == 202, response.text
            status = await kb._workspace.services.jobs.wait(
                response.json()["id"], ExecutionContext()
            )
            assert status.state == "succeeded", status
            events = await kb._workspace.services.jobs.events_after(
                status.id, 0, 100, ExecutionContext()
            )
            accepted = [
                counter.count
                for event in events
                for counter in event.counters
                if counter.name == "accepted_pdf_pages"
            ]
            assert accepted == [16, 17]
            assert owner.config.config_sha == original_config.config_sha
            assert indexed_representation_identity(
                owner.config
            ) == indexed_representation_identity(original_config)
            (tmp_path / "qualified-analysis-response.json").write_bytes(
                canonical_bytes(analysis)
            )
            (tmp_path / "qualified-job-response.json").write_bytes(
                canonical_bytes(status.model_dump(mode="json"))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["limit", "lineage", "wrapped-lineage"])
@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
async def test_pdf_composition_limit_is_durable_nonretryable_job_failure(
    tmp_path, monkeypatch, fault, retained_case
):
    from tychora.application import pdf_processing
    from tychora.contracts.artifacts import ArtifactRef
    from tychora.contracts.composition import DocumentCompositionPlan
    from tychora.ingestion.composition import _input_references

    monkeypatch.setattr(pdf_processing, "resolve_pdf_policy", synthetic_policy_resolver)
    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:

        async def oversized_parser_output(*args, **kwargs):
            if fault == "wrapped-lineage":
                from tests.runtime.test_parser_bounds import WrappedPdfParser
                from tychora.application.runtime import (
                    ParserExecutionLimits,
                    parser_process,
                )

                parser_process(
                    WrappedPdfParser(),
                    (),
                    limits=ParserExecutionLimits(
                        1, 20, 4294967296, 268435456, ExecutionContext()
                    ),
                )
            if fault == "lineage":
                from tychora.adapters.docling.ocr_lineage import validate_parser_lineage

                validate_parser_lineage((), {}, None)
            # Only descriptors; exercise the authoritative input admission and real
            # source-job failure mapping without allocating 256 MiB or loading models.
            ref = ArtifactRef(
                sha256="e" * 64,
                storage_key="e" * 64,
                size_bytes=268435457,
                media_type="image/png",
            )
            original, _, _ = retained_case
            parsed = original.model_copy(
                update={
                    "assets": (
                        original.assets[0].model_copy(update={"artifact_ref": ref}),
                    ),
                    "pdf_processing": FrozenJsonMap({"mode": "native"}),
                }
            )
            _input_references(
                parsed, DocumentCompositionPlan(base_normalizer_fingerprint="f" * 64)
            )
            pytest.fail("Oversized composition was accepted")

        monkeypatch.setattr(
            kb._workspace.services.sources, "_extract", oversized_parser_output
        )
        async with client_for(app_for(kb)) as client:
            data = make_pdf(tmp_path)
            receipt = (
                await client.post("/v1/pdf-intakes", json=descriptor(kb, len(data)))
            ).json()
            path = "/v1/pdf-intakes/" + receipt["intake_id"]
            await client.put(path + "/content", content=data)
            analysis = (await client.post(path + "/analyse")).json()["analysis"]
            response = await client.post(
                path + "/submit",
                json={
                    name: analysis[name]
                    for name in ("analysis_sha256", "policy_sha256")
                },
                headers={"Idempotency-Key": "over-limit"},
            )
            assert response.status_code == 202
            status = await kb._workspace.services.jobs.wait(
                response.json()["id"], ExecutionContext()
            )
            assert status.state == "failed"
            assert status.error_code == (
                "LIMIT_EXCEEDED" if fault == "limit" else "INTEGRITY_ERROR"
            )
            assert status.captured_inputs is not None
            retry_class = await kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT state FROM job_steps WHERE job_id=? AND step_key='__tychora__/retry'",
                    (status.id,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            assert retry_class == "permanent"
            assert status.result_snapshot_id is None
