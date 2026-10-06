"""Build staged public sources with named private paths denied on macOS."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _rule(path: Path) -> str:
    return f"(deny file-read* (subpath {json.dumps(str(path.resolve()))}))"


def _build_env(checkout: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "UV_CACHE_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PATH"] = os.pathsep.join(
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and not Path(part).resolve().is_relative_to(checkout.resolve())
    )
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _inside(args: argparse.Namespace) -> None:
    for private in (
        args.checkout / "README.md",
        args.git_common_dir / "config",
        args.private / "sentinel.txt",
    ):
        try:
            private.read_bytes()
        except PermissionError:
            continue
        raise RuntimeError(f"Private build input was readable: {private}")
    if not (args.source / "README.md").read_bytes():
        raise RuntimeError("Staged public README was unreadable or empty")
    subprocess.run(
        [str(args.uv), "build", "--offline", "--out-dir", str(args.out_dir)],
        cwd=args.source,
        check=True,
    )
    args.proof.write_text(
        json.dumps(
            {
                "checkout_denied": True,
                "git_common_dir_denied": True,
                "private_fixture_denied": True,
                "public_stage_readable": True,
                "build_under_same_profile": True,
            },
            sort_keys=True,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "source",
        "checkout",
        "git-common-dir",
        "private",
        "out-dir",
        "proof",
        "uv",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--inside", action="store_true")
    args = parser.parse_args()
    if args.inside:
        _inside(args)
        return
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("macOS sandbox-exec is required for this release build")
    if not (args.private / "sentinel.txt").is_file():
        raise RuntimeError("Synthetic private sentinel is missing")
    profile = "\n".join(
        [
            "(version 1)",
            "(allow default)",
            _rule(args.checkout),
            _rule(args.git_common_dir),
            _rule(args.private),
        ]
    )
    forwarded = sys.argv[1:] + ["--inside"]
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if executable.is_relative_to(args.checkout.resolve()):
        raise RuntimeError("Sandbox Python executable resides inside denied checkout")
    subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            str(executable),
            str(Path(__file__).resolve()),
            *forwarded,
        ],
        cwd=args.source,
        env=_build_env(args.checkout),
        check=True,
    )


if __name__ == "__main__":
    main()
