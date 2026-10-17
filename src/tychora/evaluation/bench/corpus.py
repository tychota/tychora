"""Benchmark corpus definitions, their digest, queries and verified downloads."""

import hashlib
import http.client
import os
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import model_validator

from tychora.contracts.base import Contract, NonEmptyStr, Sha256
from tychora.contracts.identity import digest
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel

from .curation import UnusablePdf, rasterize_pdf
from .synth import PROBE_QUESTION, SynthDocument, SynthSpec, generate

Category = Literal[
    "native_pdf", "scanned_pdf", "book", "text", "code", "image", "synthetic"
]
PROBE_LABEL_LIMIT = 50


class BenchDocument(Contract):
    """A pinned public document: where it lives and the bytes it must have."""

    source_key: NonEmptyStr
    title: NonEmptyStr
    kind: NonEmptyStr
    category: Category
    format: str | None = None
    media_type: NonEmptyStr
    url: NonEmptyStr
    sha256: Sha256
    licence: NonEmptyStr
    transform: Literal["rasterize@150dpi"] | None = None


class BenchQuery(Contract):
    """A benchmark question with its gold labels."""

    query_id: NonEmptyStr
    question: NonEmptyStr
    labels: tuple[GoldLabel, ...]
    probe: bool = False


class BenchCorpus(Contract):
    """Synthetic spec, pinned public documents and curated queries."""

    name: NonEmptyStr
    synthetic: SynthSpec
    allowed_hosts: tuple[NonEmptyStr, ...] = ()
    documents: tuple[BenchDocument, ...]
    queries: tuple[BenchQuery, ...]

    @model_validator(mode="after")
    def downloads_stay_on_allowed_hosts(self) -> "BenchCorpus":
        """Refuse any document URL that is not https on a declared host."""
        for document in self.documents:
            check_url(document.source_key, document.url, self.allowed_hosts)
        return self

    def digest(self) -> str:
        """Return the identity of everything that defines the corpus and queries."""
        return digest("bench-corpus-v1", self.model_dump(mode="json"))


class BenchCorpusError(ValueError):
    """A corpus file or a download that cannot be used as pinned."""


def _allowed(url: str, allowed_hosts: Sequence[str]) -> bool:
    """Tell whether ``url`` is https on the default port of an allowed host."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in allowed_hosts
        and port in (None, 443)
    )


def check_url(source_key: str, url: str, allowed_hosts: Sequence[str]) -> None:
    """Raise unless ``url`` is https on the default port of an allowed host."""
    if not _allowed(url, allowed_hosts):
        raise BenchCorpusError(
            f"{source_key}: {url} is not an https URL on an allowed host"
        )


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https URLs on the allowed hosts."""

    def __init__(self, allowed_hosts: Sequence[str]):
        self.allowed_hosts = tuple(allowed_hosts)

    def redirect_request(self, req, *details):  # ty: ignore[invalid-method-override]
        """Refuse a redirect that leaves https or the allowed hosts.

        urllib calls this positionally as ``(req, fp, code, msg, headers, newurl)``;
        the details stay variadic to keep the override within the argument limit.
        """
        fp, code, msg, headers, newurl = details
        if not _allowed(newurl, self.allowed_hosts):
            raise BenchCorpusError(f"redirect to {newurl} is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` durably next to ``path`` and rename it into place."""
    partial = path.with_name(path.name + ".part")
    try:
        with partial.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _range_label(
    document: SynthDocument, start: int, length: int, collection: str
) -> GoldLabel:
    return GoldLabel(
        collection=collection,
        source_key=document.source_key,
        anchor=CharRangeAnchor(char_start=start, char_end=start + length),
    )


def synth_queries(
    documents: Sequence[SynthDocument], count: int, collection: str = "default"
) -> tuple[BenchQuery, ...]:
    """Return marker queries spread over the documents, plus the strict-mode probe."""
    step = max(1, len(documents) // max(count, 1))
    queries = [
        BenchQuery(
            query_id=f"marker/{document.source_key}",
            question=document.marker,
            labels=(
                _range_label(
                    document, document.marker_start, len(document.marker), collection
                ),
            ),
        )
        for document in list(documents)[::step][:count]
    ]
    probed = [document for document in documents if document.has_verification]
    if probed:
        queries.append(
            BenchQuery(
                query_id="probe/verification-outcome",
                question=PROBE_QUESTION,
                labels=tuple(
                    _range_label(
                        document,
                        document.text.index(PROBE_QUESTION),
                        len(PROBE_QUESTION),
                        collection,
                    )
                    for document in probed[:PROBE_LABEL_LIMIT]
                ),
                probe=True,
            )
        )
    return tuple(queries)


def _under_root(root: Path, relative: str, corpus: Path) -> Path:
    """Return a path named by a corpus file, refusing one outside the root."""
    named = Path(relative)
    target = root / named
    if (
        named.is_absolute()
        or ".." in named.parts
        or not target.resolve().is_relative_to(root.resolve())
    ):
        raise BenchCorpusError(f"{corpus} names {relative} outside the benchmarks root")
    return target


def load_corpus(name: str, root: Path) -> BenchCorpus:
    """Read ``<root>/<name>.toml`` and the query file it names under ``root``."""
    path = root / f"{name}.toml"
    try:
        data = tomllib.loads(path.read_text())
        lines = (
            _under_root(root, data["queries"], path).read_text().splitlines()
            if "queries" in data
            else []
        )
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BenchCorpusError(f"{path}: {error}") from None
    if data.get("schema") != "tychora.bench-corpus" or data.get("version") != 1:
        raise BenchCorpusError(f"{path} is not a tychora.bench-corpus version 1 file")
    queries = [
        BenchQuery.model_validate_json(line).model_dump(mode="json")
        for line in lines
        if line
    ]
    return BenchCorpus.from_mapping(
        {
            "name": data["name"],
            "synthetic": data["synthetic"],
            "allowed_hosts": data.get("allowed_hosts", []),
            "documents": data.get("documents", []),
            "queries": queries,
        }
    )


def all_queries(corpus: BenchCorpus) -> tuple[BenchQuery, ...]:
    """Return curated queries followed by the synthetic marker queries and the probe."""
    synthetic = synth_queries(generate(corpus.synthetic), corpus.synthetic.queries)
    return corpus.queries + synthetic


def _guarded_open(hosts: Sequence[str]) -> Callable[[str], Any]:
    director = urllib.request.build_opener(GuardedRedirect(hosts))

    def open_url(url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": "tychora-bench/1"})
        return director.open(request, timeout=120)

    return open_url


def _download(
    document: BenchDocument, opener: Callable[[str], Any], max_bytes: int
) -> bytes:
    """Read one document, naming it in every failure."""
    try:
        with opener(document.url) as response:
            data = response.read(max_bytes + 1)
    except BenchCorpusError as error:
        raise BenchCorpusError(f"{document.source_key}: {error}") from None
    except (OSError, http.client.HTTPException) as error:
        raise BenchCorpusError(
            f"{document.source_key}: download from {document.url} failed: {error!r}"
        ) from None
    if len(data) > max_bytes:
        raise BenchCorpusError(
            f"{document.source_key}: download exceeds {max_bytes} bytes"
        )
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise BenchCorpusError(
            f"{document.source_key}: bytes from {document.url} differ from the"
            " pinned SHA-256 (changed upstream or truncated)"
        )
    return data


def fetch_corpus(
    corpus: BenchCorpus,
    cache: Path,
    opener: Callable[[str], Any] | None = None,
    max_bytes: int = 512 * 1024**2,
) -> int:
    """Download missing public documents into ``cache``; return how many were fetched.

    Only https URLs on the corpus's allowed hosts are opened. A document whose
    bytes differ from the pinned SHA-256 (changed upstream or truncated), or whose
    transfer fails, stops the run by name and leaves nothing in the cache.
    """
    for document in corpus.documents:
        check_url(document.source_key, document.url, corpus.allowed_hosts)
    cache.mkdir(parents=True, exist_ok=True)
    fetch = opener or _guarded_open(corpus.allowed_hosts)
    fetched = 0
    for document in corpus.documents:
        target = cache / document.sha256
        if target.is_file():
            continue
        data = _download(document, fetch, max_bytes)
        try:
            write_atomic(target, data)
        except OSError as error:
            raise BenchCorpusError(
                f"{document.source_key}: cannot store the download: {error!r}"
            ) from None
        fetched += 1
    return fetched


def document_bytes(document: BenchDocument, cache: Path) -> bytes:
    """Return the verified bytes of a fetched document, transformed when derived."""
    data = (cache / document.sha256).read_bytes()
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise BenchCorpusError(
            f"{document.source_key}: cached bytes are damaged; fetch again"
        )
    if document.transform is None:
        return data
    derived = cache / f"{document.sha256}.{document.transform.replace('@', '-')}.pdf"
    seal = derived.with_name(derived.name + ".sha256")
    if derived.is_file() and seal.is_file():
        scan = derived.read_bytes()
        if hashlib.sha256(scan).hexdigest() == seal.read_text().strip():
            return scan
    try:
        scan = rasterize_pdf(data)
    except UnusablePdf as error:
        raise BenchCorpusError(f"{document.source_key}: {error}") from None
    write_atomic(derived, scan)
    write_atomic(seal, hashlib.sha256(scan).hexdigest().encode())
    return scan
