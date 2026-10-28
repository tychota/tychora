"""A trimmed code record still names the symbol it stood for."""

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import StructuredRecord
from tychora.retrieval.response import budget_stub


def _record(record_type, value):
    return StructuredRecord(
        record_id="record-1",
        unit_id="unit-1",
        record_type=record_type,
        schema_id="code-symbol@1" if record_type == "code_symbol" else "feedback@1",
        value=FrozenJsonMap(value),
        dependencies=frozenset({"unit-1"}),
    )


def test_code_symbol_stub_keeps_its_identity():
    record = _record(
        "code_symbol",
        {
            "definition_key": "python:src/a.py:Box.run",
            "qualified_name": "Box.run",
            "relative_path": "src/a.py",
            "context_spans": [{"reason": "parent_declaration"}] * 50,
        },
    )
    stub = budget_stub(record)
    assert stub.value == FrozenJsonMap(
        {
            "omitted_budget": True,
            "definition_key": "python:src/a.py:Box.run",
            "qualified_name": "Box.run",
            "relative_path": "src/a.py",
        }
    )
    assert stub.record_id == record.record_id


def test_other_record_stub_is_unchanged():
    stub = budget_stub(_record("feedback", {"literal_quote": "x" * 5000}))
    assert stub.value == FrozenJsonMap({"omitted_budget": True})


def test_oversized_identity_values_are_not_kept():
    stub = budget_stub(_record("code_symbol", {"definition_key": "k" * 600}))
    assert stub.value == FrozenJsonMap({"omitted_budget": True})
