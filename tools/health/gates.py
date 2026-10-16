"""The gate registry: identifiers G1-G28, their group and the function that measures them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.health import advisories, external, graph, layering, static, testruns
from tools.health.model import Metric

Group = Literal["pr", "nightly"]


@dataclass(frozen=True)
class Gate:
    """One gate of the code-health design (section 4.3)."""

    id: str
    title: str
    group: Group
    measure: Callable[[Path], list[Metric]]
    fast: bool = False


GATES: tuple[Gate, ...] = (
    Gate("G1", "Lint", "pr", external.lint, fast=True),
    Gate("G2", "Format", "pr", external.format_check, fast=True),
    Gate("G3", "Types", "pr", external.types, fast=True),
    Gate("G4", "Size rules", "pr", external.size_rules),
    Gate("G5", "Function length", "pr", static.function_length),
    Gate("G6", "Function-body imports", "pr", external.body_imports),
    Gate("G7", "Layering", "pr", layering.layering, fast=True),
    Gate("G8", "Raw SQL outside adapters", "pr", static.raw_sql),
    Gate("G9-G11", "Duplication", "pr", external.duplication),
    Gate("G12", "Dead code, Python", "pr", external.dead_code),
    Gate("G13", "Dead code, TypeScript", "pr", external.dashboard_dead_code),
    Gate("G14", "Docstrings", "pr", external.docstrings),
    Gate("G14", "Public API docstrings", "pr", static.public_api_docstrings),
    Gate("G15", "Plan references", "pr", static.plan_references, fast=True),
    Gate("G16", "Coverage, lines", "pr", testruns.line_coverage),
    Gate("G16", "Coverage, dashboard", "pr", testruns.dashboard_coverage),
    Gate("G16", "Coverage, branches", "nightly", testruns.branch_coverage),
    Gate("G17", "Clean-machine suite", "pr", testruns.clean_suite),
    Gate("G18", "Test runtime", "pr", testruns.runtime),
    Gate("G19", "Advisories", "pr", advisories.advisories),
    Gate("G20", "Lock freshness", "pr", advisories.lock_freshness),
    Gate("G21", "Mutation score", "nightly", testruns.mutation),
    Gate("G22", "Code gold set", "nightly", testruns.gold_set),
    Gate("G23", "Quarantined tests", "pr", static.quarantined_tests),
    Gate("G24", "Integrity errors without a reason", "pr", static.integrity_raises),
    Gate("G25", "Cognitive complexity", "pr", external.complexity),
    Gate("G26", "Module-level import cycles", "pr", graph.module_level_cycles),
    Gate("G27", "Modules in import cycles", "pr", graph.all_import_cycles),
    Gate("G28", "Dashboard import cycles", "pr", external.dashboard_cycles),
)


def select(group: str, ids: str = "", *, fast: bool = False) -> list[Gate]:
    """Gates of a group ("pr", "nightly" or "all"), optionally narrowed by id or --fast."""
    wanted = {gate_id.strip() for gate_id in ids.split(",") if gate_id.strip()}
    chosen = [
        gate
        for gate in GATES
        if (group == "all" or gate.group == group)
        and (not wanted or gate.id in wanted)
        and (not fast or gate.fast)
    ]
    if not chosen:
        raise SystemExit(
            f"no gate selected (group={group!r}, ids={ids!r}, fast={fast})"
        )
    if wanted - {gate.id for gate in chosen}:
        raise SystemExit(
            f"unknown or unselected gates: {sorted(wanted - {g.id for g in chosen})}"
        )
    return chosen
