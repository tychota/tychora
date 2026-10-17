"""Known-item queries and corpus files produced by curation."""

import random

from tychora.evaluation.bench.corpus import BenchDocument, BenchQuery, load_corpus
from tychora.evaluation.bench.curation import corpus_toml, known_item, page_known_item
from tychora.evaluation.bench.synth import SynthSpec
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel

TEXT = (
    "Short.\n"
    "The harbour authority publishes a schedule of maintenance dredging every spring season.\n"
    "Another sentence that is long enough to be chosen as a known item for the query set."
)


def test_known_item_quotes_one_sentence():
    item = known_item(TEXT, random.Random(1))
    assert item is not None
    question, start, end = item
    assert question in TEXT[start:end]
    assert len(question.split()) == 7
    assert known_item("Too short.", random.Random(1)) is None


def test_page_known_item_returns_a_one_based_page():
    item = page_known_item(["", TEXT], random.Random(2))
    assert item is not None
    question, page = item
    assert page == 2
    assert question in TEXT


def test_page_known_item_is_none_without_a_usable_page():
    assert page_known_item(["", "Short."], random.Random(2)) is None


def _document(key: str, url: str) -> BenchDocument:
    return BenchDocument(
        source_key=key,
        title="Style Guide for Python Code",
        kind="document@1",
        category="text",
        format="text",
        media_type="text/plain",
        url=url,
        sha256="d" * 64,
        licence="Public domain or CC0-1.0",
    )


def test_corpus_toml_round_trips_through_load_corpus(tmp_path):
    document = _document(
        "public/pep-0008",
        "https://raw.githubusercontent.com/python/peps/abc/peps/pep-0008.rst",
    )
    query = BenchQuery(
        query_id="known/public/pep-0008",
        question="code is read much more often",
        labels=(
            GoldLabel(
                collection="default",
                source_key="public/pep-0008",
                anchor=CharRangeAnchor(char_start=10, char_end=60),
            ),
        ),
    )
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "x.jsonl").write_text(query.model_dump_json() + "\n")
    (tmp_path / "x.toml").write_text(
        corpus_toml(
            "x",
            SynthSpec(seed=1, count=3),
            [document],
            "queries/x.jsonl",
            "Test corpus.",
        )
    )
    corpus = load_corpus("x", tmp_path)
    assert corpus.documents == (document,)
    assert corpus.queries == (query,)
    assert corpus.allowed_hosts == ("raw.githubusercontent.com",)


def test_corpus_toml_declares_every_document_host_once_and_sorted(tmp_path):
    documents = [
        _document("a", "https://www.gutenberg.org/cache/epub/1/pg1.txt"),
        _document("b", "https://arxiv.org/pdf/1v1"),
        _document("c", "https://arxiv.org/pdf/2v1"),
    ]
    (tmp_path / "q.jsonl").write_text("")
    (tmp_path / "x.toml").write_text(
        corpus_toml("x", SynthSpec(seed=1, count=0), documents, "q.jsonl", "Header.")
    )
    assert load_corpus("x", tmp_path).allowed_hosts == (
        "arxiv.org",
        "www.gutenberg.org",
    )
