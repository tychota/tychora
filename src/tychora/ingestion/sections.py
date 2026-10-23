"""Boundary keys and section paths of units visited in reading order."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tychora.configuration.registry import Registry
from tychora.contracts.identity import digest
from tychora.contracts.sections import (
    ROOT_SECTION_KEY,
    HeadingBudget,
    HeadingLevel,
    SectionEntry,
    boundary_key,
    dump_section_path,
    heading_level_for,
    normalizer_definition,
)

if TYPE_CHECKING:
    from tychora.contracts.documents import DocumentUnit

ARTICLE_NORMALIZERS = frozenset({"numbered_provisions@1", "rule_records@1"})


def native_normalizer_fingerprint(kind: Mapping[str, Any], registry: Registry) -> str:
    """Identity of the native normalizer's output for one kind definition."""
    return digest(
        "native-normalizer-v2", normalizer_definition(kind), registry.schema_digests
    )


@dataclass(frozen=True)
class Placement:
    """Where one unit sits: its innermost boundary key and its enclosing sections."""

    boundary_key: str
    section_path: tuple[SectionEntry, ...]

    def attribute(self) -> list[dict[str, Any]]:
        """The ``section_path`` attribute value stored on the unit."""
        return dump_section_path(self.section_path)


@dataclass(frozen=True)
class Placeable:
    """What placement reads from one unit."""

    native_ref: str
    heading_level: int = 0
    text: str = ""
    unit_type: str = ""
    parent_ref: str | None = None
    record: bool = False


def _record(
    item: Placeable, path: tuple[SectionEntry, ...], parent: Placement | None
) -> Placement:
    """A record is an article, or a clause nested in its parent record."""
    base = parent.section_path if parent is not None else path
    entry = SectionEntry(
        key=boundary_key("article", item.native_ref),
        level="clause" if parent is not None else "article",
        depth=len(base),
    )
    return Placement(entry.key, (*base, entry))


def place_units(
    items: Iterable[Placeable],
    root_key: str,
    headings: Sequence[HeadingLevel] = (),
) -> dict[str, Placement]:
    """Place units read in order.

    A heading closes the open sections at or below its level and opens its own
    section; a record opens an article; a table is its own boundary inside the
    current section; any other unit takes the innermost open section.
    """
    root = SectionEntry(key=root_key, depth=0)
    open_sections: list[tuple[int, SectionEntry]] = []
    records: dict[str, Placement] = {}
    placements: dict[str, Placement] = {}
    budget = HeadingBudget()
    for item in items:
        if item.heading_level:
            while open_sections and open_sections[-1][0] >= item.heading_level:
                open_sections.pop()
        path = (root, *(entry for _, entry in open_sections))
        if item.record:
            placement = _record(item, path, records.get(item.parent_ref or ""))
            records[item.native_ref] = placement
        elif item.heading_level:
            entry = SectionEntry(
                key=boundary_key("section", item.native_ref),
                level=heading_level_for(headings, item.text, budget),
                depth=len(path),
            )
            open_sections.append((item.heading_level, entry))
            placement = Placement(entry.key, (*path, entry))
        elif item.unit_type == "table":
            placement = Placement(boundary_key("table", item.native_ref), path)
        else:
            placement = Placement(path[-1].key, path)
        placements[item.native_ref] = placement
    return placements


TABLE_VIEW_TYPES = frozenset({"table", "table_row"})


def document_placements(
    items: "Iterable[DocumentUnit]", headings: Sequence[HeadingLevel] = ()
) -> dict[str, Placement]:
    """Place Docling items: section headers open sections, tables are their own boundary."""
    return place_units(
        (
            Placeable(
                native_ref=item.native_ref,
                heading_level=item.heading_level,
                text=item.text,
                unit_type=item.unit_type,
            )
            for item in items
        ),
        ROOT_SECTION_KEY,
        headings,
    )


def view_placement(item: Placement, kind: str, native_ref: str) -> Placement:
    """A table or row view is keyed by its table; any other view keeps its item's place."""
    if kind in TABLE_VIEW_TYPES:
        return Placement(boundary_key("table", native_ref), item.section_path)
    return item


def figure_placement(native_ref: str) -> Placement:
    """A standalone image with no section is its own document root."""
    key = boundary_key("figure", native_ref)
    return Placement(key, (SectionEntry(key=key, depth=0),))
