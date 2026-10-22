"""Acceptance checks against a built executable, never an imported source tree."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
from mcp import Client, StdioServerParameters


def clean_env(
    cwd: Path, overrides: dict[str, str] | None = None, *, frozen: bool = True
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }
    env["PATH"] = "/usr/bin:/bin"
    if frozen:
        env["PYTHONHOME"] = str(cwd / "missing-python-home")
    env["PYTHONNOUSERSITE"] = "1"
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env.update(overrides or {})
    return env


def run(
    artifact: Path,
    *args: str,
    cwd: Path,
    overrides: dict[str, str] | None = None,
    frozen: bool = True,
    network_denied: bool = False,
    git_denied: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [str(artifact), *args]
    if (network_denied or git_denied) and sys.platform == "darwin":
        denials = []
        if network_denied:
            denials.append("(deny network*)")
        if git_denied:
            denials.append('(deny process-exec (literal "/usr/bin/git"))')
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)" + "".join(denials),
            *command,
        ]
    elif network_denied and sys.platform.startswith("linux"):
        command = linux_network_command(command)
    return subprocess.run(
        command,
        cwd=cwd,
        env=clean_env(cwd, overrides, frozen=frozen),
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )


def linux_network_command(command: list[str]) -> list[str]:
    """Run inside a private Linux user/network namespace; never silently fall back."""
    unshare = shutil.which("unshare")
    assert unshare is not None, "Linux network-denied acceptance requires unshare"
    return [unshare, "--user", "--map-root-user", "--net", "--", *command]


def verify_linux_network_denial(cwd: Path) -> None:
    """Prove the wrapper changes namespace and cannot reach a host listener."""
    if not sys.platform.startswith("linux"):
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host_namespace = os.readlink("/proc/self/ns/net")
        code = (
            "import os,socket,sys; "
            "print(os.readlink('/proc/self/ns/net'),flush=True); "
            "s=socket.socket(); s.settimeout(2); "
            "\ntry: s.connect(('127.0.0.1',int(sys.argv[1])))\n"
            "except OSError: print('DENIED',flush=True)\n"
            "else: print('CONNECTED',flush=True); sys.exit(3)"
        )
        probe = subprocess.run(
            linux_network_command(
                [sys.executable, "-c", code, str(listener.getsockname()[1])]
            ),
            cwd=cwd,
            env=clean_env(cwd, frozen=False),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    lines = probe.stdout.strip().splitlines()
    assert (
        probe.returncode == 0
        and len(lines) == 2
        and lines[0] != host_namespace
        and lines[1] == "DENIED"
    ), (
        f"Linux network-denial wrapper unavailable or ineffective: {probe.stderr[-1000:]} {probe.stdout[-500:]}"
    )


def checked(result: subprocess.CompletedProcess[str], label: str) -> str:
    assert result.returncode == 0, (
        f"{label} failed ({result.returncode}): {result.stderr[-3000:]}"
    )
    return result.stdout


def json_result(result: subprocess.CompletedProcess[str], label: str) -> dict:
    return json.loads(checked(result, label))


def install_location(artifact: Path, cwd: Path) -> None:
    """Exercise the final onefile from a read-only, symlinked install and temp path."""
    if artifact.parent.name == "tychora-onedir":
        return
    install = cwd / "read only install with spaces"
    install.mkdir()
    copied = install / artifact.name
    shutil.copy2(artifact, copied)
    copied.chmod(0o555)
    link = cwd / "linked executable"
    link.symlink_to(copied)
    temporary = cwd / "extraction temp with spaces"
    temporary.mkdir()
    temporary_link = cwd / "linked extraction temp"
    temporary_link.symlink_to(temporary, target_is_directory=True)
    install.chmod(0o555)
    try:
        env = {"TMPDIR": str(temporary_link)}
        probe = subprocess.Popen(
            [str(link), "--help"],
            cwd=cwd,
            env=clean_env(cwd, env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        observed_extraction = False
        deadline = time.monotonic() + 90
        while probe.poll() is None:
            observed_extraction |= bool(list(temporary.glob("_MEI*")))
            assert time.monotonic() < deadline, "Onefile extraction exceeded 90s"
            time.sleep(0.01)
        stdout, stderr = probe.communicate(timeout=10)
        assert probe.returncode == 0 and "Usage" in stdout, stderr[-1500:]
        assert observed_extraction, "Onefile ignored the selected temporary path"
        assert "Usage" in checked(
            run(link, "--help", cwd=cwd, overrides=env),
            "binary read-only symlink install help",
        )
        kinds = json_result(
            run(
                link,
                "--workspace",
                str(cwd / "install location knowledge"),
                "--offline",
                "inspect",
                "kinds",
                "--json",
                cwd=cwd,
                overrides=env,
            ),
            "binary read-only symlink install discovery",
        )
        assert kinds["subject"] == "kinds"
        assert not list(temporary.glob("_MEI*")), (
            "Frozen extraction remained in the selected temporary directory"
        )
    finally:
        install.chmod(0o755)


def http_and_delegation(
    artifact: Path, workspace: Path, cwd: Path, env: dict[str, str]
) -> None:
    log_path = cwd / "frozen-http.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [str(artifact), "--workspace", str(workspace), "serve", "--port", "0"],
            cwd=cwd,
            env=clean_env(cwd, env),
            stdout=log,
            stderr=log,
        )
        try:
            endpoint_path = workspace / ".runtime/endpoint.json"
            deadline = time.monotonic() + 90
            endpoint = {}
            while "authority" not in endpoint:
                assert process.poll() is None, (
                    "HTTP owner exited before publishing its endpoint"
                )
                assert time.monotonic() < deadline, (
                    "HTTP owner startup exceeded 90 seconds"
                )
                if endpoint_path.is_file():
                    endpoint = json.loads(endpoint_path.read_text())
                time.sleep(0.1)
            url = "http://" + endpoint["authority"] + "/v1/handshake"
            try:
                urllib.request.urlopen(url, timeout=10)
            except urllib.error.HTTPError as error:
                assert error.code == 403
                assert json.loads(error.read())["code"] == "FORBIDDEN_SCOPE"
            else:
                raise AssertionError("Unauthenticated HTTP request was accepted")
            request = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + endpoint["token"]}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                handshake = json.load(response)
            assert handshake["workspace_id"] == endpoint["workspace_id"]
            delegated = json_result(
                run(
                    artifact,
                    "--workspace",
                    str(workspace),
                    "inspect",
                    "kinds",
                    "--json",
                    cwd=cwd,
                    overrides=env,
                ),
                "binary sibling HTTP delegation",
            )
            assert delegated["subject"] == "kinds"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def external_pack_output(
    artifact: Path,
    wheel_cli: Path,
    workspace: Path,
    cwd: Path,
    model_bundle: str,
) -> None:
    """Compare public hierarchical evidence from the binary and installed wheel."""
    options = ("--workspace", str(workspace), "--offline")
    runtime_env = {"TYCHORA_MODEL_CACHE": str(cwd / "external-model-cache")}
    setup = json_result(
        run(
            artifact,
            *options,
            "manage",
            "setup",
            "--asset",
            "output-tokenizer",
            "--bundle",
            model_bundle,
            "--json",
            cwd=cwd,
            overrides=runtime_env,
        ),
        "binary external pack tokenizer setup",
    )
    assert setup["job"]["state"] == "succeeded"
    source = cwd / "external-standard.json"
    source.write_text(
        json.dumps(
            {
                "type": "text",
                "source_key": "synthetic-legacy-standard",
                "collection_id": "default",
                "kind": "legacy-standard@1",
                "format": "text",
                "text": "heritage standard evidence for frozen external pack",
                "metadata": {
                    "title": "Synthetic standard",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
    )
    added = json_result(
        run(
            artifact,
            *options,
            "add",
            "--request-file",
            str(source),
            "--json",
            cwd=cwd,
            overrides=runtime_env,
        ),
        "binary external kind ingestion",
    )
    assert added["state"] == "succeeded"

    def hierarchical(cli: Path, *, frozen: bool) -> dict:
        log_path = cwd / (
            "external-binary-http.log" if frozen else "external-wheel-http.log"
        )
        endpoint_path = workspace / ".runtime/endpoint.json"
        endpoint_path.unlink(missing_ok=True)
        with log_path.open("w+") as log:
            process = subprocess.Popen(
                [str(cli), "--workspace", str(workspace), "serve", "--port", "0"],
                cwd=cwd,
                env=clean_env(cwd, runtime_env, frozen=frozen),
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 90
                endpoint = {}
                while "authority" not in endpoint:
                    assert process.poll() is None, "External pack HTTP owner exited"
                    assert time.monotonic() < deadline, (
                        "External pack HTTP startup exceeded 90s"
                    )
                    if endpoint_path.is_file():
                        endpoint = json.loads(endpoint_path.read_text())
                    time.sleep(0.1)
                request = urllib.request.Request(
                    "http://" + endpoint["authority"] + "/v1/queries",
                    data=json.dumps(
                        {"question": "heritage", "modalities": ["lexical"]}
                    ).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer " + endpoint["token"],
                        "Accept": "application/vnd.tychora.hierarchical+json",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    assert response.headers.get_content_type() == (
                        "application/vnd.tychora.hierarchical+json"
                    )
                    return json.load(response)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    binary = hierarchical(artifact, frozen=True)
    wheel = hierarchical(wheel_cli, frozen=False)
    assert (
        binary["schema_version"]
        == wheel["schema_version"]
        == ("hierarchical-evidence@1")
    )
    assert binary["snapshot_id"] == wheel["snapshot_id"]
    assert binary["hierarchy"] == wheel["hierarchy"]
    assert binary["citations"] == wheel["citations"]
    standard = next(
        group for group in binary["hierarchy"] if group["lane_id"] == "standard"
    )
    matching = [
        segment
        for segment in standard["segments"]
        if segment["kind"] == "legacy-standard@1"
    ]
    assert matching and all(
        segment["text"] == "heritage standard evidence for frozen external pack"
        and segment["citations"]
        and all(
            citation["source_id"] and citation["unit_id"]
            for citation in segment["citations"]
        )
        for segment in matching
    )
    print("PASS: external kind ingestion and hierarchical binary/wheel parity")


def parser_cancellation(
    artifact: Path, workspace: Path, cwd: Path, source: Path, metadata: dict
) -> None:
    """Cancel an actual frozen PDF job after its parser child starts."""
    copied = cwd / "rich-cancellation.pdf"
    shutil.copy2(source, copied)
    log_path = cwd / "frozen-parser-cancellation.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [str(artifact), "--workspace", str(workspace), "serve", "--port", "0"],
            cwd=cwd,
            env=clean_env(
                cwd, {"TYCHORA_MODEL_CACHE": os.environ["TYCHORA_TEST_MODEL_CACHE"]}
            ),
            stdout=log,
            stderr=log,
        )
        try:
            endpoint_path = workspace / ".runtime/endpoint.json"
            deadline = time.monotonic() + 90
            endpoint = {}
            while "authority" not in endpoint:
                assert process.poll() is None, "Parser owner exited before startup"
                assert time.monotonic() < deadline, "Parser owner startup exceeded 90s"
                if endpoint_path.is_file():
                    endpoint = json.loads(endpoint_path.read_text())
                time.sleep(0.1)
            base = "http://" + endpoint["authority"]
            headers = {
                "Authorization": "Bearer " + endpoint["token"],
                "Content-Type": "application/json",
            }
            request = urllib.request.Request(
                base + "/v1/sources",
                headers=headers,
                data=json.dumps(
                    {
                        "type": "file",
                        "source_key": copied.name,
                        "collection_id": "default",
                        "kind": "manual@1",
                        "path": str(copied),
                        "metadata": metadata,
                    }
                ).encode(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                assert response.status == 202
                job_id = json.load(response)["id"]
            owner = psutil.Process(process.pid)
            deadline = time.monotonic() + 90
            child = None
            while child is None:
                assert process.poll() is None, (
                    "Parser owner exited before child startup"
                )
                assert time.monotonic() < deadline, "Frozen parser child did not start"
                for candidate in owner.children(recursive=True):
                    try:
                        if "--multiprocessing-fork" in candidate.cmdline():
                            child = candidate
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                time.sleep(0.05)
            try:
                open_paths = {item.path for item in child.open_files()}
            except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                raise AssertionError(
                    "Cannot inspect frozen parser file handles"
                ) from error
            assert not any(
                path.startswith(
                    (str(workspace / "corpus.sqlite3"), str(workspace / "indexes"))
                )
                for path in open_paths
            ), "Frozen parser child inherited a native store handle"
            cancellation = urllib.request.Request(
                base + f"/v1/jobs/{job_id}/cancel", headers=headers, method="POST"
            )
            with urllib.request.urlopen(cancellation, timeout=15) as response:
                assert response.status == 200
            deadline = time.monotonic() + 240
            while True:
                status_request = urllib.request.Request(
                    base + f"/v1/jobs/{job_id}", headers=headers
                )
                with urllib.request.urlopen(status_request, timeout=15) as response:
                    status = json.load(response)
                if status["state"] in {
                    "cancelled",
                    "failed",
                    "succeeded",
                    "interrupted",
                }:
                    break
                assert time.monotonic() < deadline, (
                    "Frozen parser cancellation did not settle"
                )
                time.sleep(0.25)
            assert status["state"] == "cancelled", status
            assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE, (
                "Frozen parser child survived settled cancellation"
            )
            assert not any(
                "--multiprocessing-fork" in candidate.cmdline()
                for candidate in owner.children(recursive=True)
            ), "Frozen owner retained a parser child after cancellation"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def canonical_root(workspace: Path) -> tuple:
    """Read an immutable synthetic fixture's published root without opening natives."""
    with sqlite3.connect(
        (workspace / "corpus.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as db:
        (snapshot_id,) = db.execute(
            "SELECT active_snapshot_id FROM workspaces"
        ).fetchone()
        assert snapshot_id
        root = db.execute(
            "SELECT snapshot_id, membership_sha, manifest_sha FROM snapshots "
            "WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        members = tuple(
            db.execute(
                "SELECT source_id, revision_id, extraction_id, is_default "
                "FROM snapshot_members WHERE snapshot_id=? "
                "ORDER BY source_id, revision_id, extraction_id",
                (snapshot_id,),
            )
        )
    assert root and members
    return root, members


def native_crash_cut(
    artifact: Path, cwd: Path, archive: Path, env: dict[str, str], slot: str
) -> None:
    """Kill a public rebuild only after observing its native store file open."""
    restored = cwd / f"{slot} crash recovery knowledge"
    json_result(
        run(
            artifact,
            "manage",
            "import",
            str(archive),
            str(restored),
            cwd=cwd,
            overrides=env,
        ),
        f"binary {slot} crash fixture restore",
    )
    options = ("--workspace", str(restored))
    before = json_result(
        run(
            artifact,
            *options,
            "manage",
            "health",
            "--verify-payloads",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        ),
        f"binary {slot} precrash health",
    )
    assert before["recovery"]["native_pending_batches"] == 2
    root_before = canonical_root(restored)
    command = [str(artifact), *options, "manage", "rebuild", "native", slot]
    if sys.platform == "darwin":
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            *command,
        ]
    elif sys.platform.startswith("linux"):
        command = linux_network_command(command)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=clean_env(cwd, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    owner = psutil.Process(process.pid)
    observed = None
    started = time.monotonic()
    while time.monotonic() - started < 90 and process.poll() is None:
        try:
            candidates = [owner, *owner.children(recursive=True)]
        except psutil.NoSuchProcess:
            break
        for candidate in candidates:
            try:
                paths = [item.path for item in candidate.open_files()]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            for path in paths:
                if (
                    slot == "text" and path.startswith(str(restored / "indexes/text"))
                ) or (slot == "graph" and path == str(restored / "indexes/graph")):
                    observed = (candidate.pid, path, time.monotonic() - started)
                    break
            if observed:
                break
        if observed:
            break
        time.sleep(0.01)
    if observed:
        os.killpg(process.pid, signal.SIGKILL)
    stdout, stderr = process.communicate(timeout=30)
    assert observed, (
        f"Binary {slot} rebuild finished without an observable native-open cut: "
        f"exit={process.returncode} out={stdout[-500:]} err={stderr[-500:]}"
    )
    assert process.returncode == -signal.SIGKILL
    after = json_result(
        run(
            artifact,
            *options,
            "manage",
            "health",
            "--verify-payloads",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        ),
        f"binary {slot} postcrash health",
    )
    assert canonical_root(restored) == root_before, (
        f"Binary {slot} crash changed the published canonical snapshot"
    )
    assert after["recovery"]["native_pending_batches"] == 2
    assert (
        after["recovery"]["verified_payloads"]
        == before["recovery"]["verified_payloads"]
    )
    rebuilt = json_result(
        run(
            artifact,
            *options,
            "manage",
            "rebuild",
            "native",
            "text",
            "graph",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        ),
        f"binary {slot} postcrash native rebuild",
    )
    assert rebuilt["recovery"]["native_pending_batches"] == 0
    assert canonical_root(restored) == root_before
    assert_native_reopened(
        artifact, cwd, restored, env, f"binary {slot} postcrash native reopen"
    )
    print(
        f"PASS: {slot} native-open SIGKILL at {observed[2]:.2f}s "
        f"pid={observed[0]} path={Path(observed[1]).name}; "
        "published root unchanged and native recovery complete"
    )


def assert_native_reopened(
    artifact: Path, cwd: Path, workspace: Path, env: dict[str, str], label: str
) -> None:
    """A fresh executable owner rereads native bytes and graph members."""
    result = json_result(
        run(
            artifact,
            "--workspace",
            str(workspace),
            "manage",
            "health",
            "--verify-native",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        ),
        label,
    )["recovery"]
    assert result["native_pending_batches"] == 0
    assert result["native_verified_snapshot_id"]
    assert result["native_verified_records"] > 0, result
    assert result["native_verified_generations"] > 0, result
    assert result["native_skipped_pending_batches"] == 0, result


def assert_missing_ready_native_is_read_only(
    artifact: Path, cwd: Path, workspace: Path, env: dict[str, str]
) -> None:
    """Missing ready files keep canonical access and are never recreated by health."""
    for slot, relative in (
        ("text", "indexes/text/chroma.sqlite3"),
        ("graph", "indexes/graph"),
    ):
        damaged = cwd / f"{slot} missing ready storage"
        shutil.copytree(workspace, damaged)
        physical = damaged / relative
        assert physical.is_file(), physical
        physical.unlink()
        root_before = canonical_root(damaged)
        kinds = json_result(
            run(
                artifact,
                "--workspace",
                str(damaged),
                "inspect",
                "kinds",
                "--json",
                cwd=cwd,
                overrides=env,
                network_denied=True,
            ),
            f"binary {slot} missing-native canonical inspection",
        )
        assert kinds["subject"] == "kinds"
        lexical_query = cwd / f"{slot} missing-native lexical query.json"
        lexical_query.write_text(
            json.dumps({"question": "Synthetic amber", "modalities": ["lexical"]})
        )
        lexical = json_result(
            run(
                artifact,
                "--workspace",
                str(damaged),
                "--offline",
                "search",
                "--request-file",
                str(lexical_query),
                "--json",
                cwd=cwd,
                overrides=env,
                network_denied=True,
            ),
            f"binary {slot} missing-native canonical lexical search",
        )
        assert lexical["segments"]
        archive = cwd / f"{slot} missing-native canonical export"
        json_result(
            run(
                artifact,
                "--workspace",
                str(damaged),
                "manage",
                "export",
                str(archive),
                cwd=cwd,
                overrides=env,
                network_denied=True,
            ),
            f"binary {slot} missing-native canonical export",
        )
        assert (archive / "backup-manifest.json").is_file()
        checked_native = run(
            artifact,
            "--workspace",
            str(damaged),
            "manage",
            "health",
            "--verify-native",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        )
        assert checked_native.returncode != 0
        assert f"Ready {slot} native storage is missing" in (
            checked_native.stderr + checked_native.stdout
        )
        assert not physical.exists(), "Native health recreated a missing ready store"
        assert canonical_root(damaged) == root_before
    from chromadb import PersistentClient
    from chromadb.config import Settings

    damaged = cwd / "text missing ready collection"
    shutil.copytree(workspace, damaged)
    client = PersistentClient(
        path=str(damaged / "indexes/text"),
        settings=Settings(anonymized_telemetry=False),
    )
    collections = client.list_collections()
    assert len(collections) == 1
    client.delete_collection(collections[0].name)
    root_before = canonical_root(damaged)
    kinds = json_result(
        run(
            artifact,
            "--workspace",
            str(damaged),
            "inspect",
            "kinds",
            "--json",
            cwd=cwd,
            overrides=env,
            network_denied=True,
        ),
        "binary missing collection canonical inspection",
    )
    assert kinds["subject"] == "kinds"
    result = run(
        artifact,
        "--workspace",
        str(damaged),
        "manage",
        "health",
        "--verify-native",
        cwd=cwd,
        overrides=env,
        network_denied=True,
    )
    assert result.returncode != 0
    assert "Ready text native collection is missing" in result.stderr + result.stdout
    assert client.list_collections() == [], "Native health recreated a ready collection"
    assert canonical_root(damaged) == root_before


def native_recovery(artifact: Path, cwd: Path, model_bundle: str) -> None:
    """The wheel prepares canonical input; every native recovery check uses the binary."""
    fixture = cwd / "synthetic native fixture"
    prepared = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("native_fixture.py")),
            "--root",
            str(fixture),
        ],
        cwd=cwd,
        env=clean_env(cwd, {"TYCHORA_TEST_MODEL_CACHE": model_bundle}, frozen=False),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr[-3000:]
    fields = dict(
        item.split("=", 1)
        for item in prepared.stdout.split("PREPARED ", 1)[-1].split()
        if "=" in item
    )
    assert "PREPARED installed=" in prepared.stdout, prepared.stdout[-3000:]
    assert set(fields.get("slots", "").split(",")) == {"lexical", "text", "graph"}, (
        prepared.stdout[-3000:]
    )
    native_env = {"TYCHORA_MODEL_CACHE": model_bundle}
    restored = cwd / "restored native knowledge"
    receipt = json_result(
        run(
            artifact,
            "manage",
            "import",
            str(fixture / "canonical-backup"),
            str(restored),
            cwd=cwd,
            overrides=native_env,
        ),
        "binary graph/vector canonical restore",
    )
    assert receipt["deletion_suspended"] is True
    args = ("--workspace", str(restored))
    before = json_result(
        run(
            artifact,
            *args,
            "manage",
            "health",
            "--verify-payloads",
            cwd=cwd,
            overrides=native_env,
            network_denied=True,
        ),
        "binary pending graph/vector health",
    )
    assert before["recovery"]["native_pending_batches"] == 2
    rebuilt = json_result(
        run(
            artifact,
            *args,
            "manage",
            "rebuild",
            "native",
            "text",
            "graph",
            cwd=cwd,
            overrides=native_env,
            network_denied=True,
        ),
        "binary graph/vector rebuild",
    )
    assert rebuilt["recovery"]["native_pending_batches"] == 0
    reopened = json_result(
        run(
            artifact,
            *args,
            "manage",
            "health",
            "--verify-payloads",
            cwd=cwd,
            overrides=native_env,
            network_denied=True,
        ),
        "binary graph/vector reopen health",
    )
    assert reopened["recovery"]["native_pending_batches"] == 0
    assert reopened["recovery"]["verified_payloads"] >= 10
    assert (restored / "indexes/text").exists()
    assert (restored / "indexes/graph").exists()
    assert_native_reopened(
        artifact, cwd, restored, native_env, "binary graph/vector native reopen"
    )
    assert_missing_ready_native_is_read_only(artifact, cwd, restored, native_env)
    native_crash_cut(artifact, cwd, fixture / "canonical-backup", native_env, "text")
    native_crash_cut(artifact, cwd, fixture / "canonical-backup", native_env, "graph")


async def stdio_framing(
    artifact: Path, workspace: Path, cwd: Path, env: dict[str, str]
) -> None:
    params = StdioServerParameters(
        command=str(artifact),
        args=["--workspace", str(workspace), "serve", "--stdio"],
        env=clean_env(cwd, env),
    )
    async with Client(params, mode="legacy") as mcp:
        result = await mcp.call_tool("discover", {"request": {"subject": "kinds"}})
        assert result.structured_content["subject"] == "kinds"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    assert artifact.is_file(), f"Final executable is missing: {artifact}"
    assert os.access(artifact, os.X_OK), (
        f"Final executable is not executable: {artifact}"
    )
    features = json.loads(args.features.read_text())
    assert features["schema_version"] == 1
    assert features["package"] == "tychora"
    assert features["features"]
    assert set(features["features"]) == {
        "documents",
        "code",
        "vector",
        "graph",
        "voyage",
        "vision",
        "server",
        "full",
    }
    wheel_cli = Path(__file__).parents[1] / ".venv/bin/tychora"
    assert wheel_cli.is_file(), "Installed release wheel CLI is required for parity"
    with tempfile.TemporaryDirectory(prefix="tychora-frozen-") as raw:
        clean = Path(raw).resolve() / "working directory with spaces"
        clean.mkdir()
        verify_linux_network_denial(clean)
        help_text = checked(run(artifact, "--help", cwd=clean), "binary help")
        assert "Usage" in help_text
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            no_network = run(artifact, "--help", cwd=clean, network_denied=True)
            assert checked(no_network, "network-denied binary help") == help_text
        assert (
            checked(run(wheel_cli, "--help", cwd=clean, frozen=False), "wheel help")
            == help_text
        )
        install_location(artifact, clean)
        workspace = clean / "synthetic knowledge"
        options = ("--workspace", str(workspace), "--offline")
        binary_kinds = json_result(
            run(artifact, *options, "inspect", "kinds", "--json", cwd=clean),
            "binary kinds",
        )
        wheel_kinds = json_result(
            run(
                wheel_cli,
                *options,
                "inspect",
                "kinds",
                "--json",
                cwd=clean,
                frozen=False,
            ),
            "wheel kinds",
        )
        assert binary_kinds == wheel_kinds, (
            "Pack/schema discovery differs from the installed wheel"
        )
        settings = json_result(
            run(artifact, *options, "inspect", "settings", "--json", cwd=clean),
            "binary settings",
        )
        wheel_settings = json_result(
            run(
                wheel_cli,
                *options,
                "inspect",
                "settings",
                "--json",
                cwd=clean,
                frozen=False,
            ),
            "wheel settings",
        )
        assert settings["capability_status"] == wheel_settings["capability_status"]
        expected = {
            "chromadb": "chromadb",
            "docling": "docling",
            "ladybug": "ladybug",
            "tree-sitter": "tree-sitter",
            "voyageai": "voyageai",
            "markdown-it-py": "markdown-it-py",
        }
        statuses = {item["name"]: item for item in settings["capability_status"]}
        for capability, distribution in expected.items():
            assert statuses[capability]["installed"] is True
            assert (
                statuses[capability]["version"]
                == features["resolved_versions"][distribution]
            )
        initialized = json_result(
            run(artifact, *options, "init", "--json", cwd=clean), "binary init"
        )
        assert initialized["provider"] == "local"
        assert (workspace / "corpus.sqlite3").is_file()
        model_bundle = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
        assert model_bundle and Path(model_bundle).is_dir(), (
            "Set TYCHORA_TEST_MODEL_CACHE to the verified public bundle"
        )
        runtime_env = {"TYCHORA_MODEL_CACHE": str(clean / "model-cache")}
        setup = json_result(
            run(
                artifact,
                *options,
                "manage",
                "setup",
                "--asset",
                "output-tokenizer",
                "--bundle",
                model_bundle,
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary tokenizer setup",
        )
        assert setup["job"]["state"] == "succeeded"
        request = clean / "synthetic-markdown.json"
        request.write_text(
            json.dumps(
                {
                    "type": "text",
                    "source_key": "synthetic-manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "markdown",
                    "text": "# Verification\n\nSynthetic release evidence retains provenance.",
                    "metadata": {
                        "title": "Verification",
                        "publisher": "Example",
                        "edition": "1",
                    },
                }
            )
        )
        added = json_result(
            run(
                artifact,
                *options,
                "add",
                "--request-file",
                str(request),
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary Markdown ingestion",
        )
        assert added["state"] == "succeeded"
        query = clean / "query.json"
        query.write_text(
            json.dumps({"question": "release evidence", "modalities": ["lexical"]})
        )
        found = json_result(
            run(
                artifact,
                *options,
                "search",
                "--request-file",
                str(query),
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary lexical search",
        )
        assert found["segments"], "Markdown evidence was not retrieved"
        backup = clean / "canonical-backup"
        exported = json_result(
            run(
                artifact,
                *options,
                "manage",
                "export",
                str(backup),
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary canonical export",
        )
        assert exported
        shutil.rmtree(workspace)
        request.unlink()
        restored = clean / "restored knowledge"
        receipt = json_result(
            run(
                artifact,
                *options,
                "manage",
                "import",
                str(backup),
                str(restored),
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary canonical restore",
        )
        assert receipt["workspace_id"]
        reopened = json_result(
            run(
                artifact,
                "--workspace",
                str(restored),
                "--offline",
                "search",
                "--request-file",
                str(query),
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary restored search",
        )
        assert reopened["segments"], "Restored canonical evidence is unavailable"
        http_and_delegation(artifact, restored, clean, runtime_env)
        asyncio.run(stdio_framing(artifact, restored, clean, runtime_env))
        external_workspace = clean / "external pack knowledge"
        external_workspace.mkdir()
        pack = external_workspace / "profiles/workbench"
        shutil.copytree(Path(__file__).parents[1] / "examples/profiles/workbench", pack)
        external_config = external_workspace / "workspace.toml"
        external_config.write_text(
            'schema_version=1\n[profiles.workbench]\npath="profiles/workbench"\n'
            '[defaults]\nprofile="workbench"\n'
        )
        external_options = ("--workspace", str(external_workspace), "--offline")
        json_result(
            run(
                artifact,
                *external_options,
                "init",
                "--provider",
                "configured",
                "--config",
                str(external_config),
                "--json",
                cwd=clean,
            ),
            "binary external pack init",
        )
        binary_config = json_result(
            run(artifact, *external_options, "inspect", "config", "--json", cwd=clean),
            "binary external pack configuration",
        )
        wheel_config = json_result(
            run(
                wheel_cli,
                *external_options,
                "inspect",
                "config",
                "--json",
                cwd=clean,
                frozen=False,
            ),
            "wheel external pack configuration",
        )
        assert binary_config == wheel_config
        assert "hierarchical-evidence@1" in json.dumps(binary_config)
        external_pack_output(
            artifact, wheel_cli, external_workspace, clean, model_bundle
        )
        repository = clean / "synthetic repository"
        repository.mkdir()
        (repository / "module.py").write_text(
            "def retained_symbol(value: int) -> int:\n    return value + 1\n"
        )
        (repository / "module.ts").write_text(
            "export function retainedCode(value: number): number { return value + 1; }\n"
        )
        subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "."], check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        code_workspace = clean / "code knowledge"
        code_workspace.mkdir()
        code_config = code_workspace / "workspace.toml"
        code_config.write_text(
            'schema_version=1\n[profiles.code]\nbuiltin="code@1"\n'
            '[defaults]\nprofile="code"\n[configuration.acquisition]\nroots='
            + json.dumps([str(clean)])
            + "\n"
        )
        code_options = ("--workspace", str(code_workspace), "--offline")
        json_result(
            run(
                artifact,
                *code_options,
                "init",
                "--provider",
                "configured",
                "--config",
                str(code_config),
                "--json",
                cwd=clean,
            ),
            "binary native code workspace init",
        )
        code_request = clean / "code-source.json"
        code_request.write_text(
            json.dumps(
                {
                    "type": "repository",
                    "path": str(repository),
                    "kind": "code_file@1",
                    "metadata": {"title": "Synthetic Repository"},
                    "source_key": "repository",
                    "collection_id": "default",
                }
            )
        )
        code_added = json_result(
            run(
                artifact,
                *code_options,
                "add",
                "--request-file",
                str(code_request),
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary Git/Tree-sitter ingestion and native projection",
        )
        assert code_added["state"] == "succeeded"
        assert (code_workspace / "indexes/graph").exists(), (
            "Ladybug graph was not opened"
        )
        reopened_code = json_result(
            run(
                artifact,
                *code_options,
                "inspect",
                "kinds",
                "--json",
                cwd=clean,
                overrides=runtime_env,
            ),
            "binary native graph/vector reopen",
        )
        assert reopened_code["subject"] == "kinds"
        if sys.platform == "darwin":
            unavailable_git = run(
                artifact,
                *code_options,
                "add",
                "--request-file",
                str(code_request),
                "--json",
                cwd=clean,
                overrides=runtime_env,
                git_denied=True,
            )
            assert unavailable_git.returncode != 0
            assert "CAPABILITY_MISSING" in unavailable_git.stderr, (
                "Missing system Git did not fail the repository operation: "
                + unavailable_git.stderr[-1500:]
            )
            available_kinds = json_result(
                run(
                    artifact,
                    *code_options,
                    "inspect",
                    "kinds",
                    "--json",
                    cwd=clean,
                    overrides=runtime_env,
                    git_denied=True,
                ),
                "binary nonrepository operation with Git denied",
            )
            assert available_kinds["subject"] == "kinds"
        native_recovery(artifact, clean, model_bundle)
        fixtures = Path(__file__).parents[1] / "tests/fixtures/documents"
        builtin_pack = Path(__file__).parents[1] / "src/tychora/profiles/documents"
        rich_workspace = clean / "rich document knowledge"
        rich_workspace.mkdir()
        pack = rich_workspace / "profiles/documents"
        shutil.copytree(builtin_pack, pack)
        with (pack / "profile.toml").open("a") as stream:
            stream.write('\n[extraction]\nparser="docling"\n')
        config = rich_workspace / "workspace.toml"
        config.write_text(
            'schema_version=1\n[profiles.rich]\npath="profiles/documents"\n'
            '[defaults]\nprofile="rich"\n[configuration.acquisition]\nroots='
            + json.dumps([str(clean)])
            + "\n"
        )
        rich_options = ("--workspace", str(rich_workspace), "--offline")
        json_result(
            run(
                artifact,
                *rich_options,
                "init",
                "--provider",
                "configured",
                "--config",
                str(config),
                "--json",
                cwd=clean,
            ),
            "binary rich workspace init",
        )
        metadata = clean / "manual-metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "title": "Operations Manual",
                    "publisher": "Example Team",
                    "edition": "1",
                }
            )
        )
        for suffix in ("pdf", "docx"):
            source = clean / ("rich." + suffix)
            shutil.copy2(fixtures / source.name, source)
            result = json_result(
                run(
                    artifact,
                    *rich_options,
                    "add",
                    str(source),
                    "--kind",
                    "manual@1",
                    "--metadata-file",
                    str(metadata),
                    "--json",
                    cwd=clean,
                    overrides={"TYCHORA_MODEL_CACHE": model_bundle},
                ),
                f"binary {suffix} ingestion",
            )
            assert result["state"] == "succeeded", f"{suffix} parser job failed"
        with sqlite3.connect(
            (rich_workspace / "corpus.sqlite3").as_uri() + "?mode=ro", uri=True
        ) as db:
            figure_assets = {
                suffix: db.execute(
                    "SELECT a.asset_id,a.asset_type,a.caption FROM assets a "
                    "JOIN extractions e USING(extraction_id) "
                    "JOIN revisions r USING(revision_id) "
                    "JOIN sources s USING(source_id) WHERE s.source_key=? "
                    "ORDER BY a.asset_id",
                    ("rich." + suffix,),
                ).fetchall()
                for suffix in ("pdf", "docx")
            }
        for suffix, assets in figure_assets.items():
            images = [item for item in assets if item[1] == "picture"]
            assert images, f"Binary {suffix} ingestion retained no figure image"
            asset_id = images[0][0]
            inspected = json_result(
                run(
                    artifact,
                    *rich_options,
                    "inspect",
                    "assets",
                    asset_id,
                    "--json",
                    cwd=clean,
                    overrides={"TYCHORA_MODEL_CACHE": model_bundle},
                ),
                f"binary {suffix} figure inspection",
            )
            assert inspected["subject"] == "assets" and inspected["id"] == asset_id
            assert inspected["media_type"].startswith("image/")
            assert inspected["records"][0]["record_type"] == "asset_locator"
        parser_cancellation(
            artifact,
            rich_workspace,
            clean,
            clean / "rich.pdf",
            json.loads(metadata.read_text()),
        )
    print(
        "PASS: final executable wheel parity, native restore/rebuild/crash recovery, "
        "Markdown/PDF/DOCX/figure/code ingestion, parser cancellation, search, "
        "HTTP delegation and MCP stdio"
    )


if __name__ == "__main__":
    main()
