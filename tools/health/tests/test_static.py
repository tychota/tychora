"""AST and text measures over a throwaway source tree."""

import subprocess
from pathlib import Path

import pytest

from tools.health import graph, static


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def long_function(name: str, lines: int) -> str:
    body = "".join(f"    x{i} = {i}\n" for i in range(lines - 2))
    return f"def {name}():\n{body}    return 0\n"


def test_function_length_keys_long_functions_by_qualified_name(tmp_path):
    write(
        tmp_path,
        "src/tychora/a.py",
        long_function("short", 80) + long_function("long", 81),
    )
    write(
        tmp_path,
        "src/tychora/b.py",
        "class Owner:\n"
        + "".join(
            "    " + line + "\n" for line in long_function("open", 90).splitlines()
        ),
    )
    [metric] = static.function_length(tmp_path)
    assert metric.per_key == {
        "src/tychora/a.py::long": 81,
        "src/tychora/b.py::Owner.open": 90,
    }
    assert metric.value == 2


def test_duplicate_qualified_names_get_a_suffix(tmp_path):
    write(tmp_path, "src/tychora/a.py", long_function("f", 81) + long_function("f", 82))
    [metric] = static.function_length(tmp_path)
    assert metric.per_key == {"src/tychora/a.py::f": 81, "src/tychora/a.py::f#2": 82}


def test_raw_sql_counts_calls_outside_adapters_only(tmp_path):
    write(
        tmp_path,
        "src/tychora/application/q.py",
        "db.execute('x')\ndb.executemany('y', [])\nrun()\n",
    )
    write(tmp_path, "src/tychora/adapters/sqlite/s.py", "db.execute('x')\n")
    [metric] = static.raw_sql(tmp_path)
    assert metric.per_key == {"src/tychora/application/q.py": 2}


def test_integrity_raises_without_reason(tmp_path):
    write(
        tmp_path,
        "src/tychora/application/x.py",
        "raise TychoraError('INTEGRITY_ERROR', 'a')\n"
        "raise errors.TychoraError('INTEGRITY_ERROR', 'b', reason='stale_manifest')\n"
        "raise TychoraError('NOT_FOUND', 'c')\n",
    )
    [metric] = static.integrity_raises(tmp_path)
    assert metric.value == 1
    assert metric.per_key is not None
    assert static.by_package(metric.per_key) == {"application": 1}


def test_plan_references_in_text_and_file_names(tmp_path):
    write(tmp_path, "src/tychora/a.py", '"""Bind Task19 ownership."""\n')
    write(tmp_path, "dashboard/e2e/task5.spec.ts", "test('x', () => {});\n")
    write(tmp_path, "docs/superpowers/plans/p.md", "Task 7\n")
    write(tmp_path, "tests/ok.py", "multitask 5 is not a reference\n")
    files = [
        "src/tychora/a.py",
        "dashboard/e2e/task5.spec.ts",
        "docs/superpowers/plans/p.md",
        "tests/ok.py",
    ]
    [metric] = static.plan_references(tmp_path, files)
    assert metric.per_key == {"src/tychora/a.py": 1, "dashboard/e2e/task5.spec.ts": 1}
    assert metric.kind == "hard"


def test_plan_references_reads_tracked_files(tmp_path):
    write(tmp_path, "tests/t.py", "# Task 3 leftover\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    [metric] = static.plan_references(tmp_path)
    assert metric.value == 1


def test_public_api_docstrings_include_methods_of_private_resource_classes(tmp_path):
    write(
        tmp_path,
        "src/tychora/api.py",
        '"""API."""\n\nclass LocalWorkspace:\n    """Doc."""\n    def close(self):\n        pass\n',
    )
    write(
        tmp_path,
        "src/tychora/clients/sync.py",
        "class _Sources:\n    def add(self):\n        def inner():\n            pass\n"
        "    def _private(self):\n        pass\n",
    )
    [metric] = static.public_api_docstrings(tmp_path)
    assert set(metric.per_key or {}) == {
        "src/tychora/api.py::LocalWorkspace.close",
        "src/tychora/clients/sync.py::_Sources.add",
    }


def test_quarantined_tests_are_counted(tmp_path):
    write(
        tmp_path,
        "tests/t.py",
        "import pytest\n@pytest.mark.quarantine(reason='r', issue='1')\ndef test_x():\n    pass\n",
    )
    [metric] = static.quarantined_tests(tmp_path)
    assert metric.value == 1


def test_cycles_split_module_level_and_function_body_imports(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(tmp_path, "src/tychora/a.py", "from tychora import b\n")
    write(tmp_path, "src/tychora/b.py", "def f():\n    from tychora.a import x\n")
    write(tmp_path, "src/tychora/c.py", "from . import d\n")
    write(tmp_path, "src/tychora/d.py", "from .c import y\n")
    [hard] = graph.module_level_cycles(tmp_path)
    assert hard.per_key == {"tychora.c": 1, "tychora.d": 1}
    [ratchet] = graph.all_import_cycles(tmp_path)
    assert ratchet.value == 4


def test_coupling_counts_distinct_components(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(tmp_path, "src/tychora/contracts/__init__.py", "")
    write(tmp_path, "src/tychora/contracts/ids.py", "")
    write(tmp_path, "src/tychora/application/__init__.py", "")
    write(
        tmp_path,
        "src/tychora/application/owner.py",
        "from tychora.contracts.ids import x\n",
    )
    write(tmp_path, "src/tychora/adapters/__init__.py", "")
    write(tmp_path, "src/tychora/adapters/sqlite/__init__.py", "")
    write(
        tmp_path,
        "src/tychora/adapters/sqlite/corpus.py",
        "from tychora.application.owner import y\n",
    )
    coupling = graph.coupling(graph.import_graph(tmp_path, top_level_only=False))
    assert coupling["contracts"] == (1, 0)
    assert coupling["application"] == (1, 1)
    assert coupling["adapters.sqlite"] == (0, 1)


def test_type_checking_imports_do_not_form_cycles(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(
        tmp_path,
        "src/tychora/a.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from tychora import b\n",
    )
    write(tmp_path, "src/tychora/b.py", "def f():\n    from tychora import a\n")
    write(tmp_path, "src/tychora/c.py", "def f():\n    from tychora import d\n")
    write(tmp_path, "src/tychora/d.py", "def g():\n    from tychora import c\n")
    [ratchet] = graph.all_import_cycles(tmp_path)
    assert ratchet.value == 2


def test_lookalike_package_names_are_not_tychora_modules(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(tmp_path, "src/tychora/a.py", "import tychoraish\n")
    assert graph.import_graph(tmp_path, top_level_only=True)["tychora.a"] == set()


def test_missing_or_empty_source_tree_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        static.function_length(tmp_path)
    (tmp_path / "src/tychora").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        static.raw_sql(tmp_path)
    write(tmp_path, "src/tychora/api.py", '"""API."""\n')
    with pytest.raises(FileNotFoundError):
        static.public_api_docstrings(tmp_path)
