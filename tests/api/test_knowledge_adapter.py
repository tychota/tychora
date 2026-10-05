from types import SimpleNamespace

from examples.knowledge_adapter import retrieve_segments


def test_visual_metadata_is_limited_to_cited_segment():
    def segment(name, cited):
        return SimpleNamespace(
            segment_id=name,
            text=name,
            matched_unit_ids=(name,),
            context_only_unit_ids=(),
            source_id="source",
            revision_id="revision",
            extraction_id="extraction",
            citations=(
                SimpleNamespace(asset_id="asset" if cited else None, locators=()),
            ),
            role="evidence",
        )

    visual = SimpleNamespace(
        asset_id="asset",
        source_id="source",
        revision_id="revision",
        proxies=(SimpleNamespace(proxy_id="proxy", origin="derived"),),
        citations=(),
    )
    kb = SimpleNamespace(
        search=lambda request: SimpleNamespace(
            segments=(segment("first", True), segment("second", False)),
            visuals=(visual,),
            run_id="run",
            snapshot_id="snapshot",
        )
    )
    first, second = retrieve_segments(kb, "synthetic question")
    assert first["metadata"]["asset_ids"] == ["asset"]
    assert first["metadata"]["derived"] == ["proxy"]
    assert second["metadata"]["asset_ids"] == []
    assert second["metadata"]["derived"] == []
