"""Instance-owned taps at the pinned SDK's actual cell-to-output boundaries."""

from types import SimpleNamespace

from tychora.contracts.errors import TychoraError

from .ocr_lineage import MAX_ENTRIES, segment_text, text_fields


def invalid():
    raise TychoraError("INTEGRITY_ERROR", "SDK OCR lineage differs from captured cells")


class LineageCapture:
    def __init__(self, pages, *, from_ocr=True):
        self.pages = pages
        self.from_ocr = from_ocr
        self.records = {}
        self.tables = {}

    @property
    def evidence(self):
        return {
            number: SimpleNamespace(cells=value["cells"])
            for number, value in self.pages.copy().items()
        }

    def segment(self, page, cells, join="sanitize"):
        saved = self.pages[page]["cells"]
        ids = []
        for cell in cells:
            index = cell.index
            if not 0 <= index < len(saved):
                invalid()
            actual = {
                "index": index,
                "text": cell.text,
                "from_ocr": cell.from_ocr,
                "confidence": cell.confidence,
                "rect": cell.rect.model_dump(mode="json"),
            }
            if actual != saved[index]:
                invalid()
            ids.append(index)
        segment = {"page": page, "cells": ids, "join": join}
        self.segment_text(segment)
        return segment

    def segment_text(self, segment):
        return segment_text(segment, self.evidence, from_ocr=self.from_ocr)

    def capture_native_page(self, page):
        if self.from_ocr:
            return
        cells = [
            {
                "index": cell.index,
                "text": cell.text,
                "from_ocr": cell.from_ocr,
                "confidence": cell.confidence,
                "rect": cell.rect.model_dump(mode="json"),
            }
            for cell in page.cells
        ]
        if len(cells) > MAX_ENTRIES or any(
            cell["index"] != index or cell["from_ocr"] is not False
            for index, cell in enumerate(cells)
        ):
            invalid()
        previous = self.pages.get(page.page_no)
        if previous is not None and previous != {"cells": cells}:
            invalid()
        self.pages[page.page_no] = {"cells": cells}

    def bind(self, item, element, join="sanitize"):
        segment = self.segment(element.page_no, element.cluster.cells, join)
        if join == "sanitize" and self.segment_text(segment) != element.text:
            invalid()
        projection = (
            "list"
            if item.label == "list_item"
            else "formula"
            if item.label == "formula"
            else "text"
        )
        self.bind_segment(item, segment, projection)

    def bind_segment(self, item, segment, projection="text"):
        if item.self_ref in self.records or len(self.records) >= MAX_ENTRIES:
            invalid()
        record = {
            "native_ref": item.self_ref,
            "kind": "text",
            "projection": projection,
            "segments": [segment],
        }
        self.check_text(item, record)
        self.records[item.self_ref] = record

    def check_text(self, item, record):
        fields = text_fields(record, self.evidence, from_ocr=self.from_ocr)
        if any(getattr(item, key) != value for key, value in fields.items()):
            invalid()


def instrument_reading_order(model, capture):
    class ReadingOrder(type(model)):
        def __call__(self, result):
            capture.records.clear()
            for page in result.pages:
                capture.capture_native_page(page)
            return super().__call__(result)

        def _handle_text_element(self, element, **kwargs):
            item, current_list = super()._handle_text_element(element, **kwargs)
            capture.bind(item, element)
            return item, current_list

        def _merge_elements(self, element, merged_elem, new_item, page_height):
            segment = capture.segment(merged_elem.page_no, merged_elem.cluster.cells)
            if capture.segment_text(segment) != merged_elem.text:
                invalid()
            super()._merge_elements(element, merged_elem, new_item, page_height)
            record = capture.records[new_item.self_ref]
            record["segments"].append(segment)
            capture.check_text(new_item, record)

        def _add_caption_or_footnote(self, elem, out_doc, parent, page_height):
            item = super()._add_caption_or_footnote(elem, out_doc, parent, page_height)
            capture.bind(item, elem)
            return item

        def _add_code_element(self, element, out_doc, parent=None):
            item = super()._add_code_element(element, out_doc, parent)
            capture.bind(item, element)
            return item

        def _add_child_elements(self, element, doc_item, doc):
            start = len(doc.texts)
            super()._add_child_elements(element, doc_item, doc)
            items = doc.texts[start:]
            if len(items) != len(element.cluster.children):
                invalid()
            for item, child in zip(items, element.cluster.children, strict=True):
                capture.bind(
                    item,
                    SimpleNamespace(page_no=element.page_no, cluster=child),
                    "plain",
                )

        def _add_table_element(self, element, out_doc, parent=None):
            item = super()._add_table_element(element, out_doc, parent)
            cells = capture.tables.get((element.page_no, element.cluster.id), [])
            if len(cells) != len(item.data.table_cells):
                invalid()
            for cell, segment in zip(item.data.table_cells, cells, strict=True):
                if cell.text != capture.segment_text(segment):
                    invalid()
            capture.records[item.self_ref] = {
                "native_ref": item.self_ref,
                "kind": "table",
                "cells": cells,
            }
            return item

        def _add_table_children(
            self, element, out_doc, table_item, pictures_by_cell=None
        ):
            super()._add_table_children(element, out_doc, table_item, pictures_by_cell)
            record = capture.records[table_item.self_ref]
            if not record["cells"] and table_item.data.table_cells:
                if any(cell.text for cell in table_item.data.table_cells):
                    invalid()
                record["cells"] = [
                    {"page": element.page_no, "cells": [], "join": "table"}
                    for cell in table_item.data.table_cells
                ]

        def _add_rich_table_pictures(
            self, *, table_item, pictures_by_cell, doc, page_height
        ):
            start = len(doc.texts)
            segments = [
                capture.records[table_item.self_ref]["cells"][index]
                for index in pictures_by_cell
                if table_item.data.table_cells[index].text
            ]
            super()._add_rich_table_pictures(
                table_item=table_item,
                pictures_by_cell=pictures_by_cell,
                doc=doc,
                page_height=page_height,
            )
            added = [
                item
                for item in doc.texts[start:]
                if item.self_ref not in capture.records
            ]
            if len(added) != len(segments):
                invalid()
            for item, segment in zip(added, segments, strict=True):
                capture.bind_segment(item, segment)

    # Reuse the already constructed instance state; no additional SDK/model work.
    wrapped = object.__new__(ReadingOrder)
    wrapped.__dict__.update(model.__dict__)
    return wrapped


class LineageTableStage:
    def __init__(self, model, capture):
        self.model = model
        self.capture = capture
        self.pending = []
        self.page = None
        predictor = model.tf_predictor
        merge = predictor._merge_tf_output

        def record_merge(responses, tokens):
            if self.page is None:
                invalid()
            saved = capture.pages[self.page]["cells"]
            by_id = {}
            for token in tokens:
                index = token["id"]
                if (
                    type(index) is not int
                    or not 0 <= index < len(saved)
                    or index in by_id
                    or token["text"] != saved[index]["text"]
                ):
                    invalid()
                by_id[index] = token
            groups = {}
            for response in responses:
                key = (
                    response["start_col_offset_idx"],
                    response["start_row_offset_idx"],
                )
                ids = groups.setdefault(key, [])
                index = response["cell_id"]
                if index not in by_id:
                    invalid()
                ids.append(index)
            result = merge(responses, tokens)
            segments = [
                {"page": self.page, "cells": ids, "join": "table"}
                for ids in groups.values()
            ]
            if len(result) != len(segments):
                invalid()
            self.pending.append(segments)
            return result

        # Only this child pipeline's predictor instance is instrumented.
        predictor._merge_tf_output = record_merge

    def __getattr__(self, name):
        return getattr(self.model, name)

    def __call__(self, result, pages):
        for page in pages:
            self.capture.capture_native_page(page)
            self.page = page.page_no
            self.pending = []
            yielded = list(self.model(result, (page,)))
            tables = list(page.predictions.tablestructure.table_map.values())
            if len(tables) != len(self.pending):
                invalid()
            for table, cells in zip(tables, self.pending, strict=True):
                self.capture.tables[(page.page_no, table.cluster.id)] = cells
            self.page = None
            yield from yielded
