"""Explicitly provision and verify pinned public assets for hosted test jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

SELECTIONS = frozenset({"documents", "ocr", "output-tokenizer"})


def verify_selected(cache: Path, manifest: Path) -> dict[str, Path]:
    assets = json.loads(manifest.read_text())["assets"]
    verified: dict[str, Path] = {}
    for asset in assets:
        if asset["selection"] not in SELECTIONS:
            continue
        relative = Path(asset["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe manifest asset path: {relative}")
        path = cache / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Pinned public asset missing: {relative}")
        data = path.read_bytes()
        if (
            len(data) != asset["size_bytes"]
            or hashlib.sha256(data).hexdigest() != asset["sha256"]
        ):
            raise ValueError(f"Pinned public asset checksum mismatch: {relative}")
        verified[asset["id"]] = path
    if not verified or "cl100k-base-ranks" not in verified:
        raise ValueError("Pinned tokenizer asset is absent from manifest")
    return verified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    parser.add_argument(
        "--bundle",
        type=Path,
        help="Existing verified local bundle for offline validation",
    )
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    (args.workspace / "workspace.toml").write_text(
        'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
        '[defaults]\nprofile = "local"\n[configuration]\noffline = false\n'
    )
    args.cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TYCHORA_MODEL_CACHE=str(args.cache))
    command = [
        "uv",
        "run",
        "--locked",
        "tychora",
        "--workspace",
        str(args.workspace),
        "manage",
        "setup",
        "--asset",
        "documents",
        "--asset",
        "ocr",
        "--asset",
        "output-tokenizer",
        "--json",
    ]
    if args.bundle:
        command.extend(("--bundle", str(args.bundle)))
    subprocess.run(command, env=env, check=True)
    manifest = Path(__file__).parents[1] / "src/tychora/resources/model-assets.json"
    verified = verify_selected(args.cache, manifest)
    tokenizer_dir = args.cache.parent / "tokenizer-test-assets"
    tokenizer_dir.mkdir(exist_ok=True)
    shutil.copyfile(
        verified["cl100k-base-ranks"], tokenizer_dir / "cl100k_base.tiktoken"
    )
    lines = {
        "TYCHORA_MODEL_CACHE": args.cache,
        "TYCHORA_TEST_MODEL_CACHE": args.cache,
        "TYCHORA_TEST_DOCLING_MODELS": args.cache,
        "TYCHORA_TEST_OCR_MODELS": args.cache / "rapidocr/3.9.2",
        "TYCHORA_TEST_TOKENIZER_ASSETS": tokenizer_dir,
    }
    with args.github_env.open("a") as stream:
        for key, value in lines.items():
            stream.write(f"{key}={value}\n")
    print(f"Verified {len(verified)} pinned public model/tokenizer files")


if __name__ == "__main__":
    main()
