"""Deterministic synthetic documents, corpus files and verified downloads."""

import io
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from tychora.evaluation.bench import curation
from tychora.evaluation.bench.corpus import (
    BenchCorpus,
    BenchCorpusError,
    BenchDocument,
    all_queries,
    document_bytes,
    fetch_corpus,
    load_corpus,
    synth_queries,
)
from tychora.evaluation.bench.synth import (
    PROBE_QUESTION,
    SynthSpec,
    document_at,
    generate,
)
from tychora.evaluation.dataset import CharRangeAnchor

BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"


def test_synthetic_documents_are_deterministic_and_mark_their_anchor():
    spec = SynthSpec(seed=7, count=5)
    first, again = generate(spec), generate(spec)
    assert first == again
    assert document_at(spec, 3) == first[3]
    for document in first:
        start = document.marker_start
        assert document.text[start : start + len(document.marker)] == document.marker
    assert len({document.marker for document in first}) == 5


def test_synthetic_queries_label_markers_and_the_probe():
    documents = generate(SynthSpec(seed=7, count=40, verification_share=0.5))
    queries = synth_queries(documents, 10)
    markers = [query for query in queries if not query.probe]
    assert len(markers) == 10
    probe = [query for query in queries if query.probe]
    assert probe and probe[0].question == PROBE_QUESTION
    anchors = [label.anchor for label in probe[0].labels]
    assert anchors and all(
        isinstance(anchor, CharRangeAnchor) and anchor.char_end > anchor.char_start
        for anchor in anchors
    )


def test_bench_small_loads_with_a_stable_digest():
    corpus = load_corpus("bench-small", BENCHMARKS)
    assert corpus.synthetic.count == 100
    assert corpus.documents == ()
    assert corpus.digest() == load_corpus("bench-small", BENCHMARKS).digest()
    assert len(all_queries(corpus)) == 21


def test_load_corpus_refuses_a_foreign_file(tmp_path):
    (tmp_path / "odd.toml").write_text('schema = "other"\nversion = 1\n')
    with pytest.raises(BenchCorpusError, match="odd.toml"):
        load_corpus("odd", tmp_path)
    with pytest.raises(BenchCorpusError, match="missing.toml"):
        load_corpus("missing", tmp_path)


def _corpus(
    data: bytes, transform: Literal["rasterize@150dpi"] | None = None
) -> BenchCorpus:
    return BenchCorpus(
        name="tiny",
        synthetic=SynthSpec(seed=1, count=0),
        documents=(
            BenchDocument(
                source_key="public/one",
                title="One",
                kind="document@1",
                category="text",
                format="text",
                media_type="text/plain",
                url="https://example.org/one.txt",
                sha256=sha256(data).hexdigest(),
                licence="CC0-1.0",
                transform=transform,
            ),
        ),
        queries=(),
    )


def test_fetch_stores_verified_bytes_once(tmp_path):
    calls = []

    def opener(url):
        calls.append(url)
        return io.BytesIO(b"pinned bytes")

    corpus = _corpus(b"pinned bytes")
    assert fetch_corpus(corpus, tmp_path, opener) == 1
    assert fetch_corpus(corpus, tmp_path, opener) == 0
    assert calls == ["https://example.org/one.txt"]
    assert document_bytes(corpus.documents[0], tmp_path) == b"pinned bytes"


def test_fetch_refuses_changed_bytes(tmp_path):
    corpus = _corpus(b"pinned bytes")
    with pytest.raises(BenchCorpusError, match="public/one"):
        fetch_corpus(corpus, tmp_path, lambda url: io.BytesIO(b"changed upstream"))
    assert list(tmp_path.iterdir()) == []


def test_fetch_refuses_a_truncated_download(tmp_path):
    corpus = _corpus(b"pinned bytes")
    with pytest.raises(BenchCorpusError, match="public/one"):
        fetch_corpus(corpus, tmp_path, lambda url: io.BytesIO(b"pinned"))
    assert list(tmp_path.iterdir()) == []


def test_fetch_refuses_an_oversized_download(tmp_path):
    corpus = _corpus(b"pinned bytes")
    with pytest.raises(BenchCorpusError, match="public/one"):
        fetch_corpus(
            corpus, tmp_path, lambda url: io.BytesIO(b"pinned bytes"), max_bytes=4
        )
    assert list(tmp_path.iterdir()) == []


def test_document_bytes_refuses_damaged_cache(tmp_path):
    corpus = _corpus(b"pinned bytes")
    fetch_corpus(corpus, tmp_path, lambda url: io.BytesIO(b"pinned bytes"))
    (tmp_path / corpus.documents[0].sha256).write_bytes(b"damaged")
    with pytest.raises(BenchCorpusError, match="public/one"):
        document_bytes(corpus.documents[0], tmp_path)


def _pdf_with_text_pages(pages: int) -> bytes:
    pil = pytest.importorskip("PIL.Image")
    output = io.BytesIO()
    images = [
        pil.new("RGB", (200, 100), (255 - index, 255, 255)) for index in range(pages)
    ]
    images[0].save(output, "PDF", save_all=True, append_images=images[1:])
    return output.getvalue()


def test_rasterize_pdf_returns_an_image_only_pdf_with_every_page():
    pytest.importorskip("pypdfium2")
    pypdf = pytest.importorskip("pypdf")
    scan = curation.rasterize_pdf(_pdf_with_text_pages(2))
    reader = pypdf.PdfReader(io.BytesIO(scan))
    assert scan.startswith(b"%PDF") and len(reader.pages) == 2


def test_rasterize_pdf_names_the_missing_extra(monkeypatch):
    monkeypatch.setattr(curation, "RASTERIZE_AVAILABLE", False)
    with pytest.raises(curation.CurationUnavailable, match="documents"):
        curation.rasterize_pdf(b"%PDF-")


def test_document_bytes_derives_and_caches_the_rasterized_scan(tmp_path, monkeypatch):
    original = b"%PDF- original"
    corpus = _corpus(original, transform="rasterize@150dpi")
    fetch_corpus(corpus, tmp_path, lambda url: io.BytesIO(original))
    calls = []

    def fake(data):
        calls.append(data)
        return b"%PDF- scan"

    monkeypatch.setattr("tychora.evaluation.bench.corpus.rasterize_pdf", fake)
    document = corpus.documents[0]
    assert document_bytes(document, tmp_path) == b"%PDF- scan"
    assert document_bytes(document, tmp_path) == b"%PDF- scan"
    assert calls == [original]
