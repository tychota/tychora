"""Convert golden queries into Tychora code gold-case templates (tychora-gold@1 without IDs).

Input entries carry `id`, `query`, `expected_files` (each with `path`, `required` and
`max_rank`), `expected_symbols` (each with `path` and `name`) and `tags`, the schema
of an earlier code-search project's golden queries. Output lines keep paths
and symbol names; tests/helpers/code_gold.py resolves them to the workspace-specific
file and definition identities after ingestion.

Run: uv run --locked python -m tools.health.convert_gold QUERIES OUTPUT
QUERIES may be JSON or YAML (PyYAML is in the locked full environment).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

DEVELOPMENT_CASES = 10
PROVENANCE = (
    "Queries and labels written by the maintainer against src/tychora at "
    "700df656fb391aa5eaa41a75ce27eed432be442c and reviewed before commit"
)
QUERY_DEFAULTS: dict[str, Any] = {
    "modalities": ["lexical"],
    "limit": 10,
    "budgets": {"max_tokens": 30000, "tokenizer_id": "cl100k_base"},
}


def _stable(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def development_families(entries: list[dict[str, Any]]) -> set[str]:
    """Families, in stable hash order, until at least ten cases are for development."""
    sizes: dict[str, int] = {}
    for entry in entries:
        family = entry["tags"][0]
        sizes[family] = sizes.get(family, 0) + 1
    chosen: set[str] = set()
    count = 0
    for family in sorted(sizes, key=_stable):
        if count >= DEVELOPMENT_CASES:
            break
        chosen.add(family)
        count += sizes[family]
    return chosen


def convert(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One template per query; the first tag is the family, so families never cross splits."""
    development = development_families(entries)
    templates = []
    for entry in entries:
        family = entry["tags"][0]
        files = entry["expected_files"]
        templates.append(
            {
                "case_id": entry["id"],
                "family_key": family,
                "split": "development" if family in development else "test",
                "benchmark": "semantic_code",
                "label_provenance": PROVENANCE,
                "query": {"question": entry["query"], **QUERY_DEFAULTS},
                "required_paths": [f["path"] for f in files if f.get("required", True)],
                "optional_paths": [
                    f["path"] for f in files if not f.get("required", True)
                ],
                "max_rank": min(f.get("max_rank", 10) for f in files),
                "symbols": entry.get("expected_symbols", []),
            }
        )
    return sorted(templates, key=lambda template: template["case_id"])


def load_queries(path: Path) -> list[dict[str, Any]]:
    """Read the query list from JSON, or YAML when the file ends in .yaml or .yml."""
    if path.suffix in {".yaml", ".yml"}:
        return importlib.import_module("yaml").safe_load(path.read_text())
    return json.loads(path.read_text())


def main(argv: list[str]) -> int:
    """Convert QUERIES to OUTPUT (JSON Lines, one template per case)."""
    source, output = Path(argv[0]), Path(argv[1])
    entries = load_queries(source)
    lines = [json.dumps(template, sort_keys=True) for template in convert(entries)]
    output.write_text("\n".join(lines) + "\n")
    print(f"{len(lines)} cases written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
