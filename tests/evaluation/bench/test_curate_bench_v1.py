"""The bench-v1 curation script, run offline against injected fixtures."""

import importlib.util
import io
import json
import random
import sys
import urllib.error
from collections.abc import Mapping
from email.message import Message
from hashlib import sha256
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tychora.evaluation.bench.corpus import BenchCorpusError, fetch_corpus, load_corpus

SCRIPT = Path(__file__).resolve().parents[3] / "benchmarks" / "curate_bench_v1.py"
SENTENCE = "The harbour authority publishes a schedule of maintenance dredging every spring season."


def _load():
    spec = importlib.util.spec_from_file_location("curate_bench_v1", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_bench_v1"] = module
    spec.loader.exec_module(module)
    return module


curate = _load()


def text_pdf(pages: list[str]) -> bytes:
    """A real PDF whose page ``i`` shows ``pages[i]``."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(612, 792)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 9 Tf 20 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Response:
    """A streaming response that refuses an unbounded read."""

    def __init__(self, data: bytes, reads: list[int]):
        self.buffer = io.BytesIO(data)
        self.reads = reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        assert size > 0, "downloads must be read in bounded chunks"
        self.reads.append(size)
        return self.buffer.read(size)


class Site:
    """An opener over canned bodies; a list body yields one entry per request."""

    def __init__(self, pages: Mapping[str, bytes | list[bytes]]):
        self.pages = pages
        self.requests: list[str] = []
        self.reads: list[int] = []

    def __call__(self, url: str):
        self.requests.append(url)
        body = self.pages.get(url)
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        data = body.pop(0) if isinstance(body, list) else body
        return Response(data, self.reads)


def context(tmp_path: Path, site: Site):
    cache = tmp_path / "cache"
    files = curate.Fetcher(site, cache, curate.DOCUMENT_HOSTS, pause=lambda _: None)
    meta = curate.Fetcher(site, cache, curate.METADATA_HOSTS, pause=lambda _: None)
    return curate.Context(files=files, meta=meta, report=curate.Report())


ACT = "https://www.legislation.gov.uk/ukpga/{}/{}/data.pdf"


def test_save_streams_to_the_cache_under_its_digest(tmp_path):
    body = b"%PDF-" + b"x" * 3_000_000
    site = Site({ACT.format(2020, 1): body})
    fetched = context(tmp_path, site).files.save(ACT.format(2020, 1))
    assert fetched.sha256 == sha256(body).hexdigest() and fetched.size == len(body)
    assert fetched.path == tmp_path / "cache" / fetched.sha256
    assert fetched.path.read_bytes() == body
    assert max(site.reads) <= 1024**2
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [fetched.sha256]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.legislation.gov.uk/ukpga/2020/1/data.pdf",
        "https://evil.example/ukpga/2020/1/data.pdf",
        "https://www.legislation.gov.uk:8443/ukpga/2020/1/data.pdf",
    ],
)
def test_save_refuses_urls_the_corpus_would_refuse(tmp_path, url):
    site = Site({url: b"x"})
    with pytest.raises(BenchCorpusError):
        context(tmp_path, site).files.save(url)
    assert site.requests == []


def test_save_refuses_a_download_over_the_size_cap_and_leaves_nothing(tmp_path):
    site = Site({ACT.format(2020, 1): b"x" * 100})
    ctx = context(tmp_path, site)
    ctx.files.max_bytes = 50
    with pytest.raises(BenchCorpusError):
        ctx.files.save(ACT.format(2020, 1))
    assert list((tmp_path / "cache").iterdir()) == []


def test_legislation_pins_stable_documents_and_reports_the_rest(tmp_path):
    stable, big = text_pdf([SENTENCE, SENTENCE]), text_pdf([SENTENCE] * 101)
    site = Site(
        {
            ACT.format(2020, 1): stable,
            ACT.format(2020, 2): [
                text_pdf([SENTENCE, "One."]),
                text_pdf([SENTENCE, "Two."]),
            ],
            ACT.format(2020, 3): big,
        }
    )
    ctx = context(tmp_path, site)
    found = curate.legislation(ctx, 10, range(2020, 2019, -1), range(1, 5))
    assert [document.source_key for document, _ in found] == ["public/ukpga/2020-1"]
    document, path = found[0]
    assert document.sha256 == sha256(stable).hexdigest() and path.is_file()
    assert document.category == "native_pdf" and document.licence == curate.OGL
    assert ctx.report.skipped["unstable"] == ["public/ukpga/2020-2"]
    assert ctx.report.skipped["too_long"] == ["public/ukpga/2020-3"]
    assert ctx.report.skipped["unavailable"] == ["public/ukpga/2020-4"]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [document.sha256]


def test_stability_costs_two_requests_per_pinned_document(tmp_path):
    site = Site({ACT.format(2020, 1): text_pdf([SENTENCE])})
    curate.legislation(context(tmp_path, site), 1, range(2020, 2019, -1), range(1, 3))
    assert site.requests == [ACT.format(2020, 1)] * 2


def test_legislation_stops_at_the_limit(tmp_path):
    pdf = text_pdf([SENTENCE])
    site = Site({ACT.format(2020, n): pdf for n in range(1, 6)})
    ctx = context(tmp_path, site)
    assert len(curate.legislation(ctx, 2, range(2020, 2019, -1), range(1, 6))) == 2


def test_scans_derive_from_short_acts_without_downloading_again(tmp_path):
    short, long = text_pdf([SENTENCE] * 2), text_pdf([SENTENCE] * 30)
    site = Site({ACT.format(2020, 1): short, ACT.format(2020, 2): long})
    ctx = context(tmp_path, site)
    acts = curate.legislation(ctx, 10, range(2020, 2019, -1), range(1, 3))
    before = len(site.requests)
    scans = curate.scans(acts, 50)
    assert len(site.requests) == before
    ((document, _),) = scans
    assert (
        document.transform == "rasterize@150dpi" and document.category == "scanned_pdf"
    )
    assert document.source_key == "public/ukpga/2020-1-scan"
    assert document.sha256 == acts[0][0].sha256


OAI = (
    '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'
    "{records}{token}</ListRecords></OAI-PMH>"
)
RECORD = (
    '<record><metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/">'
    "<id>{id}</id><title>A  study\n of {id}</title><license>{licence}</license>"
    "</arXiv></metadata></record>"
)
CC_BY = "http://creativecommons.org/licenses/by/4.0/"


def test_arxiv_keeps_only_cc_by_papers_and_follows_the_resumption_token(tmp_path):
    base = "https://export.arxiv.org/oai2?verb=ListRecords"
    first = OAI.format(
        records=RECORD.format(id="2401.00001", licence=CC_BY)
        + RECORD.format(
            id="2401.00002",
            licence="http://arxiv.org/licenses/nonexclusive-distrib/1.0/",
        ),
        token='<resumptionToken xmlns="http://www.openarchives.org/OAI/2.0/">tok/1</resumptionToken>',
    )
    second = OAI.format(records=RECORD.format(id="2401.00003", licence=CC_BY), token="")
    paper = text_pdf([SENTENCE])
    site = Site(
        {
            curate.ARXIV_FIRST_PAGE: first.encode(),
            f"{base}&resumptionToken=tok%2F1": second.encode(),
            "https://arxiv.org/pdf/2401.00001v1": paper,
            "https://arxiv.org/pdf/2401.00003v1": paper,
        }
    )
    ctx = context(tmp_path, site)
    found = curate.arxiv(ctx, 10, 5)
    assert [document.source_key for document, _ in found] == [
        "public/arxiv/2401.00001",
        "public/arxiv/2401.00003",
    ]
    assert found[0][0].title == "A study of 2401.00001"
    assert found[0][0].licence == "CC BY 4.0"


def test_text_sources_pin_commits_tags_and_licence_filters(tmp_path):
    commit = "a" * 40
    raw = "https://raw.githubusercontent.com"
    pep_listing = [
        {"name": "pep-0008.rst"},
        {"name": "pep-0009.rst"},
        {"name": "index.rst"},
    ]
    cpython_listing = [{"name": "abc.py"}, {"name": "data.txt"}, {"name": "ast.py"}]
    site = Site(
        {
            "https://api.github.com/repos/python/peps/commits/main": json.dumps(
                {"sha": commit}
            ).encode(),
            f"https://api.github.com/repos/python/peps/contents/peps?ref={commit}": json.dumps(
                pep_listing
            ).encode(),
            f"{raw}/python/peps/{commit}/peps/pep-0008.rst": b"This document has been placed in the public domain.",
            f"{raw}/python/peps/{commit}/peps/pep-0009.rst": b"Copyright held by the author.",
            f"https://api.github.com/repos/python/cpython/contents/Lib?ref={curate.CPYTHON_TAG}": json.dumps(
                cpython_listing
            ).encode(),
            f"{raw}/python/cpython/{curate.CPYTHON_TAG}/Lib/abc.py": b"def abc(): pass\n",
            f"{raw}/python/cpython/{curate.CPYTHON_TAG}/Lib/ast.py": b"def ast(): pass\n",
        }
    )
    ctx = context(tmp_path, site)
    peps = curate.peps(ctx, 10)
    assert [d.source_key for d, _ in peps] == ["public/peps/pep-0008"]
    assert peps[0][0].url == f"{raw}/python/peps/{commit}/peps/pep-0008.rst"
    assert ctx.report.skipped["licence"] == ["public/peps/pep-0009"]
    code = curate.cpython(ctx, 10)
    assert [d.source_key for d, _ in code] == [
        "public/cpython/abc.py",
        "public/cpython/ast.py",
    ]
    assert {d.category for d, _ in code} == {"code"}


def test_books_and_images_follow_the_published_selection(tmp_path):
    pages = {
        f"https://www.gutenberg.org/cache/epub/{n}/pg{n}.txt": f"Book {n}".encode()
        for n in curate.GUTENBERG
    }
    api = curate.COMMONS_API
    listing = {
        "query": {
            "pages": {
                "1": {
                    "pageid": 1,
                    "title": "File:A.png",
                    "imageinfo": [
                        {
                            "mime": "image/png",
                            "size": 10,
                            "url": "https://upload.wikimedia.org/a.png",
                        }
                    ],
                },
                "2": {
                    "pageid": 2,
                    "title": "File:B.svg",
                    "imageinfo": [
                        {
                            "mime": "image/svg+xml",
                            "size": 10,
                            "url": "https://upload.wikimedia.org/b.svg",
                        }
                    ],
                },
                "3": {
                    "pageid": 3,
                    "title": "File:C.jpg",
                    "imageinfo": [
                        {
                            "mime": "image/jpeg",
                            "size": 9_000_000,
                            "url": "https://upload.wikimedia.org/c.jpg",
                        }
                    ],
                },
            }
        }
    }
    pages[api] = json.dumps(listing).encode()
    pages["https://upload.wikimedia.org/a.png"] = b"\x89PNG"
    ctx = context(tmp_path, Site(pages))
    books = curate.books(ctx)
    assert len(books) == 10 and {d.category for d, _ in books} == {"book"}
    images = curate.images(ctx, 40)
    assert [(d.source_key, d.media_type) for d, _ in images] == [
        ("public/commons/1", "image/png")
    ]


def test_curated_queries_anchor_pdfs_to_pages_and_text_to_character_ranges(tmp_path):
    pdf = text_pdf(["", SENTENCE])
    prose = ("Short.\n" + SENTENCE).encode()
    site = Site(
        {ACT.format(2020, 1): pdf, "https://raw.githubusercontent.com/p.txt": prose}
    )
    ctx = context(tmp_path, site)
    acts = curate.legislation(ctx, 1, range(2020, 2019, -1), range(1, 2))
    text = curate.pin(
        curate.Source(
            key="public/p",
            title="P",
            category="text",
            media_type="text/plain",
            url="https://raw.githubusercontent.com/p.txt",
            licence="CC0-1.0",
            format="text",
        ),
        ctx.files.save("https://raw.githubusercontent.com/p.txt"),
    )
    first = curate.curated_queries(acts + [text], random.Random(3), 10)
    again = curate.curated_queries(acts + [text], random.Random(3), 10)
    assert first == again and len(first) == 2
    anchors = {q.labels[0].source_key: q.labels[0].anchor for q in first}
    assert anchors["public/ukpga/2020-1"].page == 2
    span = anchors["public/p"]
    assert SENTENCE in prose.decode()[span.char_start : span.char_end]
    assert len(curate.curated_queries(acts + [text], random.Random(3), 1)) == 1


def test_run_writes_a_corpus_that_fetches_offline_from_the_cache(tmp_path):
    pdf = text_pdf([SENTENCE] * 2)
    pages: dict[str, bytes | list[bytes]] = {ACT.format(2020, 1): pdf}
    pages[curate.ARXIV_FIRST_PAGE] = OAI.format(records="", token="").encode()
    pages.update(
        {
            f"https://www.gutenberg.org/cache/epub/{n}/pg{n}.txt": (
                SENTENCE * 3
            ).encode()
            for n in curate.GUTENBERG
        }
    )
    commit = "b" * 40
    pages["https://api.github.com/repos/python/peps/commits/main"] = json.dumps(
        {"sha": commit}
    ).encode()
    pages[f"https://api.github.com/repos/python/peps/contents/peps?ref={commit}"] = (
        b"[]"
    )
    pages[
        f"https://api.github.com/repos/python/cpython/contents/Lib?ref={curate.CPYTHON_TAG}"
    ] = b"[]"
    pages[curate.COMMONS_API] = json.dumps({"query": {"pages": {}}}).encode()
    site = Site(pages)
    ctx = context(tmp_path, site)
    out = tmp_path / "out"
    limits = curate.Limits(
        years=range(2020, 2019, -1), numbers=range(1, 3), legislation=5, queries=4
    )
    summary = curate.curate(ctx, out, limits)
    corpus = load_corpus("bench-v1", out)
    assert summary["documents"] == {"book": 10, "native_pdf": 1, "scanned_pdf": 1}
    assert len(corpus.documents) == 12
    assert corpus.synthetic.count == curate.TARGET_DOCUMENTS - 12
    assert summary["curated_queries"] == len(corpus.queries) == 4
    assert corpus.allowed_hosts == ("www.gutenberg.org", "www.legislation.gov.uk")
    assert (
        fetch_corpus(corpus, tmp_path / "cache", opener=lambda url: pytest.fail(url))
        == 0
    )
    assert (
        len(
            PdfReader(
                io.BytesIO(
                    (tmp_path / "cache" / corpus.documents[0].sha256).read_bytes()
                )
            ).pages
        )
        == 2
    )
