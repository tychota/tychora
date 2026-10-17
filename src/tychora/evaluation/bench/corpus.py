"""Benchmark corpus definitions, their digest, queries and verified downloads."""

import hashlib
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from tychora.contracts.base import Contract, NonEmptyStr, Sha256
from tychora.contracts.identity import digest
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel

from .curation import rasterize_pdf
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
    documents: tuple[BenchDocument, ...]
    queries: tuple[BenchQuery, ...]

    def digest(self) -> str:
        """Return the identity of everything that defines the corpus and queries."""
        return digest("bench-corpus-v1", self.model_dump(mode="json"))


class BenchCorpusError(ValueError):
    """A corpus file or a download that cannot be used as pinned."""


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


def load_corpus(name: str, root: Path) -> BenchCorpus:
    """Read ``<root>/<name>.toml`` and the query file it names."""
    path = root / f"{name}.toml"
    try:
        data = tomllib.loads(path.read_text())
        lines = (
            (root / data["queries"]).read_text().splitlines()
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
            "documents": data.get("documents", []),
            "queries": queries,
        }
    )


def all_queries(corpus: BenchCorpus) -> tuple[BenchQuery, ...]:
    """Return curated queries followed by the synthetic marker queries and the probe."""
    synthetic = synth_queries(generate(corpus.synthetic), corpus.synthetic.queries)
    return corpus.queries + synthetic


def _urlopen(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "tychora-bench/1"})
    return urllib.request.urlopen(request, timeout=120)


def fetch_corpus(
    corpus: BenchCorpus,
    cache: Path,
    opener: Callable[[str], Any] | None = None,
    max_bytes: int = 512 * 1024**2,
) -> int:
    """Download missing public documents into ``cache``; return how many were fetched.

    A document whose bytes differ from the pinned SHA-256 (changed upstream or
    truncated) stops the run by name and leaves nothing in the cache.
    """
    cache.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for document in corpus.documents:
        target = cache / document.sha256
        if target.is_file():
            continue
        with (opener or _urlopen)(document.url) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise BenchCorpusError(
                f"{document.source_key}: download exceeds {max_bytes} bytes"
            )
        if hashlib.sha256(data).hexdigest() != document.sha256:
            raise BenchCorpusError(
                f"{document.source_key}: bytes from {document.url} differ from the"
                " pinned SHA-256 (changed upstream or truncated)"
            )
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        partial.replace(target)
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
    if not derived.is_file():
        derived.write_bytes(rasterize_pdf(data))
    return derived.read_bytes()
