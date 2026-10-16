"""Metrics, the committed baseline and the ratchet rules that compare them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Direction = Literal["lower", "higher"]
Kind = Literal["hard", "ratchet"]

BASELINE = Path("quality/baseline.json")
UPDATE_HINT = "run `python -m tools.health update` and commit quality/baseline.json"


@dataclass(frozen=True)
class Metric:
    """One measured quantity, optionally broken down per file, rule or function.

    A hard metric counts findings and must be zero. A ratchet metric is compared
    with its baseline entry: it may not get worse, and an improvement must be
    recorded in the baseline in the same change.
    """

    name: str
    value: float
    direction: Direction = "lower"
    kind: Kind = "ratchet"
    per_key: dict[str, float] | None = None
    tolerance: float = 0.0
    relative_tolerance: float = 0.0
    new_keys_allowed: bool = False
    stale_on_improvement: bool = True


@dataclass(frozen=True)
class Finding:
    """A regression, or an improvement the baseline has not recorded yet (stale)."""

    metric: str
    message: str
    stale: bool = False


class UpdateRefused(Exception):
    """The current values cannot replace the baseline without hiding a regression."""


def _worse(direction: Direction, current: float, base: float, slack: float) -> bool:
    if direction == "lower":
        return current > base + slack
    return current < base - slack


def _better(direction: Direction, current: float, base: float, slack: float) -> bool:
    if direction == "lower":
        return current < base - slack
    return current > base + slack


def _slack(metric: Metric, base: float) -> float:
    return max(metric.tolerance, abs(base) * metric.relative_tolerance)


def compare(metric: Metric, entry: dict[str, Any] | None) -> list[Finding]:
    """Apply the hard rule, or the global, per-key and stale-baseline rules."""
    if metric.kind == "hard":
        if metric.value:
            return [
                Finding(
                    metric.name, f"{metric.value:g} finding(s); this gate allows none"
                )
            ]
        return []
    if entry is None:
        return [Finding(metric.name, f"no baseline entry; {UPDATE_HINT}")]
    findings: list[Finding] = []
    base = float(entry["value"])
    slack = _slack(metric, base)
    if _worse(metric.direction, metric.value, base, slack):
        findings.append(
            Finding(metric.name, f"{metric.value:g} is worse than baseline {base:g}")
        )
    elif metric.stale_on_improvement and _better(
        metric.direction, metric.value, base, slack
    ):
        findings.append(
            Finding(
                metric.name,
                f"improved from {base:g} to {metric.value:g}; {UPDATE_HINT}",
                stale=True,
            )
        )
    findings.extend(_compare_keys(metric, entry.get("per_key") or {}))
    return findings


def _compare_keys(metric: Metric, base_keys: dict[str, float]) -> list[Finding]:
    if metric.per_key is None:
        return []
    findings: list[Finding] = []
    for key, value in sorted(metric.per_key.items()):
        if key not in base_keys:
            if not metric.new_keys_allowed and _worse(metric.direction, value, 0, 0):
                findings.append(
                    Finding(metric.name, f"{key}: new entry with value {value:g}")
                )
            continue
        base = float(base_keys[key])
        if _worse(metric.direction, value, base, metric.tolerance):
            findings.append(
                Finding(metric.name, f"{key}: {value:g} is worse than {base:g}")
            )
        elif metric.stale_on_improvement and _better(
            metric.direction, value, base, metric.tolerance
        ):
            findings.append(
                Finding(
                    metric.name,
                    f"{key}: improved from {base:g} to {value:g}; {UPDATE_HINT}",
                    stale=True,
                )
            )
    if metric.stale_on_improvement and metric.direction == "lower":
        for key in sorted(set(base_keys) - set(metric.per_key)):
            if float(base_keys[key]) > 0:
                findings.append(
                    Finding(
                        metric.name,
                        f"{key}: no longer present; {UPDATE_HINT}",
                        stale=True,
                    )
                )
    return findings


def updated_entry(
    metric: Metric, entry: dict[str, Any] | None, commit: str, *, initial: bool
) -> dict[str, Any]:
    """Return the baseline entry after recording improvements, or refuse."""
    if entry is None:
        if not initial:
            raise UpdateRefused(
                f"{metric.name}: no baseline entry; run `update --initial` once"
            )
        return _entry(metric, metric.value, metric.per_key, commit)
    base = float(entry["value"])
    slack = _slack(metric, base)
    if _worse(metric.direction, metric.value, base, slack):
        raise UpdateRefused(f"{metric.name}: {metric.value:g} is worse than {base:g}")
    improved = metric.stale_on_improvement and _better(
        metric.direction, metric.value, base, slack
    )
    value = metric.value if improved else base
    per_key = None
    if metric.per_key is not None:
        per_key = _updated_keys(metric, entry.get("per_key") or {})
    changed = value != base or per_key != entry.get("per_key")
    return _entry(metric, value, per_key, commit if changed else entry["measured_at"])


def _updated_keys(metric: Metric, base_keys: dict[str, float]) -> dict[str, float]:
    current = metric.per_key or {}
    kept: dict[str, float] = {}
    for key, value in current.items():
        if key not in base_keys:
            continue
        base = float(base_keys[key])
        if _worse(metric.direction, value, base, metric.tolerance):
            raise UpdateRefused(
                f"{metric.name}: {key} {value:g} is worse than {base:g}"
            )
        better = _better(metric.direction, value, base, metric.tolerance)
        kept[key] = value if better else base
    added = {key: value for key, value in current.items() if key not in base_keys}
    removed = {key: float(v) for key, v in base_keys.items() if key not in current}
    if (
        metric.direction == "lower"
        and not metric.new_keys_allowed
        and sum(added.values()) > sum(removed.values())
    ):
        raise UpdateRefused(
            f"{metric.name}: new entries {sorted(added)} outweigh the removed "
            f"ones {sorted(removed)}; only renames and moves can be recorded"
        )
    kept.update(added)
    return dict(sorted(kept.items()))


def _entry(
    metric: Metric, value: float, per_key: dict[str, float] | None, commit: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "value": value,
        "direction": metric.direction,
        "measured_at": commit,
    }
    if per_key is not None:
        entry["per_key"] = dict(sorted(per_key.items()))
    return entry


def load_baseline(path: Path) -> dict[str, dict[str, Any]]:
    """Read quality/baseline.json; a missing file is an empty baseline."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("schema") != 1:
        raise ValueError(f"{path}: unsupported baseline schema {data.get('schema')!r}")
    return data["metrics"]


def save_baseline(path: Path, metrics: dict[str, dict[str, Any]]) -> None:
    """Write the baseline with sorted keys so every update is a readable diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "metrics": metrics}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
