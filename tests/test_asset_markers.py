"""The assets marker skips ordinary tests and fails qualification tests without assets."""

import pytest

from tests.helpers import assets

pytest_plugins = ["pytester"]

CONFTEST = """
import pytest
from tests.helpers import assets

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    assets.check_item(item)

def pytest_runtest_teardown(item):
    assets.end_test()
"""

TESTS = """
import pytest
from tests.helpers.assets import asset_path

@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_reads_cache():
    assert asset_path("TYCHORA_TEST_MODEL_CACHE").name == "cache"

@pytest.mark.qualification
@pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")
def test_qualifies_docling():
    pass

def test_undeclared_read():
    asset_path("TYCHORA_TEST_MODEL_CACHE")
"""


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(CONFTEST)
    pytester.makeini(
        "[pytest]\nmarkers =\n    assets(*names): provisioned test assets\n"
        "    qualification: real local adapters\n"
    )
    pytester.makepyfile(test_assets=TESTS)
    return pytester


def test_missing_asset_skips_with_the_variable_name(suite, monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_MODEL_CACHE", raising=False)
    monkeypatch.delenv("TYCHORA_TEST_DOCLING_MODELS", raising=False)
    result = suite.runpytest("-rs", "-p", "no:randomly")
    result.assert_outcomes(skipped=1, failed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*requires TYCHORA_TEST_MODEL_CACHE (provision with packaging/provision_ci_assets.py)*"
        ]
    )


def test_qualification_fails_instead_of_skipping(suite, monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_DOCLING_MODELS", raising=False)
    result = suite.runpytest("-p", "no:randomly", "-k", "qualifies")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*requires TYCHORA_TEST_DOCLING_MODELS*"])


def test_declared_asset_is_readable_and_undeclared_read_is_refused(
    suite, monkeypatch, tmp_path
):
    monkeypatch.setenv("TYCHORA_TEST_MODEL_CACHE", str(tmp_path / "cache"))
    result = suite.runpytest("-p", "no:randomly", "-k", "reads_cache or undeclared")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        ["*needs @pytest.mark.assets(*TYCHORA_TEST_MODEL_CACHE*) on the test*"]
    )


def test_unknown_asset_variable_is_a_usage_error(pytester: pytest.Pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makeini(
        "[pytest]\nmarkers =\n    assets(*names): provisioned test assets\n"
    )
    pytester.makepyfile(
        "import pytest\n@pytest.mark.assets('TYCHORA_TEST_TYPO')\ndef test_x():\n    pass\n"
    )
    result = pytester.runpytest("-p", "no:randomly")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*unknown asset variables*TYCHORA_TEST_TYPO*"])


def test_registered_variables_cover_every_provisioned_asset():
    assert "TYCHORA_TEST_CODE_CORPUS" in assets.ASSET_VARIABLES
