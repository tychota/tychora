"""Public command behavior through Typer's real command tree."""

import json
from hashlib import sha256
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from tychora.interfaces.cli import app


def test_json_discovery_contains_no_progress_noise(tmp_path):
    result = CliRunner().invoke(
        app, ["--workspace", str(tmp_path / "workspace"), "inspect", "kinds", "--json"]
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1"
    assert payload["subject"] == "kinds"
    assert result.stderr == ""


def test_setup_request_has_explicit_asset_and_bundle_fields():
    from tychora.contracts.operations import MaintenanceRequest

    request = MaintenanceRequest.from_mapping(
        {
            "operation": "setup",
            "assets": ["output-tokenizer"],
            "local_bundle": "/bundle",
            "offline": True,
        }
    )
    assert request.root.assets == ("output-tokenizer",)
    assert request.root.local_bundle == "/bundle"


def test_public_setup_imports_explicit_verified_bundle(tmp_path, monkeypatch):
    from tychora import Tychora
    from tychora.application import setup as setup_module

    payload = b"synthetic model inventory"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "inventory.bin").write_bytes(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": "synthetic-inventory",
                        "selection": "synthetic-model",
                        "path": "inventory.bin",
                        "sha256": sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "url": "https://example.invalid/inventory",
                        "license": "MIT",
                        "owner": "Synthetic",
                        "revision": "1",
                        "notice_url": "https://example.invalid/notice",
                    }
                ],
                "notices": [
                    {
                        "selection": "synthetic-model",
                        "attribution": "Synthetic",
                        "license": "MIT",
                        "notice_url": "https://example.invalid/notice",
                        "model_card_url": "https://example.invalid/card",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(setup_module, "MANIFEST", manifest)
    cache = tmp_path / "models"
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(cache))
    with Tychora.open(tmp_path / "workspace") as kb:
        result = kb.management.run(
            {
                "operation": "setup",
                "assets": ["synthetic-model"],
                "local_bundle": str(bundle),
                "offline": True,
            }
        )
        assert result.job.state == "succeeded"
        assert kb.jobs.get(result.job.id).state == "succeeded"
    assert (cache / "inventory.bin").read_bytes() == payload


def test_setup_requires_explicit_nonempty_selection(tmp_path):
    from tychora import Tychora
    from tychora.contracts.errors import TychoraError

    with (
        Tychora.open(tmp_path / "workspace") as kb,
        pytest.raises(TychoraError, match="Select at least one") as missing,
    ):
        kb.management.run({"operation": "setup"})
    assert missing.value.code == "INVALID_REQUEST"


def test_add_emits_stage_before_a_held_job_completes(capsys):
    from types import SimpleNamespace

    from tychora.interfaces.commands.add import wait_with_progress

    handle = SimpleNamespace(id="synthetic-job")

    class HeldJobs:
        polls = 0

        def wait(self, job_id, *, timeout):
            assert job_id == handle.id
            assert 0 < timeout <= 0.25
            self.polls += 1
            if self.polls == 2:
                assert "stage=acquire" in capsys.readouterr().err
                return SimpleNamespace(state="succeeded")
            return SimpleNamespace(state="running")

        def events_after(self, job_id, *, after):
            assert job_id == handle.id
            if after == 0:
                return (SimpleNamespace(sequence=1, stage="acquire", state="running"),)
            return (SimpleNamespace(sequence=2, stage="complete", state="succeeded"),)

    assert wait_with_progress(HeldJobs(), handle).state == "succeeded"


def test_unknown_inspect_noun_is_canonical_error(tmp_path):
    result = CliRunner().invoke(
        app, ["--workspace", str(tmp_path / "workspace"), "inspect", "unregistered"]
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "INVALID_REQUEST"
    assert not (tmp_path / "workspace").exists()


def test_conflicting_output_switches_are_rejected(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--format",
            "markdown",
            "inspect",
            "kinds",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "INVALID_REQUEST"
    assert not (tmp_path / "workspace").exists()


def test_public_inspect_api_exposes_scoped_records(tmp_path):
    from tychora import Tychora
    from tychora.contracts.errors import TychoraError

    with Tychora.open(tmp_path / "workspace") as kb:
        try:
            kb.inspect({"subject": "evidence", "id": "missing"})
        except TychoraError as error:
            assert error.code == "NOT_FOUND"
        else:
            raise AssertionError("Missing evidence was accepted")


@pytest.mark.asyncio
async def test_setup_http_uses_same_verified_job_and_scope(tmp_path, monkeypatch):
    from tychora import Tychora
    from tychora.application import setup as setup_module
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    payload = b"synthetic HTTP asset"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "asset.bin").write_bytes(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": "synthetic-http",
                        "selection": "synthetic-http",
                        "path": "asset.bin",
                        "sha256": sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "url": "https://example.invalid/asset",
                        "license": "MIT",
                        "owner": "Synthetic",
                        "revision": "1",
                        "notice_url": "https://example.invalid/notice",
                    }
                ],
                "notices": [
                    {
                        "selection": "synthetic-http",
                        "attribution": "Synthetic",
                        "license": "MIT",
                        "notice_url": "https://example.invalid/notice",
                        "model_card_url": "https://example.invalid/card",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(setup_module, "MANIFEST", manifest)
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(tmp_path / "models"))
    with Tychora.open(tmp_path / "workspace") as kb:
        owner = kb._workspace.owner
        reader = owner.scope.model_copy(update={"capabilities": frozenset({"read"})})
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner": owner.scope, "reader": reader}, authority="127.0.0.1:8765"
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as http:
            request = {
                "operation": "setup",
                "assets": ["synthetic-http"],
                "local_bundle": str(bundle),
                "offline": True,
            }
            denied = await http.post(
                "/v1/jobs", json=request, headers={"Authorization": "Bearer reader"}
            )
            assert denied.status_code == 403
            accepted = await http.post(
                "/v1/jobs", json=request, headers={"Authorization": "Bearer owner"}
            )
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["job"]["state"] == "succeeded"
            inspected = await http.post(
                "/v1/inspect",
                json={"subject": "evidence", "id": "missing"},
                headers={"Authorization": "Bearer owner"},
            )
            assert inspected.status_code == 404
        assert (tmp_path / "models" / "asset.bin").read_bytes() == payload


@pytest.mark.asyncio
async def test_generic_http_inspect_roundtrips_binary_original(tmp_path):
    from tychora import Tychora
    from tychora.contracts.operations import InspectionResult
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth
    from tychora.ports.execution import ExecutionContext

    class Counter:
        model = "synthetic-counter"
        tokenizer_id = "cl100k_base"

        def count(self, text):
            return len(text)

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        accepted = kb.sources.add(
            {
                "type": "text",
                "source_key": "binary-inspection",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Synthetic original.",
                "metadata": {
                    "title": "Binary inspection",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(accepted.id, timeout=5).state == "succeeded"
        owner = kb._workspace.owner
        payload = b"\x00\xff\xfe\x80\x01synthetic"

        def install_binary():
            revision_id, old_sha = owner.db.execute(
                "SELECT revision_id, original_sha FROM revisions WHERE source_id="
                "(SELECT source_id FROM sources WHERE source_key='binary-inspection')"
            ).fetchone()
            ref = owner.artifacts.put_stream(
                BytesIO(payload), "application/octet-stream"
            )
            owner.db.execute(
                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00Z",
                ),
            )
            owner.db.execute(
                "UPDATE revisions SET original_sha=? WHERE revision_id=?",
                (ref.sha256, revision_id),
            )
            owner.db.execute(
                "UPDATE revision_artifacts SET artifact_sha=? WHERE revision_id=? AND role='original'",
                (ref.sha256, revision_id),
            )
            owner.db.execute(
                "UPDATE locators SET artifact_sha=?, details_json=json_set(details_json,'$.artifact_ref',?) WHERE artifact_sha=?",
                (ref.sha256, ref.sha256, old_sha),
            )
            return revision_id

        revision_id = await owner.run_blocking(install_binary, ExecutionContext())
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as http:
            response = await http.post(
                "/v1/inspect",
                json={
                    "subject": "revisions",
                    "id": revision_id,
                    "original": True,
                },
            )
        assert response.status_code == 200, response.text
        result = InspectionResult.model_validate_json(response.content)
        assert result.payload == payload
