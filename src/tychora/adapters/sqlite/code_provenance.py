"""Canonical code-record dependencies; parsing/native span ownership is upstream."""

from tychora.contracts.errors import TychoraError


def _fail():
    raise TychoraError(
        "INTEGRITY_ERROR", "Code record provenance does not match original units"
    )


def _span_matches(span, locator, payloads):
    text = payloads[locator.artifact_ref].decode("utf-8")
    start, end = span["char_start"], span["char_end"]
    return (
        span["coordinate_domain"] == "original_file"
        and locator.char_start <= start < end <= locator.char_end
        and span["byte_start"] == len(text[:start].encode("utf-8"))
        and span["byte_end"] == len(text[:end].encode("utf-8"))
        and span["line_start"] == text[:start].count("\n") + 1
        and span["line_end"] == text[: end - 1].count("\n") + 1
    )


def validate_code_record(record, unit, original_units, metadata, payloads):
    data = record.data_json
    locators = [loc for loc in unit.locators if loc.type == "code"]
    if (
        record.record_type != "code_symbol"
        or unit.unit_type != "symbol"
        or data["parent_unit_id"] != unit.parent_id
        or data["relative_path"] != metadata["relative_path"]
        or data["language"] != metadata["language"]
        or len(locators) != 1
        or locators[0].relative_path != data["relative_path"]
        or not _span_matches(data["declaration_span"], locators[0], payloads)
    ):
        _fail()
    for span in data["context_spans"]:
        source = original_units.get(span["source_unit_id"])
        deps = span["dependency_unit_ids"]
        if (
            source is None
            or not source.text
            or source.text_origin != "source"
            or source.unit_id not in deps
            or len(deps) != len(set(deps))
            or any(uid not in original_units for uid in deps)
            or not 0 <= span["char_start"] < span["char_end"] <= len(source.text)
            or span["reason"] == "parent_declaration"
            and source.unit_id != unit.parent_id
        ):
            _fail()
        source_locators = [loc for loc in source.locators if loc.type == "code"]
        if len(source_locators) != 1:
            _fail()
        source_locator = source_locators[0]
        original = span["original_file_locator"]
        if (
            source_locator.artifact_ref != locators[0].artifact_ref
            or original["char_start"] != source_locator.char_start + span["char_start"]
            or original["char_end"] != source_locator.char_start + span["char_end"]
            or not _span_matches(original, source_locator, payloads)
        ):
            _fail()
