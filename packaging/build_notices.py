"""Copy installed distribution notice texts into a verifiable binary payload."""

from __future__ import annotations

import argparse
import json
import shutil
from hashlib import sha256
from importlib import metadata
from pathlib import Path


def _is_notice(path: Path) -> bool:
    name = path.name.upper()
    return name.startswith(("LICENSE", "LICENCE", "NOTICE", "COPYING"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    entries: list[dict[str, str]] = []
    for distribution in sorted(
        metadata.distributions(), key=lambda item: item.metadata["Name"].lower()
    ):
        name = distribution.metadata["Name"].lower().replace("_", "-")
        for recorded in distribution.files or ():
            relative = Path(str(recorded))
            if (
                not _is_notice(relative)
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                continue
            source = Path(distribution.locate_file(recorded))
            if not source.is_file():
                continue
            destination = root / name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = source.read_bytes()
            destination.write_bytes(payload)
            entries.append(
                {
                    "distribution": name,
                    "version": distribution.version,
                    "path": destination.relative_to(root).as_posix(),
                    "sha256": sha256(payload).hexdigest(),
                }
            )
    tokenizers = Path(__file__).parent / "notices/tokenizers-0.23.2-LICENSE"
    if metadata.version("tokenizers") != "0.23.2" or not tokenizers.is_file():
        raise RuntimeError("Exact tokenizers 0.23.2 upstream license is required")
    destination = root / "tokenizers" / tokenizers.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(tokenizers.read_bytes())
    entries.append(
        {
            "distribution": "tokenizers",
            "version": "0.23.2",
            "path": destination.relative_to(root).as_posix(),
            "sha256": sha256(destination.read_bytes()).hexdigest(),
        }
    )
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "notices": entries}, indent=2, sort_keys=True)
        + "\n"
    )
    print(f"Copied {len(entries)} notice texts from installed distributions")


if __name__ == "__main__":
    main()
