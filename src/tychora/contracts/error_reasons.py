"""Closed registry of reasons that refine a public error code.

A reason tells a client and an operator what went wrong more precisely than the
code alone, for example whether to reinstall a model or restore a backup. Other
sub-projects add entries here under their own prefix; nothing outside this
module defines reasons.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Reason:
    """One registered refinement of a public error code."""

    code: str
    reason: str
    retryable: bool
    operator_action: str
    http_status: int | None = None


_HEALTH = "Run `tychora manage health --verify-payloads --verify-native`"
_ENTRIES = (
    Reason(
        "INTEGRITY_ERROR",
        "payload.hash_mismatch",
        False,
        f"{_HEALTH}; restore the workspace from a backup if the payload stays damaged.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "payload.missing",
        False,
        f"{_HEALTH} to list missing payloads, then restore them from a backup.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "canonical.row_invalid",
        False,
        f"{_HEALTH}; the database holds a row this version cannot read.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "canonical.binding_mismatch",
        False,
        f"{_HEALTH}; a stored binding differs from its retained artifact.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "config.drift",
        False,
        "Restore the configuration that produced this snapshot, or run `tychora manage rebuild index`.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "provider.binding_mismatch",
        False,
        "Check the provider model and dimension in the workspace configuration.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "model_asset.mismatch",
        False,
        "Reinstall the model asset with `tychora manage setup`.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "workspace.database_invalid",
        False,
        "Restore the workspace or archive from a backup; the database failed its safety checks.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "runtime.endpoint_unsafe",
        False,
        "Stop the workspace owner, remove its runtime directory and start it again.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "invariant.unexpected",
        False,
        "Report the diagnostic ID; this is a defect in Tychora.",
    ),
    Reason(
        "INTEGRITY_ERROR",
        "invariant.page_oversize",
        False,
        "Report the diagnostic ID; a membership page exceeded its size bound.",
    ),
    Reason(
        "WORKSPACE_BUSY",
        "owner.live",
        True,
        "Another process holds the workspace without an HTTP endpoint; retry when it closes or start it with `tychora serve`.",
    ),
    Reason(
        "WORKSPACE_BUSY",
        "owner.absent",
        False,
        "Start the workspace owner with `tychora serve`, or open the workspace directly.",
    ),
    Reason(
        "WORKSPACE_BUSY",
        "queue.full",
        True,
        "Retry shortly; the workspace queue is full.",
    ),
    Reason(
        "WORKSPACE_BUSY",
        "publication.rebase",
        True,
        "Retry; another publication changed the base snapshot.",
    ),
    Reason(
        "INDEX_INCOMPLETE",
        "channel.unavailable",
        False,
        "Check the channel's provider and index with `tychora manage health`.",
    ),
    Reason(
        "INDEX_INCOMPLETE",
        "batch.missing",
        False,
        "Run `tychora manage rebuild native <channel>`.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "index.rebuild_required",
        False,
        "Run `tychora manage rebuild index`.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "workspace.format_old",
        False,
        "Recreate the workspace with `tychora --workspace NEW manage rebuild workspace --from OLD`.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "workspace.schema_mismatch",
        False,
        "Use a Tychora release that matches the workspace storage format; do not edit the database by hand.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "family.space_mismatch",
        False,
        "Use a document model and a query encoder from the same embedding space.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "family.dtype_unsupported",
        False,
        "Choose an index dtype that the configured vector store supports.",
    ),
    Reason(
        "CAPABILITY_MISSING",
        "vectors.cache_absent",
        False,
        "Run `tychora manage rebuild index` to re-embed the missing batches.",
    ),
    Reason(
        "CAPABILITY_MISSING",
        "storage.version_unsupported",
        False,
        "Install a Python build linked against SQLite 3.38 or newer.",
    ),
    Reason(
        "CAPABILITY_MISSING",
        "model_asset.absent",
        False,
        "Install the model asset with `tychora manage setup`.",
    ),
    Reason(
        "CAPABILITY_MISSING",
        "graph.disabled",
        False,
        "Select a preset that defines relations to enable the graph.",
    ),
    Reason(
        "NOT_FOUND",
        "workspace.absent",
        False,
        "Check the workspace path; the database file is missing.",
    ),
    Reason(
        "INVALID_REQUEST",
        "range.unsatisfiable",
        False,
        "Request a byte range inside the content length.",
        416,
    ),
    Reason(
        "INVALID_REQUEST",
        "range.multiple",
        False,
        "Request a single byte range.",
        416,
    ),
    Reason(
        "INVALID_REQUEST",
        "bench.incomparable",
        False,
        "Compare results of the same suite, profile and runner class; pick a baseline from that runner.",
    ),
    Reason(
        "INVALID_REQUEST",
        "cursor.invalid",
        False,
        "Restart paging without a cursor.",
    ),
    Reason(
        "INVALID_REQUEST",
        "filter.unknown_field",
        False,
        "List the filterable fields with `tychora inspect facets`.",
    ),
    Reason(
        "INVALID_REQUEST",
        "label.missing",
        False,
        "Fix the gold label: no source, revision or unit matches it in this workspace.",
    ),
    Reason(
        "INVALID_REQUEST",
        "label.ambiguous",
        False,
        "Add an edition key to the gold label so that one revision matches.",
    ),
    Reason(
        "INVALID_REQUEST",
        "rebuild.destination_not_empty",
        False,
        "Choose a new, empty workspace directory as the rebuild destination.",
    ),
    Reason(
        "INVALID_REQUEST",
        "rebuild.incomplete",
        False,
        "Delete the destination directory and rerun `tychora manage rebuild workspace --from OLD`.",
    ),
    Reason(
        "LIMIT_EXCEEDED",
        "evaluation.binding_budget",
        False,
        "One cited code file or extraction exceeds the evaluation retained binding budget on its own; evaluate a snapshot without that source.",
    ),
    Reason(
        "LIMIT_EXCEEDED",
        "rebuild.original_too_large",
        False,
        "Raise the rebuild original size limit or drop the oversized original.",
    ),
    Reason(
        "PROVIDER_FAILURE",
        "provider.auth",
        False,
        "Check the provider credential reference in the workspace configuration.",
    ),
    Reason(
        "PROVIDER_FAILURE",
        "provider.unreachable",
        True,
        "Check the network connection to the provider, then retry.",
    ),
    Reason(
        "PROVIDER_FAILURE",
        "provider.rate_limited",
        True,
        "Retry later or raise the provider rate limit.",
    ),
    Reason(
        "PROVIDER_FAILURE",
        "provider.timeout",
        True,
        "Retry; the provider did not answer in time.",
    ),
)

REASONS: Mapping[str, Reason] = MappingProxyType(
    {entry.reason: entry for entry in _ENTRIES}
)
if len(REASONS) != len(_ENTRIES):
    raise ImportError("Error reasons must be unique")


def reason_for(code: str, reason: str) -> Reason:
    """Return the registered reason, refusing unknown reasons and foreign codes."""
    entry = REASONS.get(reason)
    if entry is None:
        raise ValueError(f"Unregistered error reason: {reason}")
    if entry.code != code:
        raise ValueError(f"Reason {reason} belongs to {entry.code}, not {code}")
    return entry
