"""The browser echoes server hashes; it does not reproduce Python float JSON."""

import pytest

from tests.jobs.test_acquired_sources import synthetic_policy_resolver
from tests.transports.test_http import Counter
from tests.transports.test_pdf_intake import app_for, client_for, descriptor, make_pdf
from tychora import Tychora
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.pdf_policy import PdfProcessingPolicy
from tychora.ports.execution import ExecutionContext


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
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:

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

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        original_config = owner.config
        assert original_config.values.extraction.parser == "markdown"
        async with client_for(app_for(kb)) as client:
            data = generated_scan()
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
