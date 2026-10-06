"""Measure a local onefile's process startup, extraction and warm HTTP service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil
from test_frozen import clean_env


def rss_tree(pid: int) -> int:
    try:
        owner = psutil.Process(pid)
        processes = [owner, *owner.children(recursive=True)]
    except psutil.NoSuchProcess:
        return 0
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def disk_bytes(root: Path) -> tuple[int, int]:
    logical = allocated = 0
    for directory, _, files in os.walk(root):
        for name in files:
            info = (Path(directory) / name).stat()
            logical += info.st_size
            allocated += info.st_blocks * 512
    return logical, allocated


def help_sample(artifact: Path, cwd: Path, env: dict[str, str]) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(
        [str(artifact), "--help"],
        cwd=cwd,
        env=clean_env(cwd, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak = 0
    while process.poll() is None:
        peak = max(peak, rss_tree(process.pid))
        assert time.monotonic() - started < 120, "Executable help exceeded 120s"
        time.sleep(0.02)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0 and "Usage" in stdout, stderr[-1500:]
    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "peak_aggregate_rss_bytes": peak,
    }


def service_sample(artifact: Path, cwd: Path, env: dict[str, str]) -> dict:
    workspace = cwd / "service knowledge"
    init = subprocess.run(
        [str(artifact), "--workspace", str(workspace), "init", "--json"],
        cwd=cwd,
        env=clean_env(cwd, env),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert init.returncode == 0, init.stderr[-1500:]
    started = time.monotonic()
    process = subprocess.Popen(
        [str(artifact), "--workspace", str(workspace), "serve", "--port", "0"],
        cwd=cwd,
        env=clean_env(cwd, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak = 0
    try:
        endpoint_path = workspace / ".runtime/endpoint.json"
        deadline = started + 120
        endpoint = {}
        while "authority" not in endpoint:
            assert process.poll() is None, "Service exited during startup"
            assert time.monotonic() < deadline, "Service startup exceeded 120s"
            peak = max(peak, rss_tree(process.pid))
            if endpoint_path.is_file():
                endpoint = json.loads(endpoint_path.read_text())
            time.sleep(0.02)
        ready = time.monotonic()
        request = urllib.request.Request(
            "http://" + endpoint["authority"] + "/v1/handshake",
            headers={"Authorization": "Bearer " + endpoint["token"]},
        )
        warm = []
        for _ in range(5):
            called = time.monotonic()
            with urllib.request.urlopen(request, timeout=10) as response:
                assert json.load(response)["workspace_id"] == endpoint["workspace_id"]
            warm.append(round(time.monotonic() - called, 4))
            peak = max(peak, rss_tree(process.pid))
        extracted = list(Path(env["TMPDIR"]).glob("_MEI*"))
        assert len(extracted) == 1, "Expected one active onefile extraction"
        logical, allocated = disk_bytes(extracted[0])
        return {
            "ready_seconds": round(ready - started, 3),
            "warm_handshake_seconds": warm,
            "peak_aggregate_rss_bytes": peak,
            "extracted_logical_bytes": logical,
            "extracted_allocated_bytes": allocated,
        }
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    assert artifact.is_file() and os.access(artifact, os.X_OK)
    with artifact.open("rb") as stream:
        artifact_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    with tempfile.TemporaryDirectory(prefix="tychora-onefile-measure-") as raw:
        cwd = Path(raw).resolve()
        temporary = cwd / "onefile temp with spaces"
        temporary.mkdir()
        env = {"TMPDIR": str(temporary)}
        report = {
            "target": args.target,
            "host_platform": platform.platform(),
            "host_system": platform.system(),
            "host_release": platform.release(),
            "host_machine": platform.machine(),
            "artifact": artifact.name,
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": artifact_sha256,
            "measurement": "new process and extraction; filesystem cache not flushed",
            "help_samples": [help_sample(artifact, cwd, env) for _ in range(3)],
            "service": service_sample(artifact, cwd, env),
        }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
