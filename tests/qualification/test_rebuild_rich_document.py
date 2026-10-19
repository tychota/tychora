"""A rebuilt workspace keeps the asset ids and asset rejections of a rich PDF."""

import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.assets import asset_path
from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import make_format_one
from tychora import Tychora
from tychora.api import rebuild_workspace_from
from tychora.configuration.resolve import resolve_config
from tychora.contracts.corpus import ReviewTarget
from tychora.ports.execution import ExecutionContext


def _query(kb, sql: str):
    owner = kb._workspace.owner

    def read():
        return owner.db.execute(sql).fetchall()

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


def _asset_state(kb, asset_id: str) -> str:
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(
        owner.run_blocking(
            lambda: owner.store.review_state(ReviewTarget(asset_id=asset_id)),
            ExecutionContext(),
        )
    )


@pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")
@pytest.mark.qualification
def test_rebuild_keeps_asset_ids_and_a_rejected_asset(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_DOCLING_MODELS"))
    )
    pdf = tmp_path / "rich.pdf"
    shutil.copy2(Path(__file__).parents[1] / "fixtures/documents/rich.pdf", pdf)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.extraction]\nparser="docling"\n'
        "[configuration.acquisition]\nroots=" + json.dumps([str(tmp_path)]) + "\n"
    )
    old = tmp_path / "old"
    with Tychora.open(
        old, config=resolve_config(settings, "test", {}), token_counter=Counter()
    ) as kb:
        handle = kb.sources.add(
            {
                "type": "file",
                "path": str(pdf),
                "source_key": "rich.pdf",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "pdf",
                "metadata": {
                    "title": "Synthetic field notebook",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(handle.id, timeout=300)
        assert status.state == "succeeded", status.error_code
        asset_ids = sorted(row[0] for row in _query(kb, "SELECT asset_id FROM assets"))
        assert len(asset_ids) >= 1
        rejected = asset_ids[0]
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.store.record_review(
                    ReviewTarget(asset_id=rejected), "rejected", "reviewer"
                ),
                ExecutionContext(),
            )
        )
    make_format_one(old)
    report = rebuild_workspace_from(old, tmp_path / "new", token_counter=Counter())
    assert [item.source_key for item in report.sources] == ["rich.pdf"]
    assert report.skipped == ()
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        rebuilt = sorted(row[0] for row in _query(kb, "SELECT asset_id FROM assets"))
        assert rebuilt == asset_ids
        assert _asset_state(kb, rejected) == "rejected"
