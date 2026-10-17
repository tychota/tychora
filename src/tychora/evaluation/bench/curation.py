"""Helpers that turn public sources into benchmark documents."""

import io
import json
import random
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from tychora.contracts.base import Contract

try:
    import pypdfium2

    RASTERIZE_AVAILABLE = True
except ImportError:  # the documents extra is not installed
    RASTERIZE_AVAILABLE = False


class CurationUnavailable(RuntimeError):
    """A curation step needs the documents extra."""


class UnusablePdf(ValueError):
    """A PDF that cannot be opened or has no pages cannot be rasterized."""


def rasterize_pdf(data: bytes, dpi: int = 150) -> bytes:
    """Render every page to an image and return an image-only PDF (a synthetic scan)."""
    if not RASTERIZE_AVAILABLE:
        raise CurationUnavailable("Rasterizing needs `uv sync --extra documents`.")
    try:
        document = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as error:
        raise UnusablePdf(f"The PDF cannot be opened: {error}") from None
    try:
        if len(document) == 0:
            raise UnusablePdf("The PDF has no pages to rasterize.")
        pages = [
            document[index].render(scale=dpi / 72).to_pil().convert("RGB")
            for index in range(len(document))
        ]
    finally:
        document.close()
    output = io.BytesIO()
    pages[0].save(
        output, "PDF", save_all=True, append_images=pages[1:], resolution=float(dpi)
    )
    return output.getvalue()


_SENTENCE = re.compile(r"[^.!?\n]{40,400}[.!?]")


def known_item(
    text: str, rng: random.Random, words: int = 7
) -> tuple[str, int, int] | None:
    """A query of ``words`` consecutive words and the codepoint range of its sentence."""
    sentences = [
        match
        for match in _SENTENCE.finditer(text)
        if len(match.group().split()) >= words + 2
    ]
    if not sentences:
        return None
    match = rng.choice(sentences)
    tokens = match.group().split()
    first = rng.randrange(0, len(tokens) - words + 1)
    return " ".join(tokens[first : first + words]), match.start(), match.end()


def page_known_item(
    pages: Sequence[str], rng: random.Random, words: int = 7
) -> tuple[str, int] | None:
    """A known-item query drawn from one page, with the page's 1-based number."""
    candidates = [index for index, text in enumerate(pages) if _SENTENCE.search(text)]
    rng.shuffle(candidates)
    for index in candidates:
        item = known_item(pages[index], rng, words)
        if item is not None:
            return item[0], index + 1
    return None


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value, ensure_ascii=True)


def corpus_toml(
    name: str,
    synthetic: Contract,
    documents: Sequence[Contract],
    queries_path: str,
    header: str,
) -> str:
    """Render a ``tychora.bench-corpus`` version 1 file.

    ``allowed_hosts`` lists the host of every document URL, so the file always
    permits exactly the downloads it pins.
    """
    hosts = sorted(
        {
            urlsplit(str(document.model_dump()["url"])).hostname or ""
            for document in documents
        }
    )
    lines = [f"# {line}" for line in header.splitlines()]
    lines += [
        'schema = "tychora.bench-corpus"',
        "version = 1",
        f"name = {_toml_value(name)}",
        f"queries = {_toml_value(queries_path)}",
        f"allowed_hosts = [{', '.join(_toml_value(host) for host in hosts)}]",
        "",
        "[synthetic]",
    ]
    lines += [
        f"{key} = {_toml_value(value)}" for key, value in synthetic.model_dump().items()
    ]
    for document in documents:
        lines += ["", "[[documents]]"]
        lines += [
            f"{key} = {_toml_value(value)}"
            for key, value in document.model_dump().items()
            if value is not None
        ]
    return "\n".join(lines) + "\n"
