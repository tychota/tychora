"""Generated readable PDF fixtures; no supplied content or model invocation."""

import io


def generate_pdf(mode: str = "mixed") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    output = io.BytesIO()
    pdf = Canvas(output, pagesize=(600, 800), invariant=1)
    if mode in {"mixed", "scan"}:
        import numpy as np

        # A visible lightly textured background makes actual image data exceed the
        # JSON cap, rather than appending padding or increasing document pages.
        rng = np.random.default_rng(6)
        image = Image.fromarray(rng.integers(225, 256, (2400, 1800), dtype=np.uint8))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 100, 1740, 700), fill="white")
        font = ImageFont.load_default(size=55)
        for index, line in enumerate(
            (
                "Violet telescope field notes",
                "Synthetic local OCR evidence",
                "Review this derived transcript",
            )
        ):
            draw.text((120, 180 + index * 140), line, font=font, fill="black")
        pdf.drawImage(ImageReader(image), 0, 0, 600, 800)
        pdf.showPage()
    if mode in {"mixed", "native"}:
        pdf.drawString(50, 700, "Native telescope calibration notes")
        pdf.showPage()
    if mode in {"mixed", "blank"}:
        pdf.showPage()
    pdf.save()
    return output.getvalue()
