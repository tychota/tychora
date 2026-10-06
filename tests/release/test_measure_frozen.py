"""Per-target binary measurements retain their exact artifact provenance."""

import json
import runpy
from pathlib import Path


def test_measurement_records_target_host_and_artifact_digest(monkeypatch, tmp_path):
    packaging = Path(__file__).parents[2] / "packaging"
    monkeypatch.syspath_prepend(str(packaging))
    namespace = runpy.run_path(str(packaging / "measure_frozen.py"))
    main = namespace["main"]
    main.__globals__["help_sample"] = lambda *args: {"elapsed_seconds": 0.1}
    main.__globals__["service_sample"] = lambda *args: {"ready_seconds": 0.2}
    artifact = tmp_path / "tychora"
    artifact.write_bytes(b"#!/bin/sh\nexit 0\n")
    artifact.chmod(0o755)
    output = tmp_path / "measure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "measure_frozen.py",
            "--artifact",
            str(artifact),
            "--output",
            str(output),
            "--target",
            "linux-x86_64",
        ],
    )
    main()
    report = json.loads(output.read_text())
    assert report["target"] == "linux-x86_64"
    assert (
        report["artifact_sha256"]
        == __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    )
    assert report["host_platform"]
    assert report["host_machine"]
