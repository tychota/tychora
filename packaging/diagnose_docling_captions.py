"""Temporary synthetic PDF caption-layout probe for native qualification."""

import argparse
import json
import runpy
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from tychora.adapters.docling.parser import _Convert

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/documents/create_rich_fixtures.py"
CAPTION = "Figure 1. Reference vessel and blue marker."
VARIANTS = (
    ("near_normal", None),
    ("near_italic", "Helvetica-Oblique"),
    ("near_bold", "Helvetica-Bold"),
)


def caption_draw(original_draw, font):
    def draw(self, x, y, text, *extra, **options):
        if text == CAPTION:
            y = 792 - 510
            if font is not None:
                self.setFont(font, 10)
        return original_draw(self, x, y, text, *extra, **options)

    return draw


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--models", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args()
    build = runpy.run_path(str(FIXTURE))["build"]
    original_draw = Canvas.drawString
    for name, font in VARIANTS:
        directory = args.output / name
        directory.mkdir(parents=True, exist_ok=True)

        Canvas.drawString = caption_draw(original_draw, font)
        try:
            build(directory)
        finally:
            Canvas.drawString = original_draw
        _Convert(str(args.models), "pdf")((directory / "rich.pdf",))
        raw = json.loads((directory / "document.json").read_text())
        print(
            json.dumps(
                {
                    "variant": name,
                    "pictures": [
                        {
                            "self_ref": item["self_ref"],
                            "captions": item.get("captions"),
                            "bbox": [p["bbox"] for p in item.get("prov", [])],
                        }
                        for item in raw["pictures"]
                    ],
                    "caption_items": [
                        {
                            "self_ref": item["self_ref"],
                            "label": item.get("label"),
                            "parent": item.get("parent"),
                            "bbox": [p["bbox"] for p in item.get("prov", [])],
                        }
                        for item in raw["texts"]
                        if CAPTION in item.get("text", "")
                    ],
                },
                sort_keys=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
