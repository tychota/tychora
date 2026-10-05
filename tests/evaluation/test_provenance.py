"""Evaluation locator claims are checked against retained snapshot bindings."""

from types import SimpleNamespace

from tychora import Tychora
from tychora.contracts.evidence import CodeLocator, ImageLocator, VisualEvidence
from tychora.evaluation.provenance import (
    canonical_locator_matches,
    verify_result_locators,
    visual_binding_matches,
)
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


def test_real_retained_text_locator_rejects_wrong_identity_and_binding(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "canonical-note",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "The saffron marker identifies the canonical note.",
                "metadata": {
                    "title": "Canonical note",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id, timeout=10).state == "succeeded"
        result = kb.search({"question": "saffron marker", "modalities": ["lexical"]})
        owner = kb._workspace.owner

        def assess(value):
            return kb._workspace.bridge.call(
                owner.run_blocking(
                    lambda: verify_result_locators(owner, value), ExecutionContext()
                )
            )

        baseline = assess(result)
        assert baseline.total > 0 and baseline.valid == baseline.total
        segment = next(segment for segment in result.segments if segment.citations)
        citation = segment.citations[0]
        locator = citation.locators[0]
        for field, replacement in (
            ("artifact_ref", "wrong-artifact"),
            ("native_ref", "wrong-native-ref"),
        ):
            changed = locator.model_copy(update={field: replacement})
            bad_citation = citation.model_copy(update={"locators": (changed,)})
            bad_segment = segment.model_copy(update={"citations": (bad_citation,)})
            bad_result = result.model_copy(
                update={
                    "segments": tuple(
                        bad_segment if item is segment else item
                        for item in result.segments
                    )
                }
            )
            assert assess(bad_result).valid < assess(bad_result).total
        for field, replacement in (
            ("source_id", "wrong-source"),
            ("revision_id", "wrong-revision"),
        ):
            bad_citation = citation.model_copy(update={field: replacement})
            bad_segment = segment.model_copy(update={"citations": (bad_citation,)})
            bad_result = result.model_copy(
                update={
                    "segments": tuple(
                        bad_segment if item is segment else item
                        for item in result.segments
                    )
                }
            )
            assert assess(bad_result).valid < assess(bad_result).total


def test_code_locator_rejects_wrong_path_and_projected_offsets():
    canonical = CodeLocator(
        artifact_ref="artifact",
        native_ref="src/one.py",
        relative_path="src/one.py",
        char_start=10,
        char_end=20,
        byte_start=10,
        byte_end=20,
        line_start=2,
        line_end=2,
    )
    assert not canonical_locator_matches(
        canonical.model_copy(update={"relative_path": "src/other.py"}),
        canonical,
        None,
        "unit",
    )
    assert not canonical_locator_matches(
        canonical.model_copy(update={"byte_start": 11}),
        canonical,
        None,
        "unit",
    )


def test_visual_binding_rejects_wrong_artifact_native_source_and_revision():
    canonical = ImageLocator(artifact_ref="asset-artifact", native_ref="page-image")
    member = SimpleNamespace(
        source_id="source", revision_id="revision", extraction_id="extraction"
    )
    asset = SimpleNamespace(
        asset_id="asset",
        extraction_id="extraction",
        artifact_sha="asset-artifact",
        locator=canonical,
    )
    visual = VisualEvidence(
        asset_id="asset",
        source_id="source",
        revision_id="revision",
        asset_ref="asset-artifact",
        locator=canonical,
    )
    assert visual_binding_matches(visual, asset, member)
    assert not visual_binding_matches(
        visual.model_copy(update={"asset_ref": "wrong-artifact"}), asset, member
    )
    assert not visual_binding_matches(
        visual.model_copy(
            update={"locator": canonical.model_copy(update={"native_ref": "wrong"})}
        ),
        asset,
        member,
    )
    assert not visual_binding_matches(
        visual.model_copy(update={"source_id": "wrong"}), asset, member
    )
    assert not visual_binding_matches(
        visual.model_copy(update={"revision_id": "wrong"}), asset, member
    )
