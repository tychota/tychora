"""SQLite FTS5 and identifier search restricted to an eligibility view by joins."""

import json
import sqlite3
from collections import defaultdict
from typing import Literal

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext
from tychora.ports.search import RankedOwner, RankedOwners
from tychora.retrieval.lexical_query import lexical_expression, normalize_identifier

WINDOW_LIMIT = 4000
OWNERS_PER_KIND = 80
EXACT_LIMIT = 32
MAX_IDENTIFIERS = 16
_IN = "IN (SELECT value FROM json_each(?))"
_SEARCH = (
    "SELECT r.chunk_id,c.unit_id,p.asset_id,"
    "bm25(lexical_fts,5.0,3.0,2.0,1.0,0.5) AS score FROM lexical_fts "
    "JOIN lexical_records r ON r.rowid=lexical_fts.rowid "
    "JOIN chunks c ON c.chunk_id=r.chunk_id "
    "JOIN evidence_units u ON u.unit_id=c.unit_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    "WHERE lexical_fts MATCH ? AND r.batch_id IN (SELECT batch_id FROM snapshot_batches "
    "WHERE snapshot_id=? AND slot='lexical') "
    "AND (u.citable=1 OR p.proxy_unit_id IS NOT NULL)"
)
_EXACT = (
    "SELECT i.unit_id FROM identifiers i JOIN evidence_units u ON u.unit_id=i.unit_id "
    "JOIN snapshot_members m ON m.extraction_id=u.extraction_id "
    "WHERE m.snapshot_id=? AND i.namespace=? AND i.normalized_value=? AND u.citable=1 "
    "AND NOT EXISTS (SELECT 1 FROM asset_proxies p WHERE p.proxy_unit_id=u.unit_id)"
)


def _json_ids(ids: frozenset[str]) -> str:
    """One JSON array binding for ``json_each``, in a stable order."""
    return json.dumps(sorted(ids))


def _restriction(
    view: EligibilityView, extraction: str, unit: str, asset: str | None = None
) -> tuple[str, tuple[str, ...]]:
    """Predicates and bindings for the view's extractions, exclusions and filter."""
    clauses: list[str] = []
    args: list[str] = []
    if not view.all_members_eligible:
        clauses.append(f"{extraction} {_IN}")
        args.append(_json_ids(view.extraction_ids))
    if view.excluded_unit_ids:
        clauses.append(f"{unit} NOT {_IN}")
        args.append(_json_ids(view.excluded_unit_ids))
    if asset is not None and view.excluded_asset_ids:
        clauses.append(f"({asset} IS NULL OR {asset} NOT {_IN})")
        args.append(_json_ids(view.excluded_asset_ids))
    if view.unit_filter is not None:
        clauses.append(f"{unit} {_IN}")
        args.append(_json_ids(view.unit_filter))
    return "".join(" AND " + clause for clause in clauses), tuple(args)


def _ranked(
    groups: dict[str, list[tuple[str, float]]],
    kind: Literal["unit", "asset"],
    limit: int,
) -> tuple[RankedOwner, ...]:
    """Dense ranks of the best-scored owners; windows arrive best first."""
    ordered = sorted(groups.items(), key=lambda item: (item[1][0][1], item[0]))
    return tuple(
        RankedOwner(
            key=(kind, owner),
            rank=rank,
            score=windows[0][1],
            window_ids=tuple(chunk for chunk, _ in windows),
        )
        for rank, (owner, windows) in enumerate(ordered[:limit], 1)
    )


def _limit(value: int, upper: int) -> None:
    """Refuse a candidate limit outside ``1..upper``."""
    if not 1 <= value <= upper:
        raise TychoraError(
            "INVALID_REQUEST",
            f"Candidate limit must be 1..{upper}",
            reason="request.limit_exceeded",
        )


class SqliteLexicalSearch:
    """FTS5 ranking and identifier equality inside the caller's read transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def search(
        self,
        view: EligibilityView,
        query: str,
        limit: int,
        ctx: ExecutionContext,
        *,
        stopwords: frozenset[str] = frozenset(),
    ) -> RankedOwners:
        """Rank owners by their best of at most ``WINDOW_LIMIT`` eligible windows."""
        _limit(limit, OWNERS_PER_KIND)
        expression = lexical_expression(query, stopwords)
        if expression is None:
            return RankedOwners(owners=(), detail={"windows": 0, "owners": 0})
        restriction, args = _restriction(
            view, "c.extraction_id", "c.unit_id", "p.asset_id"
        )
        ctx.checkpoint()
        rows = self._db.execute(
            _SEARCH + restriction + " ORDER BY score,r.chunk_id LIMIT ?",
            (expression, view.snapshot_id, *args, WINDOW_LIMIT + 1),
        ).fetchall()
        ctx.checkpoint()
        truncated = len(rows) > WINDOW_LIMIT
        rows = rows[:WINDOW_LIMIT]
        units: dict[str, list[tuple[str, float]]] = defaultdict(list)
        assets: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for chunk, unit, asset, score in rows:
            (assets[asset] if asset is not None else units[unit]).append((chunk, score))
        return RankedOwners(
            owners=(*_ranked(units, "unit", limit), *_ranked(assets, "asset", limit)),
            truncated=truncated,
            detail={"windows": len(rows), "owners": len(units) + len(assets)},
        )

    def exact(
        self,
        view: EligibilityView,
        identifiers: tuple[str, ...],
        limit: int,
        ctx: ExecutionContext,
    ) -> RankedOwners:
        """Citable original units carrying any identifier; every hit is counted."""
        _limit(limit, EXACT_LIMIT)
        if len(identifiers) > MAX_IDENTIFIERS:
            raise TychoraError(
                "INVALID_REQUEST",
                f"At most {MAX_IDENTIFIERS} exact identifiers are accepted",
                reason="request.limit_exceeded",
            )
        try:
            parsed = tuple(normalize_identifier(value) for value in identifiers)
        except ValueError:
            raise TychoraError(
                "INVALID_REQUEST",
                "Identifier requires a registered namespace and value",
                reason="identifier.namespace_unknown",
            ) from None
        restriction, args = _restriction(view, "u.extraction_id", "u.unit_id")
        hits: dict[str, int] = {}
        for index, (namespace, value) in enumerate(parsed):
            ctx.checkpoint()
            for (unit_id,) in self._db.execute(
                _EXACT + restriction + " ORDER BY i.unit_id",
                (view.snapshot_id, namespace, value, *args),
            ):
                hits.setdefault(unit_id, index)
        ordered = sorted(hits, key=lambda unit: (hits[unit], unit))
        return RankedOwners(
            owners=tuple(
                RankedOwner(key=("unit", unit), rank=rank, score=0.0)
                for rank, unit in enumerate(ordered[:limit], 1)
            ),
            truncated=len(hits) > limit,
            detail={"hits": len(hits)},
        )
