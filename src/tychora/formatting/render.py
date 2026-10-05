"""Pure, fixed renderers for already authorized evidence results."""

from __future__ import annotations

import html
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from tychora.contracts.base import Contract, FrozenJsonMap, NonEmptyStr
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import QueryResult
from tychora.contracts.identity import canonical_bytes
from tychora.ports.tokens import TokenCounter

Renderer = Literal[
    "canonical-json@1", "hierarchical-evidence@1", "markdown@1", "agent@1"
]
_KEYS = frozenset(
    {
        "request",
        "hierarchy",
        "answerability",
        "conflicts",
        "citations",
        "visual_evidence",
        "retrieval_trace",
        "budget_contract",
        "coverage",
        "checks",
        "coverage_gaps",
        "decision_requirements",
        "warnings",
        "run_id",
        "snapshot_id",
        "schema_version",
    }
)
_ALIAS_SOURCES = frozenset(
    {
        "request",
        "hierarchy",
        "answerability",
        "conflicts",
        "citations",
        "visual_evidence",
        "retrieval_trace",
    }
)


class OutputProfile(Contract):
    renderer: Renderer = "canonical-json@1"
    lane_order: tuple[NonEmptyStr, ...] = ()
    lane_labels: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    labels: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    aliases: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    include_empty_lanes: bool = True
    title: str = "Evidence pack"
    envelope_tag: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")] = (
        "evidence_context"
    )

    @model_validator(mode="after")
    def fixed_parameters(self) -> Self:
        if len(set(self.lane_order)) != len(self.lane_order):
            raise ValueError("Duplicate lane order entry")
        if any(not isinstance(v, str) for v in self.lane_labels.values()):
            raise ValueError("Lane labels must be literal strings")
        if any(not isinstance(v, str) for v in self.labels.values()):
            raise ValueError("Labels must be literal strings")
        aliases = dict(self.aliases)
        if set(aliases) - _ALIAS_SOURCES or any(
            not isinstance(v, str) or not v for v in aliases.values()
        ):
            raise ValueError("Unsupported field alias")
        if len(set(aliases.values())) != len(aliases) or any(
            v in _KEYS and v != k for k, v in aliases.items()
        ):
            raise ValueError("Field aliases collide")
        return self


def envelope(tag: str, body: str) -> str:
    if (
        not tag
        or not tag.replace("_", "a").replace("-", "a").isalnum()
        or not tag[0].isalpha()
    ):
        raise TychoraError("INVALID_REQUEST", "Invalid envelope tag.")
    return f"<{tag}>\n{html.escape(body, quote=False)}\n</{tag}>"


def _project(result: QueryResult, profile: OutputProfile) -> dict:
    rows = result.model_dump(mode="json")
    groups: dict[str, list[dict]] = {lane: [] for lane in profile.lane_order}
    for segment in rows["segments"]:
        groups.setdefault(segment["lane"], []).append(segment)
    for lane in sorted(groups):
        if lane not in profile.lane_order:
            groups[lane] = groups.pop(lane)
    hierarchy = [
        {
            "lane_id": lane,
            "label": profile.lane_labels.get(lane, lane),
            "segments": segments,
        }
        for lane, segments in groups.items()
        if segments or profile.include_empty_lanes
    ]
    evidence_count = len(rows["segments"]) + len(rows["visuals"])
    if evidence_count == 0:
        answer = "insufficient"
    elif (
        rows["coverage"]["status"] == "complete"
        and not rows["coverage_gaps"]
        and not rows["decision_requirements"]
    ):
        answer = "sufficient"
    else:
        answer = "partial"
    projected = {
        "schema_version": "hierarchical-evidence@1",
        "run_id": rows["run_id"],
        "snapshot_id": rows["snapshot_id"],
        "request": rows["applied_scope"],
        "hierarchy": hierarchy,
        "answerability": {
            "status": answer,
            "missing_evidence": rows["coverage_gaps"],
            "human_decisions": rows["decision_requirements"],
        },
        "conflicts": [
            decision
            for decision in rows["decision_requirements"]
            if "conflict" in decision["reason"].lower()
        ],
        "citations": rows["citations"],
        "visual_evidence": rows["visuals"],
        "retrieval_trace": rows["trace"],
        "budget_contract": rows["budget_contract"],
        "coverage": rows["coverage"],
        "checks": rows["checks"],
        "coverage_gaps": rows["coverage_gaps"],
        "decision_requirements": rows["decision_requirements"],
        "warnings": rows["warnings"],
    }
    for source, alias in profile.aliases.items():
        if alias != source:
            projected[alias] = projected.pop(source)
    return projected


def render_unchecked(result: QueryResult, profile: OutputProfile) -> bytes:
    """Render without budget decisions; no I/O or state mutation."""
    if profile.renderer == "canonical-json@1":
        return canonical_bytes(result.model_dump(mode="json"))
    projected = _project(result, profile)
    if profile.renderer == "hierarchical-evidence@1":
        return canonical_bytes(projected)
    if profile.renderer == "agent@1":
        payload = {
            "title": profile.title,
            "run_id": result.run_id,
            "snapshot_id": result.snapshot_id,
            "coverage": result.coverage.model_dump(mode="json"),
            "checks": {
                "status": result.checks.status,
                "predicates": result.checks.predicates,
                "assertion_ids": result.checks.assertion_ids,
                "assertions": tuple(
                    path.model_dump(mode="json") for path in result.checks.assertions
                ),
                "decisions": tuple(
                    item.model_dump(mode="json") for item in result.checks.decisions
                ),
                "truncated": result.checks.truncated,
            },
            "segments": tuple(
                {
                    "segment_id": segment.segment_id,
                    "source_id": segment.source_id,
                    "revision_id": segment.revision_id,
                    "extraction_id": segment.extraction_id,
                    "kind": segment.kind,
                    "lane": segment.lane,
                    "text_origin": "source",
                    "text": segment.text,
                    "matched_unit_ids": segment.matched_unit_ids,
                    "context_only_unit_ids": segment.context_only_unit_ids,
                    "excerpts": tuple(
                        item.model_dump(mode="json") for item in segment.excerpts
                    ),
                    "context_excerpts": tuple(
                        item.model_dump(mode="json")
                        for item in segment.context_excerpts
                    ),
                    "anchors": tuple(
                        item.model_dump(mode="json") for item in segment.anchors
                    ),
                    "citations": tuple(
                        item.model_dump(mode="json") for item in segment.citations
                    ),
                    "structured_records": tuple(
                        item.model_dump(mode="json")
                        for item in segment.structured_records
                    ),
                    "complete": segment.complete,
                    "truncated": segment.truncated,
                }
                for segment in result.segments
            ),
            "visuals": tuple(
                visual.model_dump(mode="json") for visual in result.visuals
            ),
            "citations": tuple(
                item.model_dump(mode="json") for item in result.citations
            ),
            "decisions": tuple(
                item.model_dump(mode="json") for item in result.decision_requirements
            ),
            "coverage_gaps": tuple(
                item.model_dump(mode="json") for item in result.coverage_gaps
            ),
            "warnings": result.warnings,
        }
        return envelope(
            profile.envelope_tag, canonical_bytes(payload).decode("utf-8")
        ).encode("utf-8")
    labels = profile.labels
    lines = [
        f"# {profile.title}",
        "",
        f"{labels.get('coverage', 'Coverage')}: {projected['coverage']['status']}",
        f"{labels.get('checks', 'Checks')}: {projected['checks']['status']}",
    ]
    lines.extend(f"Warning: {warning}" for warning in projected["warnings"])
    for group in projected[profile.aliases.get("hierarchy", "hierarchy")]:
        if not group["segments"]:
            continue
        lines.extend(("", f"## {group['label']}"))
        for segment in group["segments"]:
            lines.extend(
                (
                    "",
                    f"### {segment['segment_id']}",
                    "",
                    segment["text"],
                    "",
                    "```json",
                    json.dumps(
                        {
                            "citations": segment["citations"],
                            "matched_unit_ids": segment["matched_unit_ids"],
                            "structured_records": segment["structured_records"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "```",
                )
            )
    lines.extend(
        (
            "",
            "```json",
            json.dumps(
                {
                    key: value
                    for key, value in projected.items()
                    if key not in {profile.aliases.get("hierarchy", "hierarchy")}
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "```",
        )
    )
    body = "\n".join(lines)
    if profile.renderer == "agent@1":
        body = envelope(profile.envelope_tag, body)
    return body.encode("utf-8")


def format_result(
    result: QueryResult,
    output_profile: OutputProfile,
    *,
    counter: TokenCounter | None = None,
) -> str:
    rendered = render_unchecked(result, output_profile)
    # UTF-8 byte length is a conservative upper bound when no qualified counter is supplied.
    count = (
        counter.count(rendered.decode("utf-8"))
        if counter is not None
        else len(rendered)
    )
    if (
        count > result.budget_contract.max_tokens
        or len(rendered) > result.budget_contract.max_response_bytes
    ):
        raise TychoraError(
            "OUTPUT_BUDGET_EXCEEDED",
            "Requested rendering exceeds the recorded output budget.",
        )
    return rendered.decode("utf-8")
