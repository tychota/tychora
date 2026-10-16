"""Module import graph of src/tychora, its cycles and its component coupling."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from tools.health.model import Metric
from tools.health.static import SOURCE, python_files

Graph = dict[str, set[str]]


def module_name(root: Path, path: Path) -> str:
    """Dotted module name of a file under src/, packages named without __init__."""
    parts = list(path.relative_to(root / SOURCE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _targets(node: ast.Import | ast.ImportFrom, package: list[str]) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        base = package[: len(package) - node.level + 1]
        module = ".".join([*base, *([node.module] if node.module else [])])
    else:
        module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Every node below node except the bodies of `if TYPE_CHECKING:` blocks."""
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            for other in child.orelse:
                yield other
                yield from _runtime_nodes(other)
        else:
            yield from _runtime_nodes(child)


def import_graph(
    root: Path, *, top_level_only: bool, include_type_checking: bool = True
) -> Graph:
    """Edges between tychora modules; top_level_only keeps module-body imports.

    include_type_checking=False drops imports under `if TYPE_CHECKING:`, which
    never run.
    """
    trees: dict[str, tuple[ast.Module, list[str]]] = {}
    for path in python_files(root):
        name = module_name(root, path)
        parts = name.split(".")
        package = parts if path.name == "__init__.py" else parts[:-1]
        trees[name] = (ast.parse(path.read_text(encoding="utf-8")), package)
    known = set(trees)

    def resolve(target: str) -> str | None:
        while target and target not in known:
            target = target.rpartition(".")[0]
        return target or None

    graph: Graph = {name: set() for name in trees}
    for name, (tree, package) in trees.items():
        if top_level_only:
            nodes: list[ast.AST] = list(tree.body)
        elif include_type_checking:
            nodes = list(ast.walk(tree))
        else:
            nodes = list(_runtime_nodes(tree))
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                for target in _targets(node, package):
                    if target != "tychora" and not target.startswith("tychora."):
                        continue
                    resolved = resolve(target)
                    if resolved and resolved != name:
                        graph[name].add(resolved)
    return graph


def strongly_connected(graph: Graph) -> list[list[str]]:
    """Tarjan's algorithm without recursion; returns components of two or more."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        result.append(sorted(component))
    return sorted(result, key=lambda c: (-len(c), c))


def module_level_cycles(root: Path) -> list[Metric]:
    """G26: modules in cycles through module-body imports; must stay zero."""
    components = strongly_connected(import_graph(root, top_level_only=True))
    per_key = {member: 1.0 for component in components for member in component}
    return [Metric("module_level_cycles", len(per_key), kind="hard", per_key=per_key)]


def all_import_cycles(root: Path) -> list[Metric]:
    """G27: modules in cycles when function-body imports count too.

    Imports under `if TYPE_CHECKING:` are excluded: they never run, so they
    cannot form a runtime cycle.
    """
    graph = import_graph(root, top_level_only=False, include_type_checking=False)
    components = strongly_connected(graph)
    return [Metric("modules_in_import_cycles", sum(len(c) for c in components))]


def component(module: str) -> str:
    """Top-level package; adapters.* and interfaces.* are split by child."""
    parts = module.split(".")
    if len(parts) < 2:
        return "tychora"
    if parts[1] in {"adapters", "interfaces"} and len(parts) > 3:
        return f"{parts[1]}.{parts[2]}"
    return parts[1]


def coupling(graph: Graph) -> dict[str, tuple[int, int]]:
    """Afferent and efferent coupling (Ca, Ce) per component."""
    efferent: dict[str, set[str]] = defaultdict(set)
    afferent: dict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            a, b = component(source), component(target)
            if a != b:
                efferent[a].add(b)
                afferent[b].add(a)
    names = {component(module) for module in graph}
    return {name: (len(afferent[name]), len(efferent[name])) for name in sorted(names)}
