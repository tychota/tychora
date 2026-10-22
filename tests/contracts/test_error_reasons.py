# tests/contracts/test_error_reasons.py
"""The closed reason registry and the version-2 error envelope."""

import json
import re

import pytest

from tychora.contracts.error_reasons import REASONS, reason_for
from tychora.contracts.errors import ERROR_HTTP_STATUS, TychoraError

SPEC_REASONS = {
    "INTEGRITY_ERROR": {
        "payload.hash_mismatch",
        "payload.missing",
        "canonical.row_invalid",
        "canonical.binding_mismatch",
        "config.drift",
        "provider.binding_mismatch",
        "model_asset.mismatch",
        "invariant.unexpected",
        "invariant.page_oversize",
    },
    "WORKSPACE_BUSY": {"owner.live", "queue.full", "publication.rebase"},
    "INDEX_INCOMPLETE": {"channel.unavailable", "batch.missing"},
    "PROFILE_INCOMPATIBLE": {
        "index.rebuild_required",
        "workspace.format_old",
        "family.space_mismatch",
        "family.dtype_unsupported",
    },
    "CAPABILITY_MISSING": {
        "vectors.cache_absent",
        "model_asset.absent",
        "graph.disabled",
    },
    "INVALID_REQUEST": {
        "range.unsatisfiable",
        "range.multiple",
        "cursor.invalid",
        "filter.unknown_field",
        "label.missing",
        "label.ambiguous",
        "rebuild.destination_not_empty",
        "rebuild.incomplete",
    },
    "LIMIT_EXCEEDED": {"rebuild.original_too_large"},
    "PROVIDER_FAILURE": {
        "provider.auth",
        "provider.unreachable",
        "provider.rate_limited",
        "provider.timeout",
    },
}

# Reasons that classify existing storage, owner and endpoint failures.
CLASSIFIED_REASONS = {
    "NOT_FOUND": {"workspace.absent", "snapshot.absent"},
    "FORBIDDEN_SCOPE": {"scope.whole_workspace"},
    "JOB_INTERRUPTED": {"job.orphaned"},
    "WORKSPACE_BUSY": {"owner.absent", "family.claimed", "storage.busy"},
    "PROFILE_INCOMPATIBLE": {"workspace.schema_mismatch"},
    "INTEGRITY_ERROR": {
        "workspace.database_invalid",
        "runtime.endpoint_unsafe",
        "runtime.path_unsafe",
        "closure.limit_exceeded",
    },
    "CAPABILITY_MISSING": {"storage.version_unsupported", "native.unavailable"},
    "LIMIT_EXCEEDED": {"evaluation.binding_budget"},
    "INVALID_REQUEST": {"bench.incomparable", "review.time_naive"},
}


def test_spec_reasons_are_registered_under_their_codes():
    for code, reasons in SPEC_REASONS.items():
        for reason in reasons:
            assert REASONS[reason].code == code


RETRYABLE = {
    "job.orphaned",
    "storage.busy",
    "owner.live",
    "queue.full",
    "publication.rebase",
    "family.claimed",
    "provider.unreachable",
    "provider.rate_limited",
    "provider.timeout",
}


def test_registry_is_exactly_the_closed_set():
    expected = {
        reason: code
        for table in (SPEC_REASONS, CLASSIFIED_REASONS)
        for code, reasons in table.items()
        for reason in reasons
    }
    assert {key: entry.code for key, entry in REASONS.items()} == expected


def test_retryable_is_pinned_for_every_reason():
    for key, entry in REASONS.items():
        assert entry.retryable is (key in RETRYABLE), key


def test_every_reason_is_well_formed():
    for key, entry in REASONS.items():
        assert key == entry.reason
        assert entry.code in ERROR_HTTP_STATUS
        assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", entry.reason)
        assert entry.operator_action.endswith(".")
        assert entry.http_status in (None, 416)


def test_reason_for_rejects_unknown_and_foreign_reasons():
    assert reason_for("WORKSPACE_BUSY", "owner.live").retryable is True
    with pytest.raises(ValueError, match="Unregistered"):
        reason_for("INTEGRITY_ERROR", "payload.unknown")
    with pytest.raises(ValueError, match="belongs to"):
        reason_for("INTEGRITY_ERROR", "owner.live")


def test_error_takes_retryable_from_its_reason():
    error = TychoraError("WORKSPACE_BUSY", "Busy.", reason="owner.live")
    assert error.retryable is True
    assert error.reason == "owner.live"
    with pytest.raises(ValueError, match="Retryable flag differs"):
        TychoraError("WORKSPACE_BUSY", "Busy.", retryable=False, reason="owner.live")


def test_reason_overrides_http_status():
    plain = TychoraError("INVALID_REQUEST", "Bad.")
    ranged = TychoraError("INVALID_REQUEST", "Bad range.", reason="range.unsatisfiable")
    assert plain.http_status == 400
    assert ranged.http_status == 416


def test_envelope_v2_carries_reason_and_diagnostic():
    error = TychoraError(
        "INTEGRITY_ERROR",
        "Unexpected failure.",
        reason="invariant.unexpected",
        diagnostic_id="diag-1",
    )
    assert json.loads(error.to_json()) == {
        "schema_version": "2",
        "code": "INTEGRITY_ERROR",
        "message": "Unexpected failure.",
        "retryable": False,
        "reason": "invariant.unexpected",
        "run_or_job_id": None,
        "diagnostic_refs": ["diag-1"],
    }


def test_reasonless_error_keeps_every_v1_field():
    envelope = json.loads(
        TychoraError("NOT_FOUND", "Missing.", retryable=True).to_json()
    )
    assert envelope["schema_version"] == "2"
    assert envelope["reason"] is None
    assert envelope["retryable"] is True
    assert {"code", "message", "retryable", "run_or_job_id", "diagnostic_refs"} <= set(
        envelope
    )


def test_oversized_message_is_truncated_below_8_kib():
    error = TychoraError("INVALID_REQUEST", "x" * 20000, reason="cursor.invalid")
    encoded = error.to_json()
    assert len(encoded.encode()) < 8192
    assert json.loads(encoded)["reason"] == "cursor.invalid"
