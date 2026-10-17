"""Immutable, digest-pinned public gold datasets."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from tychora.contracts.base import (
    CodepointRange,
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    PositiveInt,
    Sha256,
)
from tychora.contracts.operations import LabelFailure
from tychora.contracts.requests import QueryRequest


class GoldSpan(CodepointRange):
    unit_id: Identifier


class NativeRefAnchor(Contract):
    """The unit or units carrying this native reference."""

    native_ref: NonEmptyStr


class CharRangeAnchor(CodepointRange):
    """Units whose locator overlaps this codepoint range of the original."""


class PageAnchor(Contract):
    """Units located on this physical page."""

    page: PositiveInt


class GoldLabel(Contract):
    """A judgement stated in stable terms, resolved per workspace."""

    collection: NonEmptyStr
    source_key: NonEmptyStr
    edition_key: str | None = None
    anchor: NativeRefAnchor | CharRangeAnchor | PageAnchor


class GoldBinding(Contract):
    """The canonical source, revision and units a label resolved to."""

    source_id: Identifier
    revision_id: Identifier
    unit_ids: tuple[Identifier, ...]


class GoldLabelResolver(Protocol):
    """Canonical lookups a label resolution needs, scoped to one snapshot."""

    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]:
        """Return the live sources of a collection carrying this source key."""

    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]:
        """Return the snapshot revisions of a source, the default one without edition."""

    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]:
        """Return the units of the pinned extraction carrying this native reference."""

    def units_by_char_range(
        self, revision_id: str, char_start: int, char_end: int
    ) -> tuple[str, ...]:
        """Return the units whose text locators overlap the codepoint range."""

    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]:
        """Return the units located on this physical page."""


class GoldLabelError(ValueError):
    """A label that resolves to nothing, or to more than one source or revision."""

    def __init__(
        self, reason: Literal["label.missing", "label.ambiguous"], detail: str
    ):
        super().__init__(detail)
        self.reason: Literal["label.missing", "label.ambiguous"] = reason
        self.detail = detail


def _single(values: tuple[str, ...], what: str) -> str:
    if not values:
        raise GoldLabelError("label.missing", f"no {what} matches the label")
    if len(values) > 1:
        raise GoldLabelError(
            "label.ambiguous", f"{len(values)} {what}s match the label"
        )
    return values[0]


def _units(
    label: GoldLabel, revision_id: str, resolver: GoldLabelResolver
) -> tuple[str, ...]:
    anchor = label.anchor
    if isinstance(anchor, NativeRefAnchor):
        return resolver.units_by_native_ref(revision_id, anchor.native_ref)
    if isinstance(anchor, CharRangeAnchor):
        return resolver.units_by_char_range(
            revision_id, anchor.char_start, anchor.char_end
        )
    return resolver.units_by_page(revision_id, anchor.page)


def resolve_gold_binding(label: GoldLabel, resolver: GoldLabelResolver) -> GoldBinding:
    """Resolve a label to its source, revision and units in the pinned snapshot."""
    source_id = _single(
        resolver.source_ids(label.collection, label.source_key), "source"
    )
    revision_id = _single(
        resolver.revision_ids(source_id, label.edition_key), "revision"
    )
    unit_ids = _units(label, revision_id, resolver)
    if not unit_ids:
        raise GoldLabelError("label.missing", "the anchor matches no unit")
    return GoldBinding(source_id=source_id, revision_id=revision_id, unit_ids=unit_ids)


def resolve_gold_label(
    label: GoldLabel, resolver: GoldLabelResolver
) -> tuple[str, ...]:
    """Resolve a label to the IDs of the units it designates."""
    return resolve_gold_binding(label, resolver).unit_ids


class GoldCase(Contract):
    case_id: Identifier
    family_key: Identifier
    split: Literal["development", "test"]
    query: QueryRequest
    expected_source_ids: frozenset[Identifier] = frozenset()
    expected_document_ids: frozenset[Identifier] = frozenset()
    expected_revision_ids: frozenset[Identifier] = frozenset()
    expected_unit_ids: frozenset[Identifier] = frozenset()
    expected_asset_ids: frozenset[Identifier] = frozenset()
    expected_definition_ids: frozenset[Identifier] = frozenset()
    expected_file_ids: frozenset[Identifier] = frozenset()
    forbidden_source_ids: frozenset[Identifier] = frozenset()
    forbidden_revision_ids: frozenset[Identifier] = frozenset()
    forbidden_unit_ids: frozenset[Identifier] = frozenset()
    forbidden_asset_ids: frozenset[Identifier] = frozenset()
    forbidden_definition_ids: frozenset[Identifier] = frozenset()
    forbidden_file_ids: frozenset[Identifier] = frozenset()
    expected_spans: tuple[GoldSpan, ...] = ()
    gold_labels: tuple[GoldLabel, ...] = ()
    forbidden_labels: tuple[GoldLabel, ...] = ()
    graded_relevance: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    file_relevance: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    asset_relevance: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    expected_paths: tuple[tuple[Identifier, ...], ...] = ()
    expected_reference_ids: frozenset[Identifier] = frozenset()
    expected_edge_ids: frozenset[Identifier] = frozenset()
    benchmark: Literal[
        "integrity", "semantic_document", "semantic_code", "semantic_visual"
    ] = "integrity"
    label_provenance: NonEmptyStr = "synthetic mechanics"

    @field_validator("query", mode="before")
    @classmethod
    def parse_query(cls, value):
        return QueryRequest.from_mapping(value) if isinstance(value, dict) else value

    @model_validator(mode="after")
    def disjoint_judgements(self):
        for kind in ("source", "revision", "unit", "asset", "definition", "file"):
            if getattr(self, f"expected_{kind}_ids") & getattr(
                self, f"forbidden_{kind}_ids"
            ):
                raise ValueError("Expected and forbidden judgements overlap")
        if (
            self.benchmark != "integrity"
            and self.label_provenance == "synthetic mechanics"
        ):
            raise ValueError("Semantic benchmarks need independent label provenance")
        for grades in (
            self.graded_relevance,
            self.file_relevance,
            self.asset_relevance,
        ):
            if any(
                type(value) is not int or value < 0 or value > 3
                for value in grades.values()
            ):
                raise ValueError("Relevance grades must be integers from 0 through 3")
        for key in self.graded_relevance:
            is_file = key in self.expected_file_ids
            is_asset = key in self.expected_asset_ids
            if is_file == is_asset:
                raise ValueError("Flat graded relevance has an ambiguous evidence kind")
            if key in self.file_relevance or key in self.asset_relevance:
                raise ValueError("Graded relevance key is repeated across maps")
        if set(self.file_relevance) & set(self.asset_relevance):
            raise ValueError("Graded relevance key is repeated across maps")
        if (
            set(self.file_relevance) & self.expected_asset_ids
            or set(self.asset_relevance) & self.expected_file_ids
        ):
            raise ValueError(
                "Graded relevance key conflicts with another evidence kind"
            )
        return self


class EvaluationDataset(Contract):
    dataset_id: Identifier
    name: NonEmptyStr
    version_sha: Sha256
    cases: tuple[GoldCase, ...]

    @model_validator(mode="after")
    def family_isolation(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case ID")
        splits: dict[str, str] = {}
        for case in self.cases:
            earlier = splits.setdefault(case.family_key, case.split)
            if earlier != case.split:
                raise ValueError("A related family crosses development and test splits")
        return self


def load_dataset(path: Path) -> EvaluationDataset:
    """Load a bounded manifest and JSONL; the exact case bytes are versioned."""
    manifest_path = path / "manifest.json"
    cases_path = path / "cases.jsonl"
    if (
        manifest_path.stat().st_size > 1024 * 1024
        or cases_path.stat().st_size > 64 * 1024 * 1024
    ):
        raise ValueError("Gold dataset exceeds byte limits")
    return load_dataset_bytes(manifest_path.read_bytes(), cases_path.read_bytes())


def load_dataset_bytes(manifest_bytes: bytes, cases_bytes: bytes) -> EvaluationDataset:
    """Validate a received payload without dereferencing server-selected paths."""
    if len(manifest_bytes) > 1024 * 1024 or len(cases_bytes) > 64 * 1024 * 1024:
        raise ValueError("Gold dataset exceeds byte limits")
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != "tychora-gold@1":
        raise ValueError("Unsupported gold dataset format")
    version_sha = sha256(cases_bytes).hexdigest()
    if manifest.get("version_sha") != version_sha:
        raise ValueError("Gold case digest differs from manifest")
    lines = cases_bytes.splitlines()
    if len(lines) != manifest.get("case_count") or len(lines) > 100000:
        raise ValueError("Gold case count differs from manifest")
    cases = tuple(GoldCase.model_validate_json(line) for line in lines)
    return EvaluationDataset(
        dataset_id=manifest["dataset_id"],
        name=manifest["name"],
        version_sha=version_sha,
        cases=cases,
    )


def bind_case(case: GoldCase, resolver: GoldLabelResolver) -> GoldCase:
    """Return the case with its labels resolved into canonical ID judgements."""
    if not case.gold_labels and not case.forbidden_labels:
        return case
    expected = [resolve_gold_binding(label, resolver) for label in case.gold_labels]
    forbidden = [
        resolve_gold_binding(label, resolver) for label in case.forbidden_labels
    ]
    data = case.model_dump(mode="json")

    def merged(name: str, values: list[str]) -> list[str]:
        return sorted(set(data[name]) | set(values))

    data.update(
        expected_source_ids=merged(
            "expected_source_ids", [b.source_id for b in expected]
        ),
        expected_revision_ids=merged(
            "expected_revision_ids", [b.revision_id for b in expected]
        ),
        expected_unit_ids=merged(
            "expected_unit_ids", [unit for b in expected for unit in b.unit_ids]
        ),
        forbidden_source_ids=merged(
            "forbidden_source_ids", [b.source_id for b in forbidden]
        ),
        forbidden_revision_ids=merged(
            "forbidden_revision_ids", [b.revision_id for b in forbidden]
        ),
    )
    return GoldCase.from_mapping(data)


def bind_cases(
    cases: tuple[GoldCase, ...], resolver: GoldLabelResolver
) -> tuple[tuple[GoldCase, ...], tuple[LabelFailure, ...]]:
    """Bind every case; a case with an unresolvable label is reported, not scored."""
    bound: list[GoldCase] = []
    failures: list[LabelFailure] = []
    for case in cases:
        try:
            bound.append(bind_case(case, resolver))
        except GoldLabelError as error:
            failures.append(
                LabelFailure(
                    case_id=case.case_id, reason=error.reason, detail=error.detail
                )
            )
    return tuple(bound), tuple(failures)
