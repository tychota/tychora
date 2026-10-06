"""Package discoverability; consuming-agent decisions are tested separately."""

from importlib.resources import files

import pytest


@pytest.mark.parametrize(
    "name", ["configure", "add-sources", "query", "inspect-evidence"]
)
def test_companion_skill_is_a_package_resource(name: str) -> None:
    resource = files("tychora").joinpath("resources", "skills", name, "SKILL.md")
    assert resource.is_file(), f"missing packaged skill: {name}"
    assert resource.read_bytes(), f"empty packaged skill: {name}"
