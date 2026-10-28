"""The unit lists a graph traversal filters by, derived from a revision-level view."""

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.graph import GraphScope
from tychora.ports.repositories import ReadTransaction


def graph_scope(tx: ReadTransaction, view: EligibilityView) -> GraphScope | None:
    """Eligible units of eligible extractions, split as traversal reads them.

    A unit of an eligible extraction is eligible exactly when it is not
    excluded: its persisted set names itself and its revision, and exclusions
    are closed under dependency.
    """
    if view.graph is None:
        return None
    flags = [
        row
        for row in tx.eligibility.unit_flags(tuple(sorted(view.extraction_ids)))
        if row.unit_id not in view.excluded_unit_ids
    ]
    unit_filter = view.unit_filter
    return GraphScope(
        snapshot_id=view.snapshot_id,
        policy_epoch=view.policy_epoch,
        scope_digest=view.scope_digest,
        eligibility_policy_digest=view.eligibility_policy_digest,
        eligibility_request_digest=view.eligibility_request_digest,
        receipt=view.graph.receipt,
        revision_ids=frozenset(view.revisions),
        selected_batch_ids=(
            *sorted(view.batch_ids - {view.graph.receipt.generation_id}),
            view.graph.receipt.generation_id,
        ),
        source_unit_ids=frozenset(
            row.unit_id
            for row in flags
            if not row.proxy
            and row.citable
            and (unit_filter is None or row.unit_id in unit_filter)
        ),
        context_unit_ids=frozenset(
            row.unit_id for row in flags if row.proxy or row.citable
        ),
        structural_unit_ids=frozenset(
            row.unit_id for row in flags if not row.proxy and not row.citable
        ),
        blocked_assertion_ids=tx.eligibility.blocked_assertions(
            view.graph.receipt.decision_watermark
        ),
    )
