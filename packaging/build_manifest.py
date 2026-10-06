"""Generate a local release inventory from inspected artifacts and installed venvs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

from check_artifact import check_artifact


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed(python: Path) -> list[dict]:
    code = """import hashlib, importlib.metadata as metadata, json
rows = []
for dist in metadata.distributions():
    info = dist.metadata
    name = info.get("Name", "")
    if not name: continue
    license_name = info.get("License-Expression") or info.get("License") or "UNKNOWN"
    notices = []
    for file in dist.files or ():
        path = str(file)
        if "license" not in path.lower() and "notice" not in path.lower() and "copying" not in path.lower():
            continue
        actual = dist.locate_file(file)
        if actual.is_file():
            notices.append({"path": path, "sha256": hashlib.sha256(actual.read_bytes()).hexdigest()})
    rows.append({"name": name.lower().replace("_", "-"), "version": dist.version, "license": license_name.strip() or "UNKNOWN", "license_files": sorted(notices, key=lambda row: row["path"])})
print(json.dumps(sorted(rows, key=lambda row: row["name"])))
"""
    return json.loads(subprocess.check_output([str(python), "-c", code], text=True))


def source_provenance(
    repository: Path, source_root: Path, commit: str, sdist_audit
) -> dict:
    """Bind audited sdist inputs to their build tree and local Git HEAD."""
    repository = Path(repository).resolve()
    source_root = Path(source_root).resolve()
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    if commit != head:
        raise ValueError("Manifest commit does not match source repository HEAD")
    object_format = subprocess.check_output(
        ["git", "rev-parse", "--show-object-format=storage"],
        cwd=repository,
        text=True,
    ).strip()
    tracked: dict[str, str] = {}
    entries = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", "HEAD"], cwd=repository
    )
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        if kind == "blob" and mode != "120000":
            tracked[raw_name.decode("utf-8", "surrogateescape")] = oid
    dirty = False
    aggregate = hashlib.sha256()
    count = 0
    for name in sdist_audit.members:
        relative = Path(*Path(name).parts[1:])
        if relative.as_posix() == "PKG-INFO":
            continue
        actual = source_root / relative
        if not actual.is_file() or actual.is_symlink():
            raise ValueError(f"Source input missing or linked: {relative}")
        data = actual.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != sdist_audit.member_sha256[name]:
            raise ValueError(f"Source input differs from archived member: {relative}")
        aggregate.update(
            relative.as_posix().encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n"
        )
        oid = hashlib.new(
            object_format, b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        ).hexdigest()
        if tracked.get(relative.as_posix()) != oid:
            dirty = True
        count += 1
    return {
        "commit": commit,
        "dirty": dirty,
        "tree_sha256": aggregate.hexdigest(),
        "source_files": count,
    }


def build_manifest(
    *,
    wheel: Path,
    sdist: Path,
    lock: Path,
    project: Path,
    base_python: Path,
    full_python: Path,
    commit: str,
    repository: Path,
    source_root: Path,
) -> dict:
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("Expected a full lowercase Git commit ID")
    wheel_audit = check_artifact(wheel)
    sdist_audit = check_artifact(sdist)
    source = source_provenance(repository, source_root, commit, sdist_audit)
    config = tomllib.loads(project.read_text())
    locked = tomllib.loads(lock.read_text())
    with ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in wheel_audit.members if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    version = metadata.get("Version")
    if version != config["project"]["version"]:
        raise ValueError("Built wheel version differs from project metadata")
    return {
        "schema_version": 1,
        "package": {
            "name": metadata.get("Name"),
            "version": version,
            "requires_python": metadata.get("Requires-Python"),
            "license": metadata.get("License-Expression") or metadata.get("License"),
        },
        "commit": commit,
        "source": source,
        "lock": {
            "sha256": _sha256(lock),
            "packages": len(locked["package"]),
            "requires_python": locked["requires-python"],
        },
        "toolchain": {
            "uv": subprocess.check_output(
                [shutil.which("uv") or str(Path.home() / ".local/bin/uv"), "--version"],
                text=True,
            ).strip(),
            "builder": config["build-system"]["requires"],
            "base_python": subprocess.check_output(
                [str(base_python), "--version"], text=True
            ).strip(),
            "full_python": subprocess.check_output(
                [str(full_python), "--version"], text=True
            ).strip(),
        },
        "features": config["project"].get("optional-dependencies", {}),
        "requirements": config["project"]["dependencies"],
        "resolved": {"base": _installed(base_python), "full": _installed(full_python)},
        "artifacts": {
            name: {
                "filename": audit.path.name,
                "sha256": audit.sha256,
                "members": audit.member_sha256,
            }
            for name, audit in (("wheel", wheel_audit), ("sdist", sdist_audit))
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "wheel",
        "sdist",
        "lock",
        "project",
        "base-python",
        "full-python",
        "repository",
        "source-root",
        "output",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    manifest = build_manifest(
        wheel=args.wheel,
        sdist=args.sdist,
        lock=args.lock,
        project=args.project,
        base_python=args.base_python,
        full_python=args.full_python,
        commit=args.commit,
        repository=args.repository,
        source_root=args.source_root,
    )
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "wheel_sha256": manifest["artifacts"]["wheel"]["sha256"],
                "sdist_sha256": manifest["artifacts"]["sdist"]["sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
