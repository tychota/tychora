"""Lexical retrieval metrics stay at or above the b91d808 baseline (E6)."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "bench-results" / "baseline" / "evaluation-b91d808.json"
LOWER_IS_BETTER = {"forbidden_leakage"}


def _script():
    spec = importlib.util.spec_from_file_location(
        "evaluation_baseline", ROOT / "benchmarks" / "evaluation_baseline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lexical_metrics_do_not_fall_below_the_baseline(tmp_path):
    baseline = json.loads(BASELINE.read_text())["variants"]["lexical"]
    current = _script().measure(tmp_path, "HEAD")["variants"]["lexical"]
    for name, expected in baseline.items():
        if expected is None:
            continue
        observed = current[name]
        assert observed is not None, name
        if name in LOWER_IS_BETTER:
            assert observed <= expected + 1e-9, name
        else:
            assert observed >= expected - 1e-9, name
