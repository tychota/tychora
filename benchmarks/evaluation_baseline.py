"""Record lexical retrieval metrics of the synthetic executable corpus.

Uses only public API available since b91d808, so one script measures the
baseline commit and every later commit. The corpus setup is the one the
evaluation tests use (`tests/evaluation/conftest.py`), imported from the
checkout that holds this script; `tychora` itself comes from the active
environment, so run the script with the environment of the commit to measure:

    uv run --locked python benchmarks/evaluation_baseline.py --commit "$(git rev-parse HEAD)" --out OUT.json
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.evaluation.conftest import integrity_corpus

DATASET = "synthetic-executable-v1"
VARIANTS = {"lexical": {"channels": ["lexical"], "rerank": False}}
UNAVAILABLE = {
    "hybrid": "needs a text family: run with an online Voyage profile",
    "reranked": "needs a text family and a reranker: run with an online Voyage profile",
}


def measure(workspace: Path, commit: str) -> dict:
    """Ingest the synthetic corpus, evaluate each available variant, return metrics."""
    with integrity_corpus(workspace) as (kb, gold, _):
        kb.evaluations.register_dataset(gold)
        snapshot = kb.search(
            {"question": "saffron", "modalities": ["lexical"]}
        ).snapshot_id
        profile = kb._workspace.config.profile
        variants = {}
        for name, overrides in VARIANTS.items():
            handle = kb.evaluations.run(
                {
                    "dataset_id": DATASET,
                    "split": "test",
                    "snapshot_id": snapshot,
                    "offline": False,
                    "variants": [
                        {"name": name, "profile": profile, "overrides": overrides}
                    ],
                }
            )
            report = kb.evaluations.get(handle.id)["report"]
            variants[name] = {
                item["name"]: item["value"] for item in report["aggregate"]["metrics"]
            }
    return {
        "schema": "tychora.evaluation-baseline",
        "version": 1,
        "commit": commit,
        "dataset": DATASET,
        "variants": variants,
        "unavailable": UNAVAILABLE,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        document = measure(Path(directory), args.commit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
