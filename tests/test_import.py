import os
import subprocess
import sys


def test_import_has_no_runtime_side_effects(tmp_path):
    code = "import sys, tychora; assert not ({'chromadb','ladybug','torch','docling','voyageai'} & sys.modules.keys())"
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_console_command_displays_help_without_creating_state(tmp_path):
    run = subprocess.run(
        ["tychora", "--help"],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert "Discover and retrieve versioned evidence." in run.stdout
    assert list(tmp_path.iterdir()) == []


def test_python_module_displays_help_without_creating_state(tmp_path):
    run = subprocess.run(
        [sys.executable, "-m", "tychora", "--help"],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert "Discover and retrieve versioned evidence." in run.stdout
    assert list(tmp_path.iterdir()) == []
