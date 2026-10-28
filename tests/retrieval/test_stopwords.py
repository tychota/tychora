"""Versioned stop-word lists shape lexical expressions, never the index."""

from pathlib import Path

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.application.query import QueryService
from tychora.configuration.resolve import resolve_config
from tychora.configuration.stopwords import load_stopwords
from tychora.retrieval.lexical_query import lexical_expression


def _config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "workspace.toml"
    path.write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="general-documents@1"\n' + extra
    )
    return resolve_config(path, "local", {})


def test_builtin_lists_load_and_are_lowercase_and_sorted():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    assert {"the", "of", "and", "le", "la", "été"} <= words
    assert not {"ai", "eu"} & words
    assert load_stopwords(("en", "fr"), "none").words == frozenset()
    root = Path(__file__).resolve().parents[2] / "src/tychora/resources/stopwords"
    for name in ("en@1.txt", "fr@1.txt"):
        lines = [
            line
            for line in (root / name).read_text().splitlines()
            if line and not line.startswith("#")
        ]
        assert lines == sorted(lines)
        assert all(line == line.casefold() for line in lines)


def test_stopwords_removed_from_fts_expression():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    expression = lexical_expression("the inspection of the pump seal", words)
    assert expression is not None
    assert '"the"' not in expression and '"of"' not in expression
    assert '"inspection"' in expression and '"seal"' in expression


def test_only_stopwords_keeps_original_terms():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    expression = lexical_expression("the of and", words)
    assert expression is not None
    assert {'"the"', '"of"', '"and"'} <= set(expression.split(" OR "))


def test_phrase_alone_is_not_diluted_by_stopword_fallback():
    words = load_stopwords(("en",), "builtin@1").words
    assert lexical_expression('"the of"', words) == '"the of"'


def test_stopword_digest_enters_query_fingerprint(tmp_path):
    builtin = _config(tmp_path)
    none = _config(tmp_path, '[configuration.retrieval.lexical]\nstopwords = "none"\n')
    english = _config(
        tmp_path, '[configuration.retrieval.lexical]\nlanguages = ["en"]\n'
    )
    assert (
        len(
            {
                builtin.query_fingerprint,
                none.query_fingerprint,
                english.query_fingerprint,
            }
        )
        == 3
    )
    assert builtin.query_fingerprint == _config(tmp_path).query_fingerprint
    assert builtin.index_fingerprint == none.index_fingerprint


def test_changing_lists_changes_the_result_cache_inputs(tmp_path):
    builtin = _config(tmp_path)
    none = _config(tmp_path, '[configuration.retrieval.lexical]\nstopwords = "none"\n')
    assert builtin.config_sha != none.config_sha
    service = QueryService.__new__(QueryService)
    service.text_provider = None
    service.text_index = None
    service.visual_provider = None
    service.visual_index = None
    service.reranker = None
    service.execution_plan = None
    assert service._provider_fingerprint(builtin) != service._provider_fingerprint(none)


def test_changing_lists_needs_no_rebuild(tmp_path):
    workspace = tmp_path / "ws"
    with Tychora.open(workspace, token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "pumps.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# Pumps\n\nThe inspection of the pump seal is quarterly.\n",
                "metadata": {"title": "Pumps"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        before = kb.search({"question": "the inspection of the seal"})
        only = kb.search({"question": "the of"})
    changed = _config(
        tmp_path, '[configuration.retrieval.lexical]\nstopwords = "none"\n'
    )
    with Tychora.open(workspace, config=changed, token_counter=OutputCounter()) as kb:
        after = kb.search({"question": "the inspection of the seal"})
    assert before.segments and after.segments
    assert before.snapshot_id == after.snapshot_id
    assert only.segments
