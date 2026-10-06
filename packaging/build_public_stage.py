"""Stage the public source allowlist, then build under macOS path denial."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_DIRS = ("src/tychora/", "tests/", "packaging/", "docs/", "examples/")
SOURCE_FILES = {".gitignore", "README.md", "LICENSE", "pyproject.toml", "uv.lock"}


def build_public_stage(
    repository: Path,
    source: Path,
    dist: Path,
    proof: Path,
    private: Path,
    uv: Path,
) -> None:
    repository = repository.resolve()
    source = source.resolve()
    if source.exists() and any(source.iterdir()):
        raise ValueError(f"Public stage must start empty: {source}")
    source.mkdir(parents=True, exist_ok=True)
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
    )
    for raw in names.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        if name not in SOURCE_FILES and not name.startswith(SOURCE_DIRS):
            continue
        original = repository / name
        if not original.is_file() or original.is_symlink():
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    if not (source / "packaging/sandbox_build.py").is_file():
        raise RuntimeError("Sandbox build driver is absent from public stage")
    if (source / ".superpowers").exists() or (source / "reports").exists():
        raise RuntimeError("Private source directory entered public stage")
    private.mkdir(parents=True, exist_ok=True)
    (private / "sentinel.txt").write_text("synthetic private build sentinel\n")
    common_git_dir = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repository, text=True
    ).strip()
    subprocess.run(
        [
            sys.executable,
            str(source / "packaging/sandbox_build.py"),
            "--source",
            str(source),
            "--checkout",
            str(repository),
            "--git-common-dir",
            str((repository / common_git_dir).resolve()),
            "--private",
            str(private),
            "--out-dir",
            str(dist.resolve()),
            "--proof",
            str(proof.resolve()),
            "--uv",
            str(uv),
        ],
        cwd=source.parent,
        check=True,
    )
    expected = {
        "checkout_denied": True,
        "git_common_dir_denied": True,
        "private_fixture_denied": True,
        "public_stage_readable": True,
        "build_under_same_profile": True,
    }
    if json.loads(proof.read_text()) != expected:
        raise RuntimeError("Build isolation proof is incomplete")
    if len(list(dist.glob("*.whl"))) != 1 or len(list(dist.glob("*.tar.gz"))) != 1:
        raise RuntimeError("Build did not produce exactly one wheel and sdist")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "source", "dist", "proof", "private", "uv"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    build_public_stage(
        args.repository, args.source, args.dist, args.proof, args.private, args.uv
    )


if __name__ == "__main__":
    main()
