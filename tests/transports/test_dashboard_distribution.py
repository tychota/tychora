"""Build artifacts retain the dashboard without shipping developer caches."""

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.mark.release
def test_dashboard_in_built_wheel_and_sdist(tmp_path):
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-d", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        prefix = "tychora/resources/dashboard/"
        index = wheel.read(prefix + "index.html").decode()
        assert 'rel="icon"' in index
        assert "/dashboard/assets/" in index
        assets = [
            name for name in wheel.namelist() if name.startswith(prefix + "assets/")
        ]
        assert any(name.endswith(".js") for name in assets)
        assert any(name.endswith(".css") for name in assets)
        assert prefix + "assets/favicon.svg" in assets
        notices = wheel.read(prefix + "assets/THIRD-PARTY-NOTICES.txt").decode()
        for dependency in ("react", "react-dom", "scheduler"):
            assert dependency + "@" in notices
        assert "Permission is hereby granted" in notices
    with tarfile.open(next(tmp_path.glob("*.tar.gz"))) as sdist:
        names = ["/".join(name.split("/")[1:]) for name in sdist.getnames()]
        for name in (
            "package.json",
            "package-lock.json",
            "src/App.tsx",
            "e2e/server.py",
            "playwright.config.ts",
            "scripts/notices.mjs",
        ):
            assert "dashboard/" + name in names
        assert not any(
            part in name.split("/")
            for name in names
            for part in (
                "node_modules",
                "test-results",
                "playwright-report",
                "__pycache__",
            )
        )


def test_dashboard_audit_accepts_source_but_rejects_caches():
    import importlib.util
    from pathlib import PurePosixPath

    spec = importlib.util.spec_from_file_location(
        "dashboard_auditor", Path(__file__).parents[2] / "packaging/check_artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _allowed_path = module._allowed_path

    assert _allowed_path(PurePosixPath("tychora-0.1.0/dashboard/src/App.tsx"), "sdist")
    for part in ("node_modules", "test-results", "playwright-report"):
        assert not _allowed_path(
            PurePosixPath(f"tychora-0.1.0/dashboard/{part}/output.txt"), "sdist"
        )
    # The pinned Node version file ships in the sdist; no other dashboard dotfile does.
    assert _allowed_path(PurePosixPath("tychora-0.1.0/dashboard/.nvmrc"), "sdist")
    assert not _allowed_path(PurePosixPath("tychora-0.1.0/dashboard/.env"), "sdist")
    assert not _allowed_path(PurePosixPath("tychora-0.1.0/other/.nvmrc"), "sdist")
    assert not _allowed_path(PurePosixPath("tychora-0.1.0/dashboard/.nvmrc"), "wheel")
    # Dashboard source pages named models/ ship, but models/ stays denied elsewhere.
    page = "tychora-0.1.0/dashboard/src/pages/models/ExpertSettings.tsx"
    assert _allowed_path(PurePosixPath(page), "sdist")
    assert not _allowed_path(
        PurePosixPath(page.replace("dashboard/src", "src")), "sdist"
    )
    assert not _allowed_path(PurePosixPath(page), "wheel")
    assert not _allowed_path(
        PurePosixPath("tychora-0.1.0/dashboard/src/models/w.safetensors"), "sdist"
    )
    assert not _allowed_path(
        PurePosixPath("tychora-0.1.0/dashboard/src/node_modules/x.js"), "sdist"
    )
