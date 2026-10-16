import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def code_registry(tmp_path: Path) -> Registry:
    """The registry of the built-in code profile."""
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    return Registry.from_config(resolve_config(settings, "test", {}))


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Synthetic Author")
    git(root, "config", "user.email", "synthetic@example.invalid")
    (root / "module.py").write_text("def original(): return 1\n")
    (root / "notes.txt").write_text("synthetic notes\n")
    git(root, "add", "module.py", "notes.txt")
    git(root, "commit", "-qm", "synthetic fixture")
    return root


class Capture:
    def __init__(self, acquirer: Acquirer) -> None:
        self.acquirer = acquirer
        self.artifacts = acquirer.artifacts

    def __call__(self, root: Path, *, excludes: tuple[str, ...] = ()):
        return self.acquirer.capture(
            SourceInput.model_validate(
                {
                    "type": "repository",
                    "source_key": "code",
                    "collection_id": UUID("00000000-0000-4000-8000-000000000015"),
                    "kind": "code_file@1",
                    "path": str(root),
                    "excludes": excludes,
                }
            ),
            ExecutionContext(),
        )


def make_capture(tmp_path: Path) -> Capture:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        "schema_version=1\n"
        "[profiles.test]\n"
        'builtin="documents@1"\n'
        "[configuration.acquisition]\n"
        f'roots=["{tmp_path.resolve()}"]\n'
    )
    config = resolve_config(config_path, "test", {})
    acquirer = Acquirer(LocalArtifacts(tmp_path / "artifacts"), config)
    return Capture(acquirer)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def capture(tmp_path):
    return make_capture(tmp_path)
