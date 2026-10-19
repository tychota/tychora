"""Evaluation locator claims are checked against retained snapshot bindings."""

import json
from types import SimpleNamespace

import pytest

from tests.code.conftest import git
from tests.evaluation.conftest import (
    SyntheticCounter,
    add_repository,
    code_profile_config,
)
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.adapters.sqlite.retained_bindings import binding_footprint, binding_key
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
        assert kb.jobs.wait(handle.id, timeout=scaled(10)).state == "succeeded"
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


def _result_rows(owner, result) -> int:
    """Retained rows of the one extraction a text result cites."""
    unit_id = next(
        citation.unit_id
        for segment in result.segments
        for citation in segment.citations
        if citation.unit_id is not None
    )
    key = binding_key(owner.db, unit_id)
    assert key is not None
    extraction_id = key[0]
    member = next(
        member
        for member in owner.store.resolve_snapshot(result.snapshot_id).members
        if member.extraction_id == extraction_id
    )
    return binding_footprint(owner.db, member)[0]


def _two_text_sources(kb):
    for key, word in (("first", "saffron"), ("second", "indigo")):
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": key,
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": f"The {word} marker identifies the {key} note.",
                "metadata": {"title": key, "publisher": "Example", "edition": "1"},
            }
        )
        assert kb.jobs.wait(handle.id, timeout=scaled(10)).state == "succeeded"
    return (
        kb.search({"question": "saffron", "modalities": ["lexical"]}),
        kb.search({"question": "indigo", "modalities": ["lexical"]}),
    )


def test_a_full_budget_releases_the_least_recently_used_binding(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        first, second = _two_text_sources(kb)
        owner = kb._workspace.owner

        def assess():
            rows = (_result_rows(owner, first), _result_rows(owner, second))
            # Each binding fits alone; both together do not.
            monkeypatch.setattr(
                "tychora.evaluation.provenance.MAX_BINDING_ROWS", max(rows)
            )
            cache = RetainedBindingCache(owner, first.snapshot_id)
            outcomes = [
                verify_result_locators(owner, value, cache=cache)
                for value in (first, second, first)
            ]
            return outcomes, cache, rows

        outcomes, cache, rows = kb._workspace.bridge.call(
            owner.run_blocking(assess, ExecutionContext())
        )
    assert all(item.valid == item.total > 0 for item in outcomes)
    assert cache.binding_count == 3
    assert cache.rows_used == rows[0] <= max(rows)


@pytest.mark.slow
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
            assert kb.jobs.wait(handle.id, timeout=scaled(10)).state == "succeeded"
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
                reused = commands[after_first:]
                monkeypatch.setattr(
                    "tychora.evaluation.provenance.MAX_BINDING_ROWS",
                    _result_rows(owner, other_result) - 1,
                )
                with pytest.raises(TychoraError) as error:
                    verify_result_locators(owner, other_result, cache=cache)
                assert error.value.code == "LIMIT_EXCEEDED"
                return first, second, reused, cache
            finally:
                owner.db.set_trace_callback(None)

        first, second, reused, cache = kb._workspace.bridge.call(
            owner.run_blocking(assess_twice, ExecutionContext())
        )
        assert first.valid == first.total > 0
        assert second == first
        assert cache.binding_count == 1
        assert any("SELECT u.* FROM evidence_units" in sql for sql in commands)
        assert not any("SUM(" in sql for sql in reused)
        assert not any("FROM assets a WHERE" in sql for sql in reused)
        assert not any("SELECT u.* FROM evidence_units" in sql for sql in reused)


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
        assert kb.jobs.wait(handle.id, timeout=scaled(10)).state == "succeeded"
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
        assert not any("SELECT u.* FROM evidence_units" in sql for sql in commands)


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
        assert kb.jobs.wait(handle.id, timeout=scaled(10)).state == "succeeded"
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
            "tychora.evaluation.provenance.retained_rows", unexpected_hydration
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


def _code_workspace(tmp_path):
    """A repository whose cited file is small next to the rest of the extraction."""
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "target.py").write_text("def zanzibar_marker():\n    return 'zanzibar'\n")
    for name in ("bulk_one", "bulk_two", "bulk_three"):
        (repo / f"{name}.py").write_text(
            "".join(
                f"def {name}_{index}():\n    return {index}\n\n" for index in range(40)
            )
        )
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Synthetic Author")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic repository")
    config = code_profile_config(tmp_path, "limit=10")
    kb = Tychora.open(
        tmp_path / "workspace", config=config, token_counter=SyntheticCounter()
    )
    try:
        handle = add_repository(kb, repo, "synthetic-repo", "Synthetic repository")
        # jobs.wait caps its timeout at 300 s; CI scales by 3.
        status = kb.jobs.wait(handle.id, timeout=min(scaled(120), 300))
        assert status.state == "succeeded"
    except BaseException:
        # An unclosed workspace hangs the worker when the garbage collector finalizes it.
        kb.close()
        raise
    return kb


def _extraction_unit_count(owner) -> int:
    return owner.db.execute("SELECT COUNT(*) FROM evidence_units").fetchone()[0]


def test_code_citations_bind_only_the_cited_file_within_budget(tmp_path, monkeypatch):
    with _code_workspace(tmp_path) as kb:
        result = kb.search({"question": "zanzibar_marker", "modalities": ["lexical"]})
        owner = kb._workspace.owner
        cited = {
            locator.relative_path
            for segment in result.segments
            for citation in segment.citations
            for locator in citation.locators
            if isinstance(locator, CodeLocator)
        }
        assert cited == {"target.py"}

        def assess():
            # The whole extraction holds at least one row per unit, so it cannot fit.
            monkeypatch.setattr(
                "tychora.evaluation.provenance.MAX_BINDING_ROWS",
                _extraction_unit_count(owner),
            )
            cache = RetainedBindingCache(owner, result.snapshot_id)
            return verify_result_locators(owner, result, cache=cache), cache

        assessment, cache = kb._workspace.bridge.call(
            owner.run_blocking(assess, ExecutionContext())
        )
    assert assessment.total > 0
    assert assessment.valid == assessment.total
    assert cache.binding_count == 1


def test_one_code_file_above_the_budget_is_still_refused(tmp_path, monkeypatch):
    with _code_workspace(tmp_path) as kb:
        result = kb.search({"question": "zanzibar_marker", "modalities": ["lexical"]})
        owner = kb._workspace.owner

        def assess(limit: int):
            monkeypatch.setattr("tychora.evaluation.provenance.MAX_BINDING_ROWS", limit)
            return verify_result_locators(owner, result)

        def file_rows() -> int:
            member = owner.store.resolve_snapshot(result.snapshot_id).members[0]
            return binding_footprint(owner.db, member, "target.py")[0]

        rows = kb._workspace.bridge.call(
            owner.run_blocking(file_rows, ExecutionContext())
        )
        fits = kb._workspace.bridge.call(
            owner.run_blocking(lambda: assess(rows), ExecutionContext())
        )
        assert fits.valid == fits.total > 0
        with pytest.raises(TychoraError) as error:
            kb._workspace.bridge.call(
                owner.run_blocking(lambda: assess(rows - 1), ExecutionContext())
            )
    assert error.value.code == "LIMIT_EXCEEDED"
    assert error.value.reason == "evaluation.binding_budget"


def _held(cache) -> tuple[int, int]:
    """Rows and bytes of the bindings the cache currently holds."""
    resident = cache._resident.values()
    return sum(item.rows for item in resident), sum(item.size for item in resident)


def test_mixed_whole_and_file_bindings_never_exceed_the_budget(tmp_path, monkeypatch):
    with _code_workspace(tmp_path) as kb:
        result = kb.search({"question": "zanzibar_marker", "modalities": ["lexical"]})
        owner = kb._workspace.owner

        def assess():
            member = owner.store.resolve_snapshot(result.snapshot_id).members[0]
            whole = binding_footprint(owner.db, member)[0]
            single = binding_footprint(owner.db, member, "target.py")[0]
            monkeypatch.setattr(
                "tychora.evaluation.provenance.MAX_BINDING_ROWS", whole + 2 * single
            )
            cache = RetainedBindingCache(owner, result.snapshot_id)
            observed = []
            for path in ("target.py", None, "target.py", None, "target.py"):
                cache._load(member.extraction_id, path)
                observed.append((cache.rows_used, cache.bytes_used, _held(cache)))
            assessment = verify_result_locators(owner, result, cache=cache)
            observed.append((cache.rows_used, cache.bytes_used, _held(cache)))
            return observed, assessment, whole + 2 * single

        observed, assessment, budget = kb._workspace.bridge.call(
            owner.run_blocking(assess, ExecutionContext())
        )
    for rows_used, bytes_used, held in observed:
        assert (rows_used, bytes_used) == held
        assert rows_used <= budget
    assert assessment.valid == assessment.total > 0


def test_tampered_code_locator_in_the_cited_file_is_invalid(tmp_path):
    with _code_workspace(tmp_path) as kb:
        result = kb.search({"question": "zanzibar_marker", "modalities": ["lexical"]})
        owner = kb._workspace.owner
        segment = next(
            segment
            for segment in result.segments
            if any(
                isinstance(locator, CodeLocator)
                for citation in segment.citations
                for locator in citation.locators
            )
        )
        citation = next(
            citation
            for citation in segment.citations
            if isinstance(citation.locators[0], CodeLocator)
        )
        locator = citation.locators[0]

        def assess(value):
            return kb._workspace.bridge.call(
                owner.run_blocking(
                    lambda: verify_result_locators(owner, value), ExecutionContext()
                )
            )

        baseline = assess(result)
        assert baseline.valid == baseline.total > 0
        for update in (
            {"byte_start": locator.byte_start + 1},
            {"line_start": locator.line_start + 1},
            {"relative_path": "bulk_one.py", "native_ref": "bulk_one.py"},
        ):
            changed = citation.model_copy(
                update={"locators": (locator.model_copy(update=update),)}
            )
            tampered = segment.model_copy(
                update={
                    "citations": tuple(
                        changed if item is citation else item
                        for item in segment.citations
                    )
                }
            )
            bad = result.model_copy(
                update={
                    "segments": tuple(
                        tampered if item is segment else item
                        for item in result.segments
                    )
                }
            )
            outcome = assess(bad)
            assert outcome.valid < outcome.total, update
