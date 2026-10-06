"""Reject unexpected distribution members and sensitive release material.

Run before installing or publishing either artifact. Errors identify a member or
category, never the matching content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple


class ArtifactAudit(NamedTuple):
    path: Path
    kind: str
    members: tuple[str, ...]
    sha256: str
    member_sha256: dict[str, str]


_DENIED_PARTS = frozenset(
    {
        ".git",
        ".superpowers",
        ".venv",
        ".cache",
        ".huggingface",
        ".torch",
        "reports",
        "models",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".ty",
    }
)
_DENIED_SUFFIXES = (
    ".sqlite3",
    ".db",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".pyc",
    ".pickle",
    ".pkl",
    ".onnx",
    ".safetensors",
    ".bin",
)
_SENSITIVE = re.compile(
    rb"/(?:Users|home)/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})"
)


def _allowed_path(path: PurePosixPath, kind: str) -> bool:
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return False
    if kind == "sdist" and len(path.parts) == 2 and path.name == ".gitignore":
        return re.fullmatch(r"tychora-[^/]+", path.parts[0]) is not None
    if any(part.startswith(".") or part in _DENIED_PARTS for part in path.parts):
        return False
    if path.name.lower().endswith(_DENIED_SUFFIXES) or path.name.lower() in {
        ".env",
        "id_rsa",
        "id_ed25519",
    }:
        return False
    parts = path.parts
    if kind == "wheel":
        if parts[0] == "tychora" and len(parts) > 1:
            return True
        return (
            len(parts) >= 2
            and re.fullmatch(r"tychora-[^/]+\.dist-info", parts[0]) is not None
        )
    if len(parts) < 2 or re.fullmatch(r"tychora-[^/]+", parts[0]) is None:
        return False
    rel = parts[1:]
    if (
        rel[0] in {"README.md", "LICENSE", "pyproject.toml", "uv.lock", "PKG-INFO"}
        and len(rel) == 1
    ):
        return True
    if len(rel) >= 3 and rel[:2] == ("src", "tychora"):
        return True
    return len(rel) >= 2 and rel[0] in {"docs", "examples", "tests", "packaging"}


def _validate_member(name: str, data: bytes, kind: str) -> str:
    path = PurePosixPath(name)
    if not _allowed_path(path, kind) or "\\" in name:
        raise ValueError(f"Unexpected distribution member: {name}")
    if _SENSITIVE.search(data):
        raise ValueError(f"Sensitive content in distribution member: {name}")
    return hashlib.sha256(data).hexdigest()


def check_artifact(path: Path) -> ArtifactAudit:
    """Inspect every wheel or sdist member before a clean installation."""
    path = Path(path)
    kind = (
        "wheel"
        if path.suffix == ".whl"
        else "sdist"
        if path.name.endswith(".tar.gz")
        else ""
    )
    if not kind:
        raise ValueError("Expected a wheel or .tar.gz sdist")
    hashes: dict[str, str] = {}
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                mode = stat.S_IFMT(member.external_attr >> 16)
                if (
                    member.is_dir()
                    or member.filename in hashes
                    or mode not in (0, stat.S_IFREG)
                ):
                    raise ValueError(
                        f"Unexpected distribution member: {member.filename}"
                    )
                hashes[member.filename] = _validate_member(
                    member.filename, archive.read(member), kind
                )
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or member.name in hashes:
                    raise ValueError(f"Unexpected distribution member: {member.name}")
                source = archive.extractfile(member)
                assert source is not None
                hashes[member.name] = _validate_member(member.name, source.read(), kind)
    if kind == "wheel" and not any(
        name.endswith(".dist-info/METADATA") for name in hashes
    ):
        raise ValueError("Wheel has no package metadata")
    if kind == "sdist" and not any(name.endswith("/pyproject.toml") for name in hashes):
        raise ValueError("Sdist has no build metadata")
    return ArtifactAudit(
        path,
        kind,
        tuple(sorted(hashes)),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        hashes,
    )


def audit_outgoing_history(repo: Path, base: str) -> tuple[str, ...]:
    """Check every changed path and blob in commits after a local base."""
    repo = Path(repo)
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    commits = subprocess.check_output(
        ["git", "rev-list", "--reverse", "--topo-order", f"{base}..HEAD"],
        cwd=repo,
        text=True,
    ).splitlines()
    checked: list[str] = []
    scanned_blobs: set[str] = set()
    for commit in commits:
        parents = subprocess.check_output(
            ["git", "rev-list", "--parents", "-n", "1", commit],
            cwd=repo,
            text=True,
        ).split()[1:]
        for parent in parents:
            changes = subprocess.check_output(
                [
                    "git",
                    "diff-tree",
                    "-r",
                    "--no-renames",
                    "--raw",
                    "-z",
                    parent,
                    commit,
                ],
                cwd=repo,
            ).split(b"\0")
            for index in range(0, len(changes) - 1, 2):
                metadata, raw_name = changes[index : index + 2]
                if not metadata:
                    continue
                name = raw_name.decode("utf-8", "surrogateescape")
                member = PurePosixPath(name)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or any(part in _DENIED_PARTS for part in member.parts)
                    or member.name.lower().endswith(_DENIED_SUFFIXES)
                    or member.name == ".env"
                ):
                    raise ValueError(f"Unexpected outgoing history member: {name}")
                checked.append(name)
                new_oid = metadata.split()[3].decode("ascii")
                if new_oid == "0" * len(new_oid) or new_oid in scanned_blobs:
                    continue
                data = subprocess.check_output(
                    ["git", "cat-file", "blob", new_oid], cwd=repo
                )
                if _SENSITIVE.search(data):
                    raise ValueError(
                        f"Sensitive content in outgoing history member: {name}"
                    )
                scanned_blobs.add(new_oid)
    return tuple(sorted(set(checked)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path)
    parser.add_argument("--history-base")
    args = parser.parse_args()
    for path in args.artifacts:
        audit = check_artifact(path)
        print(
            json.dumps(
                {
                    "path": str(path),
                    "kind": audit.kind,
                    "sha256": audit.sha256,
                    "members": len(audit.members),
                },
                sort_keys=True,
            )
        )
    if args.history_base:
        checked = audit_outgoing_history(Path.cwd(), args.history_base)
        print(
            json.dumps(
                {"history_base": args.history_base, "checked_paths": len(checked)},
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
