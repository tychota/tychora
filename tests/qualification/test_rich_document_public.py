"""Ordinary table-and-figure documents survive public source publication."""

import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.assets import asset_path
from tychora import Tychora
from tychora.configuration.resolve import resolve_config


class _Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.qualification
@pytest.mark.parametrize("suffix", ["pdf", "docx"])
def test_public_rich_document_with_table_and_figure_publishes(
    tmp_path, monkeypatch, suffix
):
    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    source = tmp_path / ("rich." + suffix)
    shutil.copy2(Path(__file__).parents[1] / "fixtures/documents" / source.name, source)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.extraction]\nparser="docling"\n'
        "[configuration.acquisition]\nroots=" + json.dumps([str(tmp_path)]) + "\n"
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=_Counter()
    ) as kb:
        handle = kb.sources.add(
            {
                "type": "file",
                "path": str(source),
                "source_key": source.name,
                "collection_id": "default",
                "kind": "manual@1",
                "format": suffix,
                "metadata": {
                    "title": "Synthetic field notebook",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(handle.id, timeout=180)
        assert status.state == "succeeded", status.error_code
