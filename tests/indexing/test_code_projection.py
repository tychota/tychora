"""Code excerpt projection validates one retained extraction once, not once per excerpt."""

import gc

import pytest

from tests.code.test_structure import _registration
from tychora.adapters.code.normalize import normalize_code, retain_code_extraction
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.evidence import CodeLocator
from tychora.indexing import representations
from tychora.indexing.representations import (
    prepare_extraction,
    project_code_excerpt_locator,
)


@pytest.fixture
def code_bundle(tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    registry = Registry.from_config(resolve_config(settings, "test", {}))
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry, artifacts)
    return normalize_code(retained, registry)


def _code_units(bundle):
    return [
        unit
        for unit in bundle.units
        if len(unit.text) > 1
        and any(isinstance(locator, CodeLocator) for locator in unit.locators)
    ]


def test_repeated_code_projections_validate_the_extraction_once(
    code_bundle, monkeypatch
):
    calls = []
    validate = representations._code_files

    def counting(bundle):
        calls.append(bundle)
        return validate(bundle)

    monkeypatch.setattr(representations, "_code_files", counting)
    units = _code_units(code_bundle)
    assert len(units) >= 2
    prepare_extraction(code_bundle)
    first = [
        project_code_excerpt_locator(code_bundle, unit.unit_id, 0, len(unit.text))
        for unit in units
        for _ in range(3)
    ]
    # Every projection reads the same validated index as the preparation.
    assert len(calls) == 1
    again = [
        project_code_excerpt_locator(code_bundle, unit.unit_id, 0, len(unit.text))
        for unit in units
        for _ in range(3)
    ]
    assert again == first
    assert len(calls) == 1


def test_projection_index_is_not_shared_across_equal_bundle_objects(
    code_bundle, monkeypatch
):
    calls = []
    validate = representations._code_files

    def counting(bundle):
        calls.append(bundle)
        return validate(bundle)

    monkeypatch.setattr(representations, "_code_files", counting)
    [unit, *_] = _code_units(code_bundle)
    copy = code_bundle.model_copy()
    project_code_excerpt_locator(code_bundle, unit.unit_id, 0, 1)
    project_code_excerpt_locator(copy, unit.unit_id, 0, 1)
    assert len(calls) == 2


def test_projection_index_is_released_with_its_bundle(code_bundle):
    [unit, *_] = _code_units(code_bundle)
    copy = code_bundle.model_copy()
    project_code_excerpt_locator(copy, unit.unit_id, 0, 1)
    cached = len(representations._BUNDLE_INDEXES)
    del copy
    gc.collect()
    assert len(representations._BUNDLE_INDEXES) == cached - 1


def test_invalid_projection_still_raises_after_caching(code_bundle):
    [unit, *_] = _code_units(code_bundle)
    project_code_excerpt_locator(code_bundle, unit.unit_id, 0, 1)
    with pytest.raises(ValueError, match="outside its retained unit"):
        project_code_excerpt_locator(code_bundle, unit.unit_id, 0, len(unit.text) + 1)
