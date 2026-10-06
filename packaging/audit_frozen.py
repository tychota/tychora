"""Inspect actual onefile archive contents and Tychora-controlled code paths."""

from __future__ import annotations

import argparse
import json
import marshal
from hashlib import sha256
from pathlib import Path
from types import CodeType

from PyInstaller.archive.readers import CArchiveReader

FORBIDDEN = (
    b"/Users/",
    b"/home/",
    b"/private/var/folders/",
    b"tychora-development",
    b".superpowers/",
    b".worktrees/",
)
WEIGHTS = (".onnx", ".pt", ".pth", ".safetensors", ".gguf", ".ckpt")


def inspect_code(code: CodeType, label: str) -> None:
    for value in (code.co_filename, *code.co_consts):
        if isinstance(value, CodeType):
            inspect_code(value, label)
        elif isinstance(value, (str, bytes)):
            raw = value.encode() if isinstance(value, str) else value
            assert not any(pattern in raw for pattern in FORBIDDEN), (
                f"Private path in frozen Tychora code {label}: {raw[:150]!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    args = parser.parse_args()
    archive = CArchiveReader(str(args.artifact))
    paths = set(archive.toc)
    assert "PYZ.pyz" in paths
    assert not any(name.endswith(WEIGHTS) for name in paths), (
        "Model weights entered onefile archive"
    )
    assert not any(
        marker in name
        for name in paths
        for marker in (".worktrees/", ".superpowers/", "Library/Caches/", ".venv/")
    ), "Private source or cache path entered archive names"
    pyz = archive.open_embedded_archive("PYZ.pyz")
    modules = [
        name for name in pyz.toc if name == "tychora" or name.startswith("tychora.")
    ]
    assert len(modules) > 100, f"Unexpectedly few Tychora modules: {len(modules)}"
    for name in modules:
        code = pyz.extract(name)
        if code is None:
            assert pyz.toc[name][0] == 3, f"Missing code for {name}"
        else:
            inspect_code(code, name)
    inspect_code(marshal.loads(archive.extract("frozen_main")), "frozen_main")
    data_files = [name for name in paths if name.startswith("tychora/")]
    for name in data_files:
        payload = archive.extract(name)
        assert isinstance(payload, bytes)
        assert not any(pattern in payload for pattern in FORBIDDEN), (
            f"Private path in {name}"
        )
    notice_index = json.loads((args.notices / "manifest.json").read_text())
    required = {"third_party_notices/manifest.json"}
    for notice in notice_index["notices"]:
        name = "third_party_notices/" + notice["path"]
        required.add(name)
        assert name in paths, f"Missing notice text: {name}"
        assert sha256(archive.extract(name)).hexdigest() == notice["sha256"], name
    assert required <= paths
    print(
        f"PASS: {len(modules)} frozen Tychora modules, {len(data_files)} package resources, "
        f"{len(required)} actual notice files, no controlled private paths or model weights"
    )


if __name__ == "__main__":
    main()
