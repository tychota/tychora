"""Immutable, digest-pinned public gold datasets."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tychora.contracts.base import (
    CodepointRange,
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    Sha256,
)
from tychora.contracts.requests import QueryRequest


class GoldSpan(CodepointRange):
    unit_id: Identifier


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
