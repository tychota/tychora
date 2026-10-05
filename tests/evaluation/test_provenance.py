"""Evaluation locator claims are checked against retained snapshot bindings."""

import json
from types import SimpleNamespace

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import CodeLocator, ImageLocator, VisualEvidence
from tychora.evaluation.provenance import (
    RetainedBindingCache,
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


def test_selected_bindings_are_bounded_and_reused_without_snapshot_rehydration(
    tmp_path,
    monkeypatch,
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        for key, text in (
            ("selected", "The saffron marker identifies the selected note."),
            ("unrelated", "indigo " * 5_000),
        ):
            handle = kb.sources.add(
                {
                    "type": "text",
                    "source_key": key,
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": text,
                    "metadata": {"title": key, "publisher": "Example", "edition": "1"},
                }
            )
            assert kb.jobs.wait(handle.id, timeout=10).state == "succeeded"
        result = kb.search({"question": "saffron", "modalities": ["lexical"]})
        other_result = kb.search({"question": "indigo", "modalities": ["lexical"]})
        owner = kb._workspace.owner
        commands = []

        def assess_twice():
            owner.db.set_trace_callback(commands.append)
            try:
                cache = RetainedBindingCache(owner, result.snapshot_id)
                first = verify_result_locators(owner, result, cache=cache)
                after_first = len(commands)
                second = verify_result_locators(owner, result, cache=cache)
                monkeypatch.setattr(
                    "tychora.evaluation.provenance.MAX_BINDING_ROWS", cache.rows_used
                )
                with pytest.raises(TychoraError) as error:
                    verify_result_locators(owner, other_result, cache=cache)
                assert error.value.code == "LIMIT_EXCEEDED"
                return first, second, after_first, cache
            finally:
                owner.db.set_trace_callback(None)

        first, second, after_first, cache = kb._workspace.bridge.call(
            owner.run_blocking(assess_twice, ExecutionContext())
        )
        assert first.valid == first.total > 0
        assert second == first
        assert cache.binding_count == 1
        assert not any(
            "FROM evidence_units WHERE extraction_id=" in sql
            for sql in commands[after_first:]
        )
        assert not any(
            "FROM assets WHERE extraction_id=" in sql for sql in commands[after_first:]
        )
        assert not any(
            "SELECT * FROM evidence_units" in sql for sql in commands[after_first:]
        )


def test_selected_text_budget_rejects_before_materialization(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "large-selected",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "saffron " * 1000,
                "metadata": {
                    "title": "Large selected",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id, timeout=10).state == "succeeded"
        result = kb.search({"question": "saffron", "modalities": ["lexical"]})
        owner = kb._workspace.owner
        monkeypatch.setattr("tychora.evaluation.provenance.MAX_BINDING_BYTES", 5_000)
        commands = []

        def assess():
            owner.db.set_trace_callback(commands.append)
            try:
                verify_result_locators(owner, result)
            finally:
                owner.db.set_trace_callback(None)

        with pytest.raises(TychoraError) as error:
            kb._workspace.bridge.call(owner.run_blocking(assess, ExecutionContext()))
        assert error.value.code == "LIMIT_EXCEEDED"
        assert any("SUM(" in sql and "FROM evidence_units" in sql for sql in commands)
        assert not any("SELECT * FROM evidence_units" in sql for sql in commands)


def test_mismatched_retained_locator_artifact_fails_before_hydration(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "artifact-mismatch",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "The saffron marker has one canonical artifact.",
                "metadata": {
                    "title": "Artifact mismatch",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id, timeout=10).state == "succeeded"
        result = kb.search({"question": "saffron", "modalities": ["lexical"]})
        unit_id = next(
            citation.unit_id
            for segment in result.segments
            for citation in segment.citations
            if citation.unit_id is not None
        )
        owner = kb._workspace.owner

        def corrupt():
            locator_id, artifact_sha, raw = owner.db.execute(
                "SELECT locator_id,artifact_sha,details_json FROM locators WHERE unit_id=?",
                (unit_id,),
            ).fetchone()
            other = owner.db.execute(
                "SELECT artifact_sha FROM artifacts WHERE artifact_sha<>? LIMIT 1",
                (artifact_sha,),
            ).fetchone()[0]
            details = json.loads(raw)
            details["artifact_ref"] = other
            owner.db.execute(
                "UPDATE locators SET details_json=? WHERE locator_id=?",
                (json.dumps(details), locator_id),
            )

        kb._workspace.bridge.call(owner.run_blocking(corrupt, ExecutionContext()))
        hydrated = []

        def unexpected_hydration(*args):
            hydrated.append(True)
            raise AssertionError(
                "Retained rows were hydrated before artifact validation"
            )

        monkeypatch.setattr(
            "tychora.evaluation.provenance._retained_rows", unexpected_hydration
        )
        with pytest.raises(TychoraError) as error:
            kb._workspace.bridge.call(
                owner.run_blocking(
                    lambda: verify_result_locators(owner, result), ExecutionContext()
                )
            )
        assert error.value.code == "INTEGRITY_ERROR"
        assert not hydrated


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
