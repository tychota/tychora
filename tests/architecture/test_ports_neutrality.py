"""No port names a store type or takes store query syntax."""

import ast
import importlib
import inspect
import pkgutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

from tychora import ports

STORE_MODULES = ("sqlite3", "chromadb", "ladybug")
QUERY_PARAMETERS = {"sql", "where", "match"}


def _protocols() -> Iterator[type]:
    for info in pkgutil.iter_modules(ports.__path__):
        module = importlib.import_module(f"tychora.ports.{info.name}")
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and getattr(value, "_is_protocol", False)
                and value is not Protocol
                and value.__module__ == module.__name__
            ):
                yield value


def _text(annotation: object) -> str:
    return annotation if isinstance(annotation, str) else repr(annotation)


def _offenders(protocol: type) -> list[tuple[str, str, str]]:
    """Store types in any annotation and query-syntax parameters of one protocol."""
    found = [
        (protocol.__name__, name, _text(annotation))
        for name, annotation in inspect.get_annotations(protocol).items()
        if any(store in _text(annotation) for store in STORE_MODULES)
    ]
    for name, member in vars(protocol).items():
        if name.startswith("_") or not isinstance(member, Callable):  # type: ignore[arg-type]
            continue
        for parameter, annotation in inspect.get_annotations(member).items():
            text = _text(annotation)
            if any(store in text for store in STORE_MODULES):
                found.append((protocol.__name__, f"{name}({parameter})", text))
        for parameter in inspect.signature(member).parameters:
            if parameter in QUERY_PARAMETERS:
                found.append((protocol.__name__, name, f"parameter {parameter}"))
    return found


def test_ports_modules_do_not_import_store_packages():
    for path in Path(ports.__path__[0]).glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert name.split(".")[0] not in STORE_MODULES, (path.name, name)


def test_no_sqlite_type_escapes():
    offenders = [item for protocol in _protocols() for item in _offenders(protocol)]
    assert offenders == []


def test_neutrality_check_flags_store_types_and_query_parameters():
    class Leaky(Protocol):
        connection: sqlite3.Connection

        def run(self, sql: str) -> sqlite3.Row: ...

        def find(self, where: str, match: str) -> None: ...

    flagged = _offenders(Leaky)
    assert len(flagged) == 5


def test_repository_protocols_are_discovered():
    names = {protocol.__name__ for protocol in _protocols()}
    assert {"WorkspaceRepository", "ArtifactRepository"} <= names
