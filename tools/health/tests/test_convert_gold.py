"""Golden-query conversion keeps families inside one split."""

from tools.health.convert_gold import convert, development_families


def entry(case_id, family, required=True):
    return {
        "id": case_id,
        "query": f"where is {case_id}",
        "expected_files": [
            {"path": "src/tychora/a.py", "required": required, "max_rank": 3},
            {"path": "src/tychora/b.py", "required": False, "max_rank": 10},
        ],
        "expected_symbols": [{"path": "src/tychora/a.py", "name": "a"}],
        "tags": [family, "extra"],
    }


def test_conversion_shape_and_required_paths():
    [template] = convert([entry("one", "identity")])
    assert template["required_paths"] == ["src/tychora/a.py"]
    assert template["optional_paths"] == ["src/tychora/b.py"]
    assert template["max_rank"] == 3
    assert template["family_key"] == "identity"
    assert template["benchmark"] == "semantic_code"
    assert template["query"]["question"] == "where is one"
    assert template["query"]["modalities"] == ["lexical"]


def test_at_least_ten_development_cases_and_families_never_split():
    entries = [entry(f"case-{i}", f"family-{i % 7}") for i in range(30)]
    templates = convert(entries)
    development = [t for t in templates if t["split"] == "development"]
    assert len(development) >= 10
    splits_by_family: dict[str, set[str]] = {}
    for template in templates:
        splits_by_family.setdefault(template["family_key"], set()).add(
            template["split"]
        )
    assert all(len(splits) == 1 for splits in splits_by_family.values())
    assert development_families(entries) == development_families(
        list(reversed(entries))
    )
