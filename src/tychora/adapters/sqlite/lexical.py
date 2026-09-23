"""SQLite FTS5 ranking over materialized selected and eligible rows."""

import sqlite3
from collections import defaultdict
from threading import RLock
from typing import Literal

from tychora.contracts.candidates import ChannelCandidate, ChannelCandidates
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import EligibilityView
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.lexical_query import lexical_expression, normalize_identifier


class LexicalIndex:
    def __init__(
        self, db: sqlite3.Connection, *, stopwords: frozenset[str] = frozenset()
    ):
        self.db = db
        self.stopwords = stopwords
        self._lock = RLock()

    def search(
        self,
        view: EligibilityView,
        query: str,
        candidate_limit: int,
        ctx: ExecutionContext,
    ) -> ChannelCandidates:
        if not 1 <= candidate_limit <= 80:
            raise TychoraError(
                "INVALID_REQUEST", "Lexical candidate limit must be 1..80"
            )
        expression = lexical_expression(query, self.stopwords)
        ctx.checkpoint()
        with self._lock:
            if self.db.in_transaction:
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Lexical search requires a fresh read transaction",
                )
            self.db.execute("BEGIN")
            try:
                epoch_row = self.db.execute(
                    "SELECT lexical_statistics_epoch FROM workspaces JOIN snapshots USING(workspace_id) WHERE snapshot_id=?",
                    (view.snapshot_id,),
                ).fetchone()
                if epoch_row is None:
                    raise TychoraError("NOT_FOUND", "Selected snapshot is unavailable")
                epoch = epoch_row[0]
                rows: list[tuple[str, str, str | None, float]] = []
                if expression and view.chunk_ids and view.selected_batch_ids:
                    self.db.execute(
                        "CREATE TEMP TABLE eligible_lexical_rows(rowid INTEGER PRIMARY KEY)"
                    )
                    self.db.execute(
                        "CREATE TEMP TABLE eligible_chunk_ids(chunk_id TEXT PRIMARY KEY)"
                    )
                    self.db.execute(
                        "CREATE TEMP TABLE selected_lexical_batch_ids(batch_id TEXT PRIMARY KEY)"
                    )
                    self.db.executemany(
                        "INSERT INTO eligible_chunk_ids VALUES (?)",
                        ((chunk,) for chunk in view.chunk_ids),
                    )
                    self.db.executemany(
                        "INSERT INTO selected_lexical_batch_ids VALUES (?)",
                        ((batch,) for batch in view.selected_batch_ids),
                    )
                    self.db.execute(
                        """INSERT INTO eligible_lexical_rows
                        SELECT r.rowid FROM lexical_records r
                        JOIN eligible_chunk_ids c ON c.chunk_id=r.chunk_id
                        JOIN selected_lexical_batch_ids b ON b.batch_id=r.batch_id
                        JOIN snapshot_batches s ON s.batch_id=r.batch_id
                          AND s.snapshot_id=? AND s.slot='lexical'""",
                        (view.snapshot_id,),
                    )
                    ctx.checkpoint()
                    rows = self.db.execute(
                        """WITH scored AS MATERIALIZED (
                          SELECT r.rowid, r.chunk_id,
                            bm25(lexical_fts,5.0,3.0,2.0,1.0,0.5) AS score
                          FROM lexical_fts
                          JOIN lexical_records r ON r.rowid=lexical_fts.rowid
                          JOIN eligible_lexical_rows e ON e.rowid=r.rowid
                          WHERE lexical_fts MATCH ?
                        )
                        SELECT scored.chunk_id,c.unit_id,p.asset_id,scored.score
                        FROM scored JOIN chunks c ON c.chunk_id=scored.chunk_id
                        LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id
                        ORDER BY scored.score,scored.chunk_id""",
                        (expression,),
                    ).fetchall()
                ctx.checkpoint()
                sources: dict[str, list[tuple[str, float]]] = defaultdict(list)
                assets: dict[str, list[tuple[str, float]]] = defaultdict(list)
                for chunk, unit, asset, score in rows:
                    if asset is None and unit in view.source_unit_ids:
                        sources[unit].append((chunk, score))
                    elif asset in view.asset_ids and unit in view.proxy_unit_ids:
                        assets[asset].append((chunk, score))
                source_candidates = _rank(sources, "unit", candidate_limit)
                asset_candidates = _rank(assets, "asset", candidate_limit)
                return ChannelCandidates(
                    channel="lexical",
                    snapshot_id=view.snapshot_id,
                    policy_epoch=view.policy_epoch,
                    source_candidates=source_candidates,
                    asset_candidates=asset_candidates,
                    matching_window_count=sum(map(len, sources.values()))
                    + sum(map(len, assets.values())),
                    distinct_candidate_count=len(sources) + len(assets),
                    lexical_statistics_epoch=epoch,
                    complete=len(sources) <= candidate_limit
                    and len(assets) <= candidate_limit,
                )
            finally:
                for table in (
                    "eligible_lexical_rows",
                    "eligible_chunk_ids",
                    "selected_lexical_batch_ids",
                ):
                    self.db.execute(f"DROP TABLE IF EXISTS temp.{table}")
                self.db.rollback()


def _rank(
    groups: dict[str, list[tuple[str, float]]],
    kind: Literal["unit", "asset"],
    limit: int,
) -> tuple[ChannelCandidate, ...]:
    ordered = sorted(
        groups.items(), key=lambda item: (min(score for _, score in item[1]), item[0])
    )
    return tuple(
        ChannelCandidate(
            key=(kind, owner),
            rank=rank,
            matching_window_ids=tuple(chunk for chunk, _ in windows),
            score=min(score for _, score in windows),
        )
        for rank, (owner, windows) in enumerate(ordered[:limit], 1)
    )


def lookup_identifiers(
    store, namespaced: tuple[str, ...], view: EligibilityView, limit: int
) -> ChannelCandidates:
    """Equality lookup restricted to original candidate units in the pinned snapshot."""
    if len(namespaced) > 16 or not 1 <= limit <= 32:
        raise TychoraError(
            "INVALID_REQUEST",
            "Exact lookup accepts up to 16 identifiers and 32 candidates",
        )
    try:
        parsed = tuple(normalize_identifier(value) for value in namespaced)
    except ValueError:
        raise TychoraError(
            "INVALID_REQUEST", "Identifier requires a registered namespace and value"
        ) from None
    hits: dict[str, int] = {}
    for index, (namespace, value) in enumerate(parsed):
        rows = store.db.execute(
            """SELECT i.unit_id FROM identifiers i
            JOIN evidence_units u ON u.unit_id=i.unit_id
            JOIN snapshot_members m ON m.extraction_id=u.extraction_id
            WHERE m.snapshot_id=? AND i.namespace=? AND i.normalized_value=?
            ORDER BY i.unit_id""",
            (view.snapshot_id, namespace, value),
        )
        for (unit_id,) in rows:
            if unit_id in view.source_unit_ids:
                hits.setdefault(unit_id, index)
    ordered = sorted(hits, key=lambda unit: (hits[unit], unit))
    return ChannelCandidates(
        channel="exact",
        snapshot_id=view.snapshot_id,
        policy_epoch=view.policy_epoch,
        source_candidates=tuple(
            ChannelCandidate(key=("unit", unit), rank=rank)
            for rank, unit in enumerate(ordered[:limit], 1)
        ),
        distinct_candidate_count=len(hits),
        complete=len(hits) <= limit,
    )
