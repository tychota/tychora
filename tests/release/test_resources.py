"""Resources must survive installation, not only checkout imports."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.release
def test_installed_package_contains_schema_and_generic_profiles(installed_base):
    code = """from importlib.resources import files
import tomlkit
from importlib.metadata import requires, version
assert version("tomlkit") == "0.15.1"
assert any(item.startswith("tomlkit==0.15.1") for item in requires("tychora"))
root = files("tychora")
assert root.joinpath("resources/migrations/0001.sql").is_file()
assert root.joinpath("profiles/documents/profile.toml").is_file()
assert root.joinpath("resources/skills/query/SKILL.md").is_file()
assert root.joinpath("resources/stopwords/en@1.txt").is_file()
assert root.joinpath("resources/stopwords/fr@1.txt").is_file()
"""
    result = subprocess.run(
        [str(installed_base.python), "-c", code],
        cwd=installed_base.working_dir,
        env=installed_base.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not Path(os.getcwd()).samefile(installed_base.working_dir)
