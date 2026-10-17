"""Helpers that turn public sources into benchmark documents."""

import io

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
