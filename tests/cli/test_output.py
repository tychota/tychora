"""Real process I/O, completion and local owner delegation."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

CLI = Path(sys.executable).parent / "tychora"


def _run(*args, cwd, env=None):
    return subprocess.run(
        [str(CLI), *map(str, args)],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_help_and_completion_have_no_workspace_or_model_side_effects(tmp_path):
    before = set(tmp_path.iterdir())
    help_result = _run("--help", cwd=tmp_path)
    assert help_result.returncode == 0, help_result.stderr
    assert "inspect" in help_result.stdout
    assert "exact-query.json" in help_result.stdout
    for command in ("init", "add", "search", "inspect", "serve", "manage"):
        detailed = _run(command, "--help", cwd=tmp_path)
        assert detailed.returncode == 0, (command, detailed.stderr)
    completion = _run(cwd=tmp_path, env={"_TYCHORA_COMPLETE": "source_zsh"})
    assert completion.returncode == 0, completion.stderr
    assert "_TYCHORA_COMPLETE" in completion.stdout
    suggestions = _run(
        cwd=tmp_path,
        env={
            "_TYCHORA_COMPLETE": "complete_zsh",
            "_TYPER_COMPLETE_ARGS": "tychora in",
        },
    )
    assert suggestions.returncode == 0, suggestions.stderr
    assert "inspect" in suggestions.stdout
    assert set(tmp_path.iterdir()) == before
    imports = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tychora.interfaces.cli; print(sorted(k for k in sys.modules if k.startswith(('tychora.adapters.voyage', 'tychora.adapters.docling', 'tychora.application.owner'))))",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert imports.returncode == 0, imports.stderr
    assert imports.stdout.strip() == "[]"


def test_subprocess_json_stdout_and_static_config_inspection(tmp_path):
    workspace = tmp_path / "workspace"
    initialized = _run("--workspace", workspace, "init", "--json", cwd=tmp_path)
    assert initialized.returncode == 0, initialized.stderr
    payload = json.loads(initialized.stdout)
    assert payload["schema_version"] == "1"
    assert not (workspace / "models").exists()
    result = _run("--workspace", workspace, "inspect", "kinds", "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["subject"] == "kinds"
    assert result.stderr == ""
    db_before = (workspace / "corpus.sqlite3").stat().st_mtime_ns
    config = _run("--workspace", workspace, "inspect", "config", "--json", cwd=tmp_path)
    assert config.returncode == 0, config.stderr
    assert json.loads(config.stdout)["profile"] == "local"
    assert (workspace / "corpus.sqlite3").stat().st_mtime_ns == db_before


def test_busy_owner_cli_delegates_discovery(tmp_path):
    workspace = tmp_path / "workspace"
    cache = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    process = subprocess.Popen(
        [sys.executable, "-m", "tychora.interfaces.http.server", str(workspace)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **({"TYCHORA_MODEL_CACHE": cache} if cache else {})},
    )
    try:
        endpoint = workspace / ".runtime" / "endpoint.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not endpoint.exists():
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            time.sleep(0.05)
        assert endpoint.exists()
        result = _run(
            "--workspace", workspace, "inspect", "kinds", "--json", cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["subject"] == "kinds"
        profiled = _run(
            "--workspace",
            workspace,
            "--profile",
            "local",
            "inspect",
            "kinds",
            "--json",
            cwd=tmp_path,
        )
        assert profiled.returncode == 1
        assert profiled.stdout == ""
        assert json.loads(profiled.stderr)["code"] == "PROFILE_INCOMPATIBLE"
        if cache:
            profiled_setup = _run(
                "--workspace",
                workspace,
                "--profile",
                "local",
                "manage",
                "setup",
                "--asset",
                "output-tokenizer",
                "--json",
                cwd=tmp_path,
            )
            assert profiled_setup.returncode == 1
            assert json.loads(profiled_setup.stderr)["code"] == "PROFILE_INCOMPATIBLE"
            setup = _run(
                "--workspace",
                workspace,
                "manage",
                "setup",
                "--asset",
                "output-tokenizer",
                "--json",
                cwd=tmp_path,
            )
            assert setup.returncode == 0, setup.stderr
            assert json.loads(setup.stdout)["job"]["state"] == "succeeded"
            source = tmp_path / "busy-source.json"
            source.write_text(
                json.dumps(
                    {
                        "type": "text",
                        "source_key": "busy-owner-source",
                        "collection_id": "default",
                        "kind": "manual@1",
                        "format": "text",
                        "text": "Synthetic busy owner source.",
                        "metadata": {
                            "title": "Busy source",
                            "publisher": "Example",
                            "edition": "1",
                        },
                    }
                )
            )
            added = _run(
                "--workspace",
                workspace,
                "add",
                "--request-file",
                source,
                "--json",
                cwd=tmp_path,
            )
            assert added.returncode == 0, added.stderr
            assert json.loads(added.stdout)["state"] == "succeeded"
            assert "stage=" in added.stderr
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_http_serve_interrupt_exits_130(tmp_path):
    workspace = tmp_path / "workspace"
    process = subprocess.Popen(
        [str(CLI), "--workspace", str(workspace), "serve"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        endpoint = workspace / ".runtime" / "endpoint.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not endpoint.exists():
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            time.sleep(0.05)
        assert endpoint.exists()
        time.sleep(0.5)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (stdout, stderr)
        assert stdout == ""
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def test_add_request_file_routes_progress_to_stderr(tmp_path):
    cache = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if not cache:
        pytest.skip("Installed model cache is required for a real source job")
    workspace = tmp_path / "workspace"
    request = tmp_path / "source.json"
    request.write_text(
        json.dumps(
            {
                "type": "text",
                "source_key": "synthetic-record",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "A synthetic document about precise lookup.",
                "metadata": {
                    "title": "Synthetic record",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
    )
    result = _run(
        "--workspace",
        workspace,
        "add",
        "--request-file",
        request,
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "succeeded"
    assert "Accepted add job" in result.stderr
    assert "stage=" in result.stderr
    assert "Accepted" not in result.stdout


def test_add_file_uses_explicit_kind_metadata_and_root(tmp_path):
    cache = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if not cache:
        pytest.skip("Installed model cache is required for a real source job")
    workspace = tmp_path / "workspace"
    source = tmp_path / "synthetic.txt"
    source.write_text("Synthetic handbook with a precise lookup term.")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {"title": "Synthetic handbook", "publisher": "Example", "edition": "1"}
        )
    )
    config = tmp_path / "workspace.toml"
    config.write_text(
        'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "local"\n[configuration.acquisition]\n'
        f"roots = [{json.dumps(str(tmp_path))}]\n"
    )
    initialized = _run(
        "--workspace", workspace, "init", "--config", config, cwd=tmp_path
    )
    assert initialized.returncode == 0, initialized.stderr
    result = _run(
        "--workspace",
        workspace,
        "add",
        source,
        "--kind",
        "manual@1",
        "--metadata-file",
        metadata,
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "succeeded"
    queried = _run(
        "--workspace",
        workspace,
        "search",
        "precise lookup term",
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert queried.returncode == 0, queried.stderr
    assert json.loads(queried.stdout)["schema_version"] == "1"
    exact = tmp_path / "exact-query.json"
    exact.write_text(
        json.dumps(
            {
                "question": "Find the precise term",
                "exact_identifiers": ["rule:precise"],
                "exact_only": True,
            }
        )
    )
    replay = _run(
        "--workspace",
        workspace,
        "search",
        "--request-file",
        exact,
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["schema_version"] == "1"
    markdown = _run(
        "--workspace",
        workspace,
        "--format",
        "markdown",
        "search",
        "precise lookup term",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert markdown.returncode == 0, markdown.stderr
    assert "Evidence pack" in markdown.stdout
    agent = _run(
        "--workspace",
        workspace,
        "--format",
        "agent",
        "search",
        "precise lookup term",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": cache},
    )
    assert agent.returncode == 0, agent.stderr
    assert "<evidence_context>" in agent.stdout


def test_manage_setup_imports_explicit_local_bundle(tmp_path):
    source_cache = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if not source_cache:
        pytest.skip("Installed model cache is required for a verified bundle test")
    from tychora.application.setup import SetupService

    assets = (
        SetupService(Path(source_cache), offline=True)
        .plan(("output-tokenizer",))
        .assets
    )
    bundle = tmp_path / "bundle"
    for asset in assets:
        target = bundle / asset.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source_cache) / asset.path, target)
    workspace = tmp_path / "workspace"
    cache = tmp_path / "new-cache"
    result = _run(
        "--workspace",
        workspace,
        "--offline",
        "manage",
        "setup",
        "--asset",
        "output-tokenizer",
        "--bundle",
        bundle,
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": str(cache)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["job"]["state"] == "succeeded"
    for asset in assets:
        assert (cache / asset.path).read_bytes() == (bundle / asset.path).read_bytes()


@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_cli_stdio_is_protocol_only_and_delegates(tmp_path):
    from tychora.interfaces.http.client import connect_local

    workspace = tmp_path / "workspace"
    process = StdioServerParameters(
        command=str(CLI),
        args=["--workspace", str(workspace), "serve", "--stdio"],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with Client(process, mode="legacy") as mcp:
        result = await mcp.call_tool("discover", {"request": {"subject": "kinds"}})
        assert result.structured_content["subject"] == "kinds"
        sibling = connect_local(workspace)
        try:
            assert sibling.discover({"subject": "kinds"}).subject == "kinds"
        finally:
            sibling.close()


def test_search_missing_offline_tokenizer_reports_setup_guidance(tmp_path):
    workspace = tmp_path / "workspace"
    _run("--workspace", workspace, "init", cwd=tmp_path)
    result = _run(
        "--workspace",
        workspace,
        "--offline",
        "search",
        "exact synthetic lookup",
        "--json",
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": str(tmp_path / "empty-models")},
    )
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "CAPABILITY_MISSING"
    assert "setup" in error["message"].lower()


def test_offline_search_never_opens_a_network_connection(tmp_path):
    workspace = tmp_path / "workspace"
    script = """
import socket
import sys
def forbidden(*args, **kwargs):
    raise AssertionError("offline command attempted a network connection")
socket.socket.connect = forbidden
socket.create_connection = forbidden
from tychora.interfaces.cli import main
sys.argv = ['tychora', '--workspace', sys.argv[1], '--offline', 'search', 'synthetic lookup']
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(workspace)],
        cwd=tmp_path,
        env={**os.environ, "TYCHORA_MODEL_CACHE": str(tmp_path / "empty-models")},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stderr)["code"] == "CAPABILITY_MISSING"


@pytest.mark.parametrize(
    "selection",
    [
        ("--passage", "synthetic passage"),
        ("--image", "synthetic-image"),
        ("--request-file", "request.json"),
    ],
)
def test_offline_search_input_modes_fail_before_provider_use(tmp_path, selection):
    workspace = tmp_path / "workspace"
    _run("--workspace", workspace, "init", cwd=tmp_path)
    if selection[0] == "--request-file":
        (tmp_path / "request.json").write_text(
            json.dumps({"input": {"type": "passage", "text": "synthetic passage"}})
        )
    result = _run(
        "--workspace",
        workspace,
        "--offline",
        "search",
        *selection,
        cwd=tmp_path,
        env={"TYCHORA_MODEL_CACHE": str(tmp_path / "empty-models")},
    )
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "CAPABILITY_MISSING"
