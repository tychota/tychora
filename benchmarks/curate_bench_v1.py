"""Curate benchmarks/bench-v1.toml and benchmarks/queries/bench-v1.jsonl.

Run once with network access, review the summary, commit both outputs. Every
document is pinned by SHA-256; downloads stream to --cache and are never
committed. Only https URLs on the default port of a declared host are opened,
the same rule ``fetch`` enforces, and a document whose bytes change between two
requests is skipped and reported instead of pinned.

    uv run --locked python benchmarks/curate_bench_v1.py --cache .cache/bench

The run makes about 1,400 to 1,800 requests to legislation.gov.uk (one second
apart), a few hundred to arXiv (two seconds apart), and a few hundred to
GitHub, Project Gutenberg and Wikimedia: expect one to two hours and a few GB
of disk in --cache.
"""

import argparse
import hashlib
import http.client
import json
import os
import random
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from tychora.evaluation.bench.corpus import (
    BenchCorpusError,
    BenchDocument,
    BenchQuery,
    GuardedRedirect,
    check_url,
)
from tychora.evaluation.bench.curation import corpus_toml, known_item, page_known_item
from tychora.evaluation.bench.synth import SynthSpec
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel, PageAnchor

ROOT = Path(__file__).resolve().parent
SEED = 20261016
TARGET_DOCUMENTS = 1000
TARGET_QUERIES = 200
CURATED_QUERIES = 150
MAX_PAGES = 100
SCAN_MAX_PAGES = 20
CHUNK = 1024**2
OGL = "Open Government Licence v3.0"
GUTENBERG = {
    1342: "Pride and Prejudice",
    2701: "Moby Dick",
    1400: "Great Expectations",
    98: "A Tale of Two Cities",
    2600: "War and Peace",
    135: "Les Miserables",
    145: "Middlemarch",
    766: "David Copperfield",
    1399: "Anna Karenina",
    4300: "Ulysses",
}
CPYTHON_TAG = "v3.12.7"
# Hosts documents are pinned to (they become the corpus allowed_hosts) and hosts
# only listings are read from.
DOCUMENT_HOSTS = (
    "www.legislation.gov.uk",
    "arxiv.org",
    "www.gutenberg.org",
    "raw.githubusercontent.com",
    "upload.wikimedia.org",
)
METADATA_HOSTS = ("export.arxiv.org", "api.github.com", "commons.wikimedia.org")
ARXIV_BASE = "https://export.arxiv.org/oai2?verb=ListRecords"
ARXIV_FIRST_PAGE = ARXIV_BASE + "&metadataPrefix=arXiv&set=cs&from=2024-01-01"
ARXIV_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "a": "http://arxiv.org/OAI/arXiv/",
}
CC_BY_4 = "http://creativecommons.org/licenses/by/4.0/"
ARXIV_DELAY = 2.0
COMMONS_API = (
    "https://commons.wikimedia.org/w/api.php?action=query&generator=categorymembers"
    "&gcmtitle=Category:CC-zero&gcmtype=file&gcmlimit=200&prop=imageinfo"
    "&iiprop=url|mime|size&format=json"
)
IMAGE_TYPES = ("image/png", "image/jpeg")
MAX_IMAGE_BYTES = 5_000_000
FETCH_ERRORS = (OSError, http.client.HTTPException, BenchCorpusError)
Entry = tuple[BenchDocument, Path]
Opener = Callable[[str], Any]


@dataclass(frozen=True)
class Fetched:
    """A download that was streamed into the cache under its digest."""

    path: Path
    sha256: str
    size: int


class Fetcher:
    """Stream https downloads from declared hosts into a content-addressed cache."""

    def __init__(
        self,
        opener: Opener,
        cache: Path,
        hosts: Sequence[str],
        pause: Callable[[float], None] = time.sleep,
        delay: float = 1.0,
    ):
        self.opener = opener
        self.cache = cache
        self.hosts = tuple(hosts)
        self.pause = pause
        self.delay = delay
        self.max_bytes = 512 * 1024**2

    def _chunks(self, url: str) -> Iterator[bytes]:
        check_url(url, url, self.hosts)
        total = 0
        try:
            with self.opener(url) as response:
                while chunk := response.read(CHUNK):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise BenchCorpusError(f"{url}: over {self.max_bytes} bytes")
                    yield chunk
        finally:
            self.pause(self.delay)

    def save(self, url: str) -> Fetched:
        """Stream ``url`` to ``cache/<sha256>`` without holding it in memory."""
        self.cache.mkdir(parents=True, exist_ok=True)
        partial = self.cache / f".{uuid.uuid4().hex}.part"
        digest, size = hashlib.sha256(), 0
        try:
            with partial.open("wb") as handle:
                for chunk in self._chunks(url):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            target = self.cache / digest.hexdigest()
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return Fetched(target, digest.hexdigest(), size)

    def probe(self, url: str) -> str:
        """Return the SHA-256 of ``url`` without keeping the bytes."""
        digest = hashlib.sha256()
        for chunk in self._chunks(url):
            digest.update(chunk)
        return digest.hexdigest()

    def read(self, url: str) -> bytes:
        """Return a small listing in memory (the size cap applies)."""
        return b"".join(self._chunks(url))


@dataclass
class Report:
    """Documents that were not pinned, by reason."""

    skipped: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def skip(self, reason: str, key: str) -> None:
        """Record that ``key`` was left out."""
        self.skipped[reason].append(key)


@dataclass
class Context:
    """The fetchers for documents and listings, and what was skipped."""

    files: Fetcher
    meta: Fetcher
    report: Report


@dataclass(frozen=True, kw_only=True)
class Limits:
    """How much of each source to take; tests shrink them."""

    legislation: int = 400
    arxiv: int = 200
    scans: int = 50
    peps: int = 200
    cpython: int = 100
    images: int = 40
    queries: int = CURATED_QUERIES
    years: range = range(2023, 2000, -1)
    numbers: range = range(1, 61)
    arxiv_pages: int = 20


@dataclass(frozen=True, kw_only=True)
class Source:
    """Where a document comes from and how it is classified."""

    key: str
    title: str
    category: Any
    media_type: str
    url: str
    licence: str
    format: str | None = None
    transform: Any = None


def pin(source: Source, fetched: Fetched) -> Entry:
    """Pin a downloaded file by its SHA-256."""
    document = BenchDocument(
        source_key=source.key,
        title=source.title,
        kind="document@1",
        category=source.category,
        format=source.format,
        media_type=source.media_type,
        url=source.url,
        sha256=fetched.sha256,
        licence=source.licence,
        transform=source.transform,
    )
    return document, fetched.path


def page_count(path: Path) -> int:
    """Number of pages of a PDF on disk."""
    return len(PdfReader(path).pages)


def _pdf_problem(
    ctx: Context, source: Source, fetched: Fetched, *, stable: bool
) -> str | None:
    """Why a fetched PDF must not be pinned, or None."""
    try:
        pages = page_count(fetched.path)
    except (PyPdfError, ValueError, OSError):
        return "unusable"
    if pages > MAX_PAGES:
        return "too_long"
    if not stable:
        return None
    try:
        again = ctx.files.probe(source.url)
    except FETCH_ERRORS:
        return "unavailable"
    return None if again == fetched.sha256 else "unstable"


def fetch_entry(ctx: Context, source: Source) -> Entry | None:
    """Download and pin one document, reporting a failed transfer."""
    try:
        return pin(source, ctx.files.save(source.url))
    except FETCH_ERRORS:
        ctx.report.skip("unavailable", source.key)
        return None


def pdf_entry(ctx: Context, source: Source, *, stable: bool = False) -> Entry | None:
    """Pin a PDF of at most 100 pages; with ``stable`` it must hash alike on two fetches."""
    try:
        fetched = ctx.files.save(source.url)
    except FETCH_ERRORS:
        ctx.report.skip("unavailable", source.key)
        return None
    problem = _pdf_problem(ctx, source, fetched, stable=stable)
    if problem is None:
        return pin(source, fetched)
    fetched.path.unlink(missing_ok=True)
    ctx.report.skip(problem, source.key)
    return None


def legislation(ctx: Context, limit: int, years: range, numbers: range) -> list[Entry]:
    """UK Public General Acts of at most 100 pages with a stable rendering."""
    found: list[Entry] = []
    for year in years:
        for number in numbers:
            if len(found) >= limit:
                return found
            source = Source(
                key=f"public/ukpga/{year}-{number}",
                title=f"UK Public General Act {year} c. {number}",
                category="native_pdf",
                media_type="application/pdf",
                url=f"https://www.legislation.gov.uk/ukpga/{year}/{number}/data.pdf",
                licence=OGL,
            )
            entry = pdf_entry(ctx, source, stable=True)
            if entry is not None:
                found.append(entry)
    return found


def _arxiv_records(ctx: Context, max_pages: int) -> Iterator[tuple[str, str]]:
    """Identifier and title of CC BY 4.0 computer-science papers, page by page."""
    url = ARXIV_FIRST_PAGE
    for _ in range(max_pages):
        tree = ElementTree.fromstring(ctx.meta.read(url))
        for record in tree.iterfind(".//a:arXiv", ARXIV_NS):
            if record.findtext("a:license", "", ARXIV_NS) == CC_BY_4:
                title = record.findtext("a:title", "", ARXIV_NS)
                yield record.findtext("a:id", "", ARXIV_NS), " ".join(title.split())
        token = tree.findtext(".//oai:resumptionToken", None, ARXIV_NS)
        if not token:
            return
        url = f"{ARXIV_BASE}&resumptionToken={urllib.parse.quote(token, safe='')}"


def arxiv(ctx: Context, limit: int, max_pages: int) -> list[Entry]:
    """CC BY 4.0 arXiv papers of at most 100 pages."""
    found: list[Entry] = []
    try:
        for identifier, title in _arxiv_records(ctx, max_pages):
            if len(found) >= limit:
                break
            source = Source(
                key=f"public/arxiv/{identifier}",
                title=title,
                category="native_pdf",
                media_type="application/pdf",
                url=f"https://arxiv.org/pdf/{identifier}v1",
                licence="CC BY 4.0",
            )
            entry = pdf_entry(ctx, source)
            ctx.files.pause(ARXIV_DELAY)
            if entry is not None:
                found.append(entry)
    except (*FETCH_ERRORS, ElementTree.ParseError):
        ctx.report.skip("listing", "arxiv")
    return found


def scans(acts: Sequence[Entry], limit: int) -> list[Entry]:
    """The first acts of at most 20 pages, to be rasterized when fetched."""
    short = [(d, path) for d, path in acts if page_count(path) <= SCAN_MAX_PAGES]
    return [
        pin(
            Source(
                key=document.source_key + "-scan",
                title=document.title + " (scan)",
                category="scanned_pdf",
                media_type="application/pdf",
                url=document.url,
                licence=OGL,
                transform="rasterize@150dpi",
            ),
            Fetched(path, document.sha256, path.stat().st_size),
        )
        for document, path in short[:limit]
    ]


def books(ctx: Context) -> list[Entry]:
    """The ten Project Gutenberg books as plain text."""
    sources = [
        Source(
            key=f"public/gutenberg/{number}",
            title=title,
            category="book",
            media_type="text/plain",
            url=f"https://www.gutenberg.org/cache/epub/{number}/pg{number}.txt",
            licence="Public domain in the USA (Project Gutenberg License)",
            format="text",
        )
        for number, title in GUTENBERG.items()
    ]
    return [entry for source in sources if (entry := fetch_entry(ctx, source))]


def _listing(ctx: Context, url: str, name: str) -> Any:
    """A JSON listing, or None (reported) when it cannot be read."""
    try:
        return json.loads(ctx.meta.read(url))
    except (*FETCH_ERRORS, ValueError):
        ctx.report.skip("listing", name)
        return None


def _is_public_domain(data: bytes) -> bool:
    return b"public domain" in data.lower() or b"CC0" in data


def peps(ctx: Context, limit: int) -> list[Entry]:
    """Python Enhancement Proposals at one pinned commit that state public domain or CC0."""
    head = _listing(
        ctx, "https://api.github.com/repos/python/peps/commits/main", "peps"
    )
    if head is None:
        return []
    commit = head["sha"]
    listing = _listing(
        ctx,
        f"https://api.github.com/repos/python/peps/contents/peps?ref={commit}",
        "peps",
    )
    names = sorted(
        item["name"]
        for item in listing or []
        if item["name"].startswith("pep-") and item["name"].endswith(".rst")
    )
    found: list[Entry] = []
    for name in names:
        if len(found) >= limit:
            break
        source = Source(
            key=f"public/peps/{name[:-4]}",
            title=name[:-4].upper(),
            category="text",
            media_type="text/plain",
            url=f"https://raw.githubusercontent.com/python/peps/{commit}/peps/{name}",
            licence="Public domain or CC0-1.0",
            format="text",
        )
        entry = fetch_entry(ctx, source)
        if entry is None:
            continue
        if _is_public_domain(entry[1].read_bytes()):
            found.append(entry)
        else:
            entry[1].unlink(missing_ok=True)
            ctx.report.skip("licence", source.key)
    return found


def cpython(ctx: Context, limit: int) -> list[Entry]:
    """Top-level modules of ``Lib/`` at the pinned CPython tag."""
    listing = _listing(
        ctx,
        f"https://api.github.com/repos/python/cpython/contents/Lib?ref={CPYTHON_TAG}",
        "cpython",
    )
    names = sorted(i["name"] for i in listing or [] if i["name"].endswith(".py"))
    sources = [
        Source(
            key=f"public/cpython/{name}",
            title=f"CPython Lib/{name}",
            category="code",
            media_type="text/plain",
            url=f"https://raw.githubusercontent.com/python/cpython/{CPYTHON_TAG}/Lib/{name}",
            licence="PSF-2.0",
            format="text",
        )
        for name in names[:limit]
    ]
    return [entry for source in sources if (entry := fetch_entry(ctx, source))]


def _usable_image(info: dict[str, Any]) -> bool:
    return info.get("mime") in IMAGE_TYPES and info.get("size", 0) <= MAX_IMAGE_BYTES


def images(ctx: Context, limit: int) -> list[Entry]:
    """CC0 Wikimedia Commons PNG and JPEG files of at most 5 MB."""
    listing = _listing(ctx, COMMONS_API, "commons")
    pages = sorted(
        ((listing or {}).get("query", {}).get("pages", {})).values(),
        key=lambda page: page["title"],
    )
    sources = [
        Source(
            key=f"public/commons/{page['pageid']}",
            title=page["title"],
            category="image",
            media_type=info["mime"],
            url=info["url"],
            licence="CC0-1.0",
        )
        for page in pages
        if _usable_image(info := (page.get("imageinfo") or [{}])[0])
    ]
    return [entry for source in sources[:limit] if (entry := fetch_entry(ctx, source))]


def _pdf_item(path: Path, rng: random.Random) -> tuple[str, PageAnchor] | None:
    pages = [page.extract_text() or "" for page in PdfReader(path).pages]
    item = page_known_item(pages, rng)
    return None if item is None else (item[0], PageAnchor(page=item[1]))


def _text_item(path: Path, rng: random.Random) -> tuple[str, CharRangeAnchor] | None:
    found = known_item(path.read_bytes().decode("utf-8", errors="replace"), rng)
    if found is None:
        return None
    return found[0], CharRangeAnchor(char_start=found[1], char_end=found[2])


def _query_for(
    document: BenchDocument, path: Path, rng: random.Random
) -> BenchQuery | None:
    try:
        if document.category == "native_pdf":
            item = _pdf_item(path, rng)
        else:
            item = _text_item(path, rng)
    except (PyPdfError, ValueError, OSError):
        return None
    if item is None:
        return None
    label = GoldLabel(
        collection="default", source_key=document.source_key, anchor=item[1]
    )
    return BenchQuery(
        query_id=f"known/{document.source_key}", question=item[0], labels=(label,)
    )


def curated_queries(
    entries: Sequence[Entry], rng: random.Random, limit: int
) -> list[BenchQuery]:
    """Known-item queries over PDFs, books, text and code, at most ``limit``."""
    kinds = ("native_pdf", "book", "text", "code")
    candidates = [item for item in entries if item[0].category in kinds]
    rng.shuffle(candidates)
    queries: list[BenchQuery] = []
    for document, path in candidates:
        if len(queries) >= limit:
            break
        query = _query_for(document, path, rng)
        if query is not None:
            queries.append(query)
    return queries


def collect(ctx: Context, limits: Limits) -> list[Entry]:
    """Gather every source in the published order."""
    acts = legislation(ctx, limits.legislation, limits.years, limits.numbers)
    return [
        *acts,
        *arxiv(ctx, limits.arxiv, limits.arxiv_pages),
        *scans(acts, limits.scans),
        *books(ctx),
        *peps(ctx, limits.peps),
        *cpython(ctx, limits.cpython),
        *images(ctx, limits.images),
    ]


def curate(ctx: Context, output: Path, limits: Limits) -> dict[str, Any]:
    """Write the corpus and query files into ``output`` and return a summary."""
    entries = collect(ctx, limits)
    queries = curated_queries(entries, random.Random(SEED), limits.queries)
    documents = [document for document, _ in entries]
    synthetic = SynthSpec(
        seed=SEED,
        count=max(0, TARGET_DOCUMENTS - len(documents)),
        queries=max(0, TARGET_QUERIES - 1 - len(queries)),
    )
    (output / "queries").mkdir(parents=True, exist_ok=True)
    lines = "".join(query.model_dump_json() + "\n" for query in queries)
    (output / "queries" / "bench-v1.jsonl").write_text(lines)
    header = (
        "Engine-core benchmark corpus bench-v1, generated by benchmarks/curate_bench_v1.py.\n"
        "Public documents are pinned by SHA-256 and fetched into a cache; never commit them."
    )
    (output / "bench-v1.toml").write_text(
        corpus_toml("bench-v1", synthetic, documents, "queries/bench-v1.jsonl", header)
    )
    return {
        "documents": dict(Counter(document.category for document in documents)),
        "synthetic": synthetic.count,
        "curated_queries": len(queries),
        "skipped": {reason: len(keys) for reason, keys in ctx.report.skipped.items()},
        "unstable": ctx.report.skipped.get("unstable", []),
    }


def default_context(cache: Path) -> Context:
    """Fetchers that only open https URLs on their own declared hosts."""

    def opener(hosts: Sequence[str]) -> Opener:
        director = urllib.request.build_opener(GuardedRedirect(hosts))

        def open_url(url: str) -> Any:
            headers = {"User-Agent": "tychora-bench-curation/1"}
            return director.open(
                urllib.request.Request(url, headers=headers), timeout=120
            )

        return open_url

    files = Fetcher(opener(DOCUMENT_HOSTS), cache, DOCUMENT_HOSTS)
    meta = Fetcher(opener(METADATA_HOSTS), cache, METADATA_HOSTS)
    meta.max_bytes = 32 * 1024**2
    return Context(files=files, meta=meta, report=Report())


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    summary = curate(default_context(args.cache), args.output, Limits())
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
