"""Resolve code gold-case templates to the identities of one ingested repository."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from tychora.contracts.identity import canonical_bytes

TEMPLATES = (
    Path(__file__).parents[1] / "fixtures/evaluation/code-gold/cases.template.jsonl"
)
DATASET_ID = "tychora-code-gold-v1"
TEMPLATE_ONLY = ("required_paths", "optional_paths", "max_rank", "symbols")


def load_templates(path: Path = TEMPLATES) -> list[dict[str, Any]]:
    """Every template line of the committed conversion."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def materialize(
    template: dict[str, Any],
    *,
    source_id: str,
    revision_id: str,
    definitions: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    """A tychora-gold@1 case: required files are recall targets, optional ones judged only."""

    def file_id(path: str) -> str:
        return f"{source_id}:{revision_id}:{path}"

    case = {key: value for key, value in template.items() if key not in TEMPLATE_ONLY}
    required = [file_id(path) for path in template["required_paths"]]
    optional = [file_id(path) for path in template["optional_paths"]]
    case["expected_source_ids"] = [source_id]
    case["expected_revision_ids"] = [revision_id]
    case["expected_file_ids"] = required
    case["file_relevance"] = {**{f: 1 for f in optional}, **{f: 3 for f in required}}
    case["expected_definition_ids"] = sorted(
        key
        for symbol in template["symbols"]
        for key in definitions[(symbol["path"], symbol["name"])]
    )
    return case


def write_dataset(directory: Path, cases: list[dict[str, Any]]) -> None:
    """Write manifest.json and cases.jsonl exactly as the loader verifies them."""
    payload = b"".join(canonical_bytes(case) + b"\n" for case in cases)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cases.jsonl").write_bytes(payload)
    (directory / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": DATASET_ID,
                "name": "Tychora source code retrieval",
                "version_sha": sha256(payload).hexdigest(),
                "case_count": len(cases),
            }
        )
    )


def mean_metrics(
    report: dict[str, Any], names: tuple[str, ...]
) -> dict[str, dict[str, float]]:
    """Mean of each metric per variant over the cases where it applies."""
    sums: dict[str, dict[str, list[float]]] = {}
    for entry in report["cases"].values():
        for run in entry["runs"]:
            values = {m["name"]: m["value"] for m in run["metrics"]["metrics"]}
            bucket = sums.setdefault(run["variant"], {name: [] for name in names})
            for name in names:
                if values.get(name) is not None:
                    bucket[name].append(values[name])
    return {
        variant: {
            name: round(sum(v) / len(v), 4) if v else 0.0 for name, v in metrics.items()
        }
        for variant, metrics in sorted(sums.items())
    }
