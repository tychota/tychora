"""Explicitly provision and verify pinned public assets for hosted test jobs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SELECTIONS = frozenset(
    {"documents", "ocr", "output-tokenizer", "voyage-context-4", "rerank-2.5"}
)


CODE_CORPUS_COMMIT = "700df656fb391aa5eaa41a75ce27eed432be442c"
CODE_CORPUS_TREE = "34fa1cac9248f208c5dfaa34347674a4f6d6d26b"


def provision_code_corpus(
    repository: Path,
    destination: Path,
    *,
    commit: str = CODE_CORPUS_COMMIT,
    tree: str = CODE_CORPUS_TREE,
) -> Path:
    """Export src/tychora at the pinned commit; Git verifies the bytes by tree id."""

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(repository), *args], capture_output=True, check=False
        )

    missing = git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0
    if missing and git("fetch", "--quiet", "--depth", "1", "origin", commit).returncode:
        raise SystemExit(f"cannot fetch the pinned code corpus commit {commit}")
    actual = git("rev-parse", f"{commit}:src/tychora").stdout.decode().strip()
    if actual != tree:
        raise SystemExit(
            f"code corpus tree {actual} differs from the pinned tree {tree}"
        )
    archive = git("archive", "--format=tar", commit, "src/tychora")
    if archive.returncode != 0:
        raise SystemExit(f"cannot export the code corpus: {archive.stderr.decode()}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(destination, filter="data")
    return destination


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


def _retryable_download_failure(stderr: str) -> bool:
    for line in reversed(stderr.splitlines()):
        try:
            error = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(error, dict):
            return (
                error.get("schema_version") == "1"
                and error.get("code") == "PROVIDER_FAILURE"
                and error.get("retryable") is True
            )
    return False


def run_setup(command: list[str], env: dict[str, str]) -> None:
    """Retry only an explicitly transient public asset download failure."""
    for attempt in range(1, 4):
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, check=False
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode == 0:
            return
        if attempt == 3 or not _retryable_download_failure(result.stderr):
            result.check_returncode()
        print(f"Retrying public asset setup ({attempt + 1}/3)", file=sys.stderr)
        time.sleep(5 * attempt)


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
    parser.add_argument(
        "--code-corpus",
        type=Path,
        help="Export the pinned code gold-set corpus (Tychora's own source) here",
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
        "--asset",
        "voyage-context-4",
        "--asset",
        "rerank-2.5",
        "--json",
    ]
    if args.bundle:
        command.extend(("--bundle", str(args.bundle)))
    run_setup(command, env)
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
    if args.code_corpus:
        lines["TYCHORA_TEST_CODE_CORPUS"] = provision_code_corpus(
            Path(__file__).parents[1], args.code_corpus
        )
    with args.github_env.open("a") as stream:
        for key, value in lines.items():
            stream.write(f"{key}={value}\n")
    print(f"Verified {len(verified)} pinned public model/tokenizer files")


if __name__ == "__main__":
    main()
