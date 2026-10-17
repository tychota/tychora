"""Bounded structural PDF census; never returns extracted text or image bytes."""

from pathlib import Path

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError


def analyse_pdf(path: Path, limits: FrozenJsonMap) -> FrozenJsonMap:
    try:
        from pypdf import PdfReader, apply_configuration
        from pypdf.generic import IndirectObject
    except ImportError:
        raise TychoraError(
            "CAPABILITY_MISSING", "PDF analysis requires the pinned pypdf runtime."
        ) from None

    def limit(name: str, ceiling: int) -> int:
        value = limits.get(name, ceiling)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TychoraError(
                "INVALID_REQUEST", "PDF analysis limit must be a positive integer."
            )
        return min(ceiling, value)

    max_pages = limit("max_pages", 1000)
    max_pixels = limit("max_image_pixels", 40000000)
    max_objects = limit("max_objects", 100000)

    def capacity():
        raise TychoraError(
            "LIMIT_EXCEEDED", "PDF structural analysis exceeds its capacity."
        )

    work = 0

    def walk(value, active, depth=0):
        nonlocal work
        work += 1
        if work > max_objects or depth > 128:
            capacity()
        key = (
            (value.idnum, value.generation)
            if isinstance(value, IndirectObject)
            else id(value)
        )
        if key in active:
            raise TychoraError(
                "INVALID_REQUEST", "PDF contains cyclic image resources."
            )
        obj = value.get_object()
        subtype = obj.get("/Subtype")
        active = active | {key}
        if subtype == "/Image":
            width, height = int(obj["/Width"]), int(obj["/Height"])
            if width < 1 or height < 1 or width * height > max_pixels:
                capacity()
            for mask_name in ("/SMask", "/Mask"):
                mask = obj.get(mask_name)
                if (
                    mask is not None
                    and mask != "/None"
                    and hasattr(mask.get_object(), "get")
                ):
                    walk(mask, active, depth + 1)
            return True
        if subtype == "/Form":
            resources = obj.get("/Resources", {})
            xobjects = resources.get("/XObject", {})
            return any([walk(child, active, depth + 1) for child in xobjects.values()])  # noqa: C419 - preflight every resource
        return False

    try:
        with (
            path.open("rb") as stream,
            apply_configuration(
                maximum_declared_stream_length=64 * 1024 * 1024,
                zlib_maximum_output_length=64 * 1024 * 1024,
                array_based_stream_maximum_output_length=64 * 1024 * 1024,
                page_tree_maximum_entries=max_objects,
                page_tree_maximum_depth=128,
            ),
        ):
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise TychoraError(
                    "INVALID_REQUEST", "Encrypted PDFs are not accepted."
                )
            if (
                sum(len(items) for items in reader.xref.values())
                + len(reader.xref_objStm)
                > max_objects
            ):
                capacity()
            count = len(reader.pages)
            if count > max_pages:
                capacity()
            if count < 1:
                raise TychoraError("INVALID_REQUEST", "PDF contains no pages.")
            native, images, blank, unknown = [], [], [], []
            harmless = {
                b"q",
                b"Q",
                b"cm",
                b"BT",
                b"ET",
                b"Tf",
                b"TL",
                b"Td",
                b"TD",
                b"Tm",
                b"T*",
                b"Tc",
                b"Tw",
                b"Tz",
                b"Ts",
                b"Tr",
                b"rg",
                b"RG",
                b"g",
                b"G",
                b"k",
                b"K",
            }
            for number, page in enumerate(reader.pages, 1):
                resources = page.get("/Resources", {})
                resources = resources.get_object()
                xobjects = (
                    resources.get("/XObject", {}).get_object()
                    if "/XObject" in resources
                    else {}
                )
                has_image = any([walk(child, set()) for child in xobjects.values()])  # noqa: C419 - no short circuit for capacity checks
                # The scale-three planned OCR canvas must also fit the pixel ceiling.
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                if width <= 0 or height <= 0 or width * height * 9 > max_pixels:
                    capacity()
                text = page.extract_text()
                content = page.get_contents()
                operations = content.operations if content else []
                work += len(operations)
                if work > max_objects:
                    capacity()
                if text and text.strip():
                    native.append(number)
                elif has_image:
                    images.append(number)
                elif all(operator in harmless for _, operator in operations):
                    blank.append(number)
                else:
                    unknown.append(number)
            return FrozenJsonMap(
                {
                    "page_count": count,
                    "native_text_pages": native,
                    "image_only_pages": images,
                    "blank_pages": blank,
                    "unknown_pages": unknown,
                    "requires_ocr": bool(images),
                }
            )
    except TychoraError:
        raise
    except Exception:  # noqa: BLE001 - untrusted parser errors must be sanitized
        raise TychoraError(
            "INVALID_REQUEST", "PDF structural analysis failed."
        ) from None
