"""G19 and G20: dependency advisories against the waiver register, and lock freshness."""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from tools.health.external import command_gate, json_output, run, venv_tool
from tools.health.model import Metric

REGISTER = Path("quality/advisories.toml")
MAX_WAIVER_DAYS = 30


@dataclass(frozen=True)
class Waiver:
    """One owner-approved, time-limited exception for a Python or npm advisory."""

    id: str
    package: str
    reason: str
    reviewer: str
    approved: dt.date
    expires: dt.date


def _waiver(entry: dict[str, Any]) -> Waiver:
    missing = [f.name for f in fields(Waiver) if f.name not in entry]
    if missing:
        raise ValueError(f"{entry.get('id', '?')}: missing {', '.join(missing)}")
    return Waiver(**entry)


def load_register(text: str) -> list[Waiver]:
    """Parse quality/advisories.toml; every field is required."""
    data = tomllib.loads(text)
    waivers = [_waiver(entry) for entry in data.get("advisory", [])]
    for waiver in waivers:
        if (waiver.expires - waiver.approved).days > MAX_WAIVER_DAYS:
            raise ValueError(
                f"{waiver.id}: a waiver lasts at most {MAX_WAIVER_DAYS} days"
            )
    return waivers


def reported_advisories(pip_audit: dict[str, Any]) -> dict[str, str]:
    """Advisory id to package name from `pip-audit --format json`."""
    found: dict[str, str] = {}
    for dependency in pip_audit.get("dependencies", []):
        for vulnerability in dependency.get("vulns", []):
            found[vulnerability["id"]] = dependency["name"]
    return found


def advisory_findings(
    reported: dict[str, str], waivers: list[Waiver], today: dt.date
) -> dict[str, float]:
    """Unlisted or expired advisories, and waivers no advisory needs any more."""
    by_id = {waiver.id: waiver for waiver in waivers}
    findings: dict[str, float] = {}
    for advisory, package in sorted(reported.items()):
        waiver = by_id.get(advisory)
        if waiver is None:
            findings[f"{package}:{advisory}: not in {REGISTER}"] = 1
        elif waiver.expires < today:
            findings[f"{package}:{advisory}: waiver expired {waiver.expires}"] = 1
    for advisory in sorted(set(by_id) - set(reported)):
        findings[f"{advisory}: no longer reported; remove it from {REGISTER}"] = 1
    return findings


def npm_advisories(audit: dict[str, Any]) -> dict[str, str]:
    """Advisory id to `npm:<package>` from `npm audit --json`.

    `via` holds advisory objects for the package that carries the flaw and plain
    package names for transitive exposure; only the objects are advisories.
    """
    found: dict[str, str] = {}
    for vulnerability in audit.get("vulnerabilities", {}).values():
        for via in vulnerability.get("via", []):
            if isinstance(via, dict):
                advisory = str(via["url"]).rsplit("/", 1)[-1]
                found[advisory] = f"npm:{via['name']}"
    return found


def advisories(root: Path, today: dt.date | None = None) -> list[Metric]:
    """Hard gate over pip-audit (with the register) and npm audit."""
    pip = run([venv_tool("pip-audit"), "--local", "--format", "json"], root)
    npm = run(
        ["npm", "--prefix", "dashboard", "audit", "--audit-level=low", "--json"], root
    )
    reported = reported_advisories(json_output(pip, "pip-audit"))
    reported |= npm_advisories(json_output(npm, "npm audit"))
    waivers = load_register((root / REGISTER).read_text())
    findings = advisory_findings(
        reported, waivers, today or dt.datetime.now(tz=dt.UTC).date()
    )
    return [Metric("advisories", sum(findings.values()), kind="hard", per_key=findings)]


def lock_freshness(root: Path) -> list[Metric]:
    """G20: uv.lock matches pyproject.toml (npm ci in CI proves package-lock.json)."""
    return [command_gate("lock", [["uv", "lock", "--check"]], root)]
