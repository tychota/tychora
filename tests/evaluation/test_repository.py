"""Versioned gold registrations persist typed payloads in canonical SQL/CAS."""

from pathlib import Path

import pytest

from tychora.adapters.sqlite.evaluations import EvaluationRepository
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.evaluation.dataset import load_dataset
from tychora.ports.execution import ExecutionContext

FIXTURE = Path(__file__).parent.parent / "fixtures" / "evaluation"


def test_register_dataset_retains_each_case_and_rejects_changed_version(tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(principal_id="owner", capabilities=frozenset({"manage"}))
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:

        def register():
            repo = EvaluationRepository(owner)
            dataset = repo.register(load_dataset(FIXTURE))
            assert owner.db.execute(
                "SELECT COUNT(*) FROM evaluation_cases"
            ).fetchone() == (13,)
            assert repo.load(dataset.dataset_id).version_sha == dataset.version_sha
            return dataset

        dataset = owner.run_blocking_sync(register, ExecutionContext())
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

        owner.run_blocking_sync(other_principal, ExecutionContext())
