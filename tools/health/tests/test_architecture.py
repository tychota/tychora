"""Architecture report sections over a throwaway package."""

from pathlib import Path

from tools.health import architecture


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_lcom4_finds_two_unrelated_method_clusters(tmp_path):
    write(
        tmp_path,
        "src/tychora/store.py",
        "class Store:\n"
        "    def read(self):\n        return self.db\n"
        "    def write(self):\n        self.db = 1\n"
        "    def render(self):\n        return self.template\n",
    )
    [row] = architecture.lcom4(tmp_path)
    assert row["lcom4"] == 2
    assert row["class"].endswith("Store")


def test_pass_through_detects_exact_forwarding_only(tmp_path):
    write(
        tmp_path,
        "src/tychora/client.py",
        "def add(source, key):\n    return service.add(source, key)\n"
        "def wait(job):\n    return service.wait(job, timeout=30)\n",
    )
    depth, forwarding = architecture.depth_and_pass_through(tmp_path)
    assert forwarding == ["src/tychora/client.py:1 add"]
    assert depth[0]["module"] == "src/tychora/client.py"


def test_render_shows_deltas_against_the_previous_report():
    data = {
        "coupling": [
            {
                "component": "application",
                "modules": 31,
                "ca": 14,
                "ce": 18,
                "i": 0.56,
                "a": 0.0,
                "d": 0.44,
            }
        ],
        "cycles": [60, 4],
        "complexity": [("src/tychora/x.py::f", 240)],
        "complexity_over_15": 228,
        "lcom4": [],
        "maintainability_c": [],
        "depth_median": 12.4,
        "shallowest": [],
        "co_change": {
            "commits": 10,
            "median_files": 4,
            "share_three_or_more_packages": 0.4,
        },
        "co_change_pairs": [],
        "pass_through": [],
    }
    previous = {
        "coupling": [{"component": "application", "ca": 15, "ce": 18}],
        "cycles": [63, 4, 3, 2, 2, 2],
        "complexity_over_15": 229,
        "depth_median": 12.4,
    }
    text = architecture.render(data, previous)
    assert "| `application` | 31 | 14 (-1) | 18 (=) |" in text
    assert "64 modules (-12)" in text
    assert "Functions above 15: 228 (-1)." in text
