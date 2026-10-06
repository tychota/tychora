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
    from tychora.contracts.operations import MaintenanceRequest, SetupMaintenance

    request = MaintenanceRequest.from_mapping(
        {
            "operation": "setup",
            "assets": ["output-tokenizer"],
            "local_bundle": "/bundle",
            "offline": True,
        }
    )
    assert isinstance(request.root, SetupMaintenance)
    assert request.root.assets == ("output-tokenizer",)
    assert request.root.local_bundle == "/bundle"


def test_manage_import_bootstraps_without_original_or_control_workspace(tmp_path):
    import shutil

    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.application.backup import BackupService
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope
    from tychora.ports.execution import ExecutionContext

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "custom", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset(),
        access_ceiling=0,
        capabilities=frozenset({"manage"}),
    )
    source = tmp_path / "removed-source"
    with WorkspaceOwner.open(source, config, scope) as owner:
        BackupService(owner).export(tmp_path / "backup", scope, ctx=ExecutionContext())
    shutil.rmtree(source)
    settings.unlink()
    restored = tmp_path / "restored"
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(source),
            "manage",
            "import",
            str(tmp_path / "backup"),
            str(restored),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert restored.is_dir()
    assert not source.exists()
    db = open_corpus(restored / "corpus.sqlite3")
    try:
        assert db.execute("SELECT workspace_id FROM workspaces").fetchone()[0]
    finally:
        db.close()


def test_manage_migrate_uses_packaged_v1_target_and_rejects_unknown_version(tmp_path):
    workspace = tmp_path / "workspace"
    valid = CliRunner().invoke(
        app, ["--workspace", str(workspace), "manage", "migrate", "1"]
    )
    assert valid.exit_code == 0, valid.stderr
    unsupported = CliRunner().invoke(
        app, ["--workspace", str(workspace), "manage", "migrate", "2"]
    )
    assert unsupported.exit_code != 0
    assert "Unsupported migration target" in unsupported.stderr


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


def test_root_output_conflict_is_canonical_before_workspace_creation(tmp_path):
    workspace = tmp_path / "workspace"
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "--json",
            "--format",
            "markdown",
            "inspect",
            "kinds",
        ],
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "INVALID_REQUEST"
    assert not workspace.exists()


def test_serve_passes_explicit_profile_to_http_host(tmp_path, monkeypatch):
    from tychora.interfaces.http import server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
        '[profiles.alternate]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "local"\n'
    )
    selected = []

    async def fake_serve(path, *, port, config):
        selected.append((path, port, config.profile, config.values.offline))

    monkeypatch.setattr(server, "serve", fake_serve)
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "--profile",
            "alternate",
            "--offline",
            "serve",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert selected == [(workspace, 0, "alternate", True)]


def test_serve_rejects_offline_flag_for_online_profile_before_host(
    tmp_path, monkeypatch
):
    from tychora.interfaces.http import server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "local"\n[configuration]\noffline = false\n'
    )
    called = []

    async def fake_serve(path, *, port, config):
        called.append(path)

    monkeypatch.setattr(server, "serve", fake_serve)
    result = CliRunner().invoke(
        app, ["--workspace", str(workspace), "--offline", "serve"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "CAPABILITY_MISSING"
    assert called == []


def test_serve_passes_explicit_profile_to_stdio_host(tmp_path, monkeypatch):
    from tychora.interfaces.mcp import server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
        '[profiles.alternate]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "local"\n'
    )
    selected = []

    async def fake_stdio(path, *, config):
        selected.append((path, config.profile, config.values.offline))

    monkeypatch.setattr(server, "serve_stdio", fake_stdio)
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "--profile",
            "alternate",
            "--offline",
            "serve",
            "--stdio",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert selected == [(workspace, "alternate", True)]


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
        http_app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=http_app),
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
    cli = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "inspect",
            "revisions",
            revision_id,
            "--original",
            "--json",
        ],
    )
    assert cli.exit_code == 0, cli.stderr
    cli_data = json.loads(cli.stdout)
    assert cli_data.pop("schema_version") == "1"
    assert InspectionResult.model_validate_json(json.dumps(cli_data)).payload == payload
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        source_id = await kb._workspace.owner.run_blocking(
            lambda: kb._workspace.owner.db.execute(
                "SELECT source_id FROM revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()[0],
            ExecutionContext(),
        )
        kb.management.run({"operation": "remove", "source_ids": [source_id]})
        kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source_id,
                "revision_ids": [revision_id],
                "confirm_original_removal": True,
            }
        )
    tombstoned = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "inspect",
            "revisions",
            revision_id,
            "--original",
            "--json",
        ],
    )
    assert tombstoned.exit_code != 0
    assert json.loads(tombstoned.stderr)["code"] == "ORIGINAL_REMOVED"
    missing = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "inspect",
            "revisions",
            "missing",
            "--original",
            "--json",
        ],
    )
    assert missing.exit_code != 0
    assert json.loads(missing.stderr)["code"] == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_job_event_cursor_rejects_oversized_integer_at_public_and_http_boundaries(
    tmp_path,
):
    from tychora import Tychora
    from tychora.contracts.errors import TychoraError
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    class Counter:
        model = "synthetic-counter"
        tokenizer_id = "cl100k_base"

        def count(self, text):
            return len(text)

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        accepted = kb.sources.add(
            {
                "type": "text",
                "source_key": "cursor-source",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Synthetic cursor record.",
                "metadata": {"title": "Cursor", "publisher": "Example", "edition": "1"},
            }
        )
        assert kb.jobs.wait(accepted.id, timeout=5).state == "succeeded"
        huge = 2**100
        with pytest.raises(TychoraError) as direct:
            kb.jobs.events_after(accepted.id, after=huge)
        assert direct.value.code == "INVALID_REQUEST"
        owner = kb._workspace.owner
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
            response = await http.get(
                f"/v1/jobs/{accepted.id}/events/page", params={"after": huge}
            )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_init_reports_effective_configured_provider_when_config_is_supplied(tmp_path):
    settings = tmp_path / "input.toml"
    settings.write_text(
        'schema_version = 1\n[profiles.workbench]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "workbench"\n'
    )
    workspace = tmp_path / "knowledge"
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "init",
            "--provider",
            "local",
            "--config",
            str(settings),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["provider"] == "configured"
    assert (workspace / "workspace.toml").read_bytes() == settings.read_bytes()
