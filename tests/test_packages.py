"""Every directory of Python modules under src/tychora is an explicit package."""

from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "tychora"


def test_every_python_directory_has_an_init_module():
    missing = sorted(
        directory.relative_to(SOURCE).as_posix()
        for directory in [SOURCE, *SOURCE.rglob("*")]
        if directory.is_dir()
        and directory.name != "__pycache__"
        and any(directory.rglob("*.py"))
        and not (directory / "__init__.py").exists()
    )
    assert missing == []
