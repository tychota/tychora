"""Versioned gold registrations persist typed payloads in canonical SQL/CAS."""

import pytest

from tychora.adapters.sqlite.evaluations import EvaluationRepository
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.evaluation.dataset import load_dataset
from tychora.ports.execution import ExecutionContext


def test_register_dataset_retains_each_case_and_rejects_changed_version(
    executable_integrity_corpus,
):
    kb, gold, _ = executable_integrity_corpus
    owner = kb._workspace.owner
    scope = kb.scope

    def register():
        repo = EvaluationRepository(owner)
        dataset = repo.register(load_dataset(gold), scope)
        assert owner.db.execute("SELECT COUNT(*) FROM evaluation_cases").fetchone() == (
            5,
        )
        assert (
            repo.load(dataset.dataset_id, scope=scope).version_sha
            == dataset.version_sha
        )
        return dataset

    dataset = kb._workspace.bridge.call(
        owner.run_blocking(register, ExecutionContext())
    )
    assert dataset.version_sha

    def other_principal():
        repo = EvaluationRepository(owner)
        with pytest.raises(TychoraError) as denied:
            repo.load(
                dataset.dataset_id,
                scope=RequestScope(
                    principal_id="other", capabilities=frozenset({"manage"})
                ),
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"

    kb._workspace.bridge.call(owner.run_blocking(other_principal, ExecutionContext()))
