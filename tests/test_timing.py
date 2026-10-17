"""Fixed waits in tests scale with TYCHORA_TEST_TIMEOUT_FACTOR."""

from tests.helpers.timing import scaled


def test_wait_budgets_scale_with_the_factor(monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_TIMEOUT_FACTOR", raising=False)
    assert scaled(10) == 10
    monkeypatch.setenv("TYCHORA_TEST_TIMEOUT_FACTOR", "3")
    assert scaled(10) == 30
    monkeypatch.setenv("TYCHORA_TEST_TIMEOUT_FACTOR", "1.5")
    assert scaled(2) == 3
