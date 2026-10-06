"""Resources must survive installation, not only checkout imports."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_installed_package_contains_schema_and_generic_profiles(installed_base):
    code = """from importlib.resources import files
root = files("tychora")
assert root.joinpath("resources/migrations/0001.sql").is_file()
assert root.joinpath("profiles/documents/profile.toml").is_file()
assert root.joinpath("resources/skills/query/SKILL.md").is_file()
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
