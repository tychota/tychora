"""A failed historical code projection can be retried with current semantics."""

from tests.code.conftest import git, make_repo
from tests.evaluation.conftest import SyntheticCounter
from tychora import Tychora
from tychora.adapters.code import normalize as code_normalize
from tychora.configuration.resolve import resolve_config
from tychora.ports.execution import ExecutionContext


def test_public_retry_rebuilds_current_code_projection(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "entry.py").write_text(
        "def entry():\n    return helper()\n\ndef helper():\n    return 7\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Synthetic code")
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    request = {
        "type": "repository",
        "path": str(repo),
        "source_key": "code",
        "collection_id": "default",
        "kind": "code_file@1",
        "metadata": {"title": "Synthetic Repository"},
    }
    fingerprints = code_normalize._fingerprints

    def legacy_fingerprints(source, files, registry):
        parser, _ = fingerprints(source, files, registry)
        return parser, code_normalize._legacy_normalizer_fingerprint(source, registry)

    with Tychora.open(
        tmp_path / "workspace", config=config, token_counter=SyntheticCounter()
    ) as kb:
        with monkeypatch.context() as patch:
            patch.setattr(code_normalize, "_fingerprints", legacy_fingerprints)
            old = kb.sources.add(request)
            old_status = kb.jobs.wait(old.id, timeout=20)
        assert old_status.state == "failed"
        assert old_status.error_code == "INTEGRITY_ERROR"
        owner = kb._workspace.owner

        def extraction_rows():
            return owner.db.execute(
                "SELECT extraction_id,document_artifact_sha FROM extractions "
                "ORDER BY created_at,extraction_id"
            ).fetchall()

        before = kb._workspace.bridge.call(
            owner.run_write(extraction_rows, ExecutionContext())
        )
        assert len(before) == 1
        fresh = kb.sources.add(request)
        fresh_status = kb.jobs.wait(fresh.id, timeout=20)
        assert fresh_status.state == "succeeded", (
            fresh_status.error_code,
            fresh_status.state,
        )
        after = kb._workspace.bridge.call(
            owner.run_write(extraction_rows, ExecutionContext())
        )
        assert len(after) == 2
        assert before[0] in after
        assert after[0][0] != after[1][0]
        result = kb.search({"question": "entry", "modalities": ["lexical"]})
        assert result.segments
