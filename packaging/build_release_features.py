"""Derive the frozen acceptance contract from the audited wheel inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    release = json.loads(args.manifest.read_text())
    if release["package"]["name"] != "tychora" or not release["features"]["full"]:
        raise ValueError("Expected an audited full Tychora wheel inventory")
    expected = {
        "schema_version": 1,
        "package": "tychora",
        "version": release["package"]["version"],
        "wheel_sha256": release["artifacts"]["wheel"]["sha256"],
        "features": release["features"],
        "resolved_versions": {
            row["name"]: row["version"] for row in release["resolved"]["full"]
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
