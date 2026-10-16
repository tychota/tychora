# Engine Core v2, Phase 1: Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every later engine-core phase its measuring stick and its shared vocabulary: a closed error reason registry carried by every transport, store-agnostic repository ports with their first SQLite implementations, workspace format 2 with a rebuild path from older workspaces, stable gold labels, and a benchmark corpus and harness with a recorded retrieval baseline.

**Architecture:** Contracts first (`contracts/error_reasons.py`, `ErrorEnvelope` v2), then ports (`ports/repositories.py`) and their SQLite package (`adapters/sqlite/repositories/`), which absorb the duplicated epoch, watermark and artifact-row reads. Format 2 is a one-line DDL change plus a version receipt; `rebuild_workspace` reads any older workspace read-only through an adapter and re-adds its retained originals through the public service path. The benchmark harness is split into pure measurement code (`tychora.evaluation.bench`) and a thin composition entry point in the interfaces layer, because the evaluation layer may not import the public client.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite (WAL, STRICT), Typer, FastAPI/httpx, MCP 2.2.0, pytest + pytest-asyncio, uv, `tomllib`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` (§4.10 benchmark corpus and harness, §4.11 rebuild `workspace`, §4.12 budgets, §4.13 repository layer and error reasons, §4.14 stable gold labels, §5 migration, §6, §7). Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` ("Produced by phase 1" is binding for names and types).

## Prerequisites

1. Sub-project 0 (code health) is merged: `tools/health/`, `quality/baseline.json`, `[tool.importlinter]` in `pyproject.toml`, gate G24. This worktree does not contain it yet; every `tools.health` command below assumes it. If `uv run --locked python -m tools.health report` fails with "No module named tools", stop: the prerequisite is missing.
2. The Docling neighbour-context fix is merged on `main` with its regression tests in `tests/retrieval/test_segments.py`.
3. `git rev-parse b91d808` resolves (the E6 baseline is measured there in Task 17).

## Global Constraints

- Python `>=3.12,<3.13`. No new runtime dependency. `tomllib` (standard library) reads benchmark TOML.
- Format 2: `schema_version` 2 in `workspaces`, one `src/tychora/resources/migrations/0001.sql`. Opening a format-1 workspace or importing a format-1 archive fails with `PROFILE_INCOMPATIBLE` reason `workspace.format_old`, naming `tychora --workspace NEW manage rebuild workspace --from OLD`. No dual-read code: the only code that reads an older database is the read-only inventory adapter of the rebuild, and it reads only tables whose shape is unchanged since format 1.
- Identity formulas in `src/tychora/contracts/identity.py` are untouched.
- Every new `TychoraError` site in this phase passes `reason=` from `src/tychora/contracts/error_reasons.py`. Existing sites in `adapters/sqlite/migrations.py`, `adapters/sqlite/connection.py` and `interfaces/http/client.py::connect_local` are classified here; other modules are classified by the phase that rewrites them.
- Ports in `src/tychora/ports/` use contracts, IDs, scalars, tuples and `Cursor`; never `sqlite3`, Chroma or Ladybug types, never parameters named `sql`, `where` or `match`.
- New modules contain no raw SQL unless they live under `src/tychora/adapters/` (gate G8: a file absent from the baseline must count zero).
- No new function-body imports (G6), new functions at most 80 lines (G5) and cognitive complexity 15 (G25), no new `ignore_imports` entries (G7). In particular `tychora.evaluation` must not import `tychora.api`, `tychora.clients` or `tychora.interfaces`.
- Comments and docstrings never cite plan tasks, plan files or review IDs (G15 matches `\b[Tt]ask ?[0-9]+\b`).
- Public or synthetic benchmark material only; downloaded corpora live outside the repository (`--cache`), never committed.
- Default suite stays green offline: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`.
- Before every commit step, run `uv run --locked ruff format <changed files>` and `uv run --locked ruff check --fix <changed files>`: code blocks in this plan are correct but not always wrapped to the formatter's line length.

## Review Focus

1. **An error crosses a process or transport boundary** (PDF analysis child, HTTP client rebuilding an error from a response body, MCP, CLI). A person expects the same `code`, `reason` and `retryable` on the other side. Pinned in Task 2 (`test_pdf_analysis_child_keeps_reason`, `test_http_client_rebuilds_reason`).
2. **A workspace or archive written by a previous commit is opened.** A person expects `PROFILE_INCOMPATIBLE` reason `workspace.format_old` naming the rebuild command, never a raw `sqlite3` error or `INTEGRITY_ERROR`, and the old file untouched. Pinned in Task 7 (`test_format_one_workspace_is_refused_untouched`, `test_format_one_archive_import_is_refused`).
3. **`rebuild workspace` is pointed at a non-empty destination, at the old workspace itself, or at an old workspace with withdrawn sources and purged originals.** A person expects a refusal before anything is written, the old workspace byte-for-byte unchanged, and every skipped item listed with its reason. Pinned in Task 9 (`test_rebuild_refuses_non_empty_or_same_destination`, `test_rebuild_reports_skipped_items_and_leaves_old_unchanged`).
4. **A tampered, truncated or oversized cursor token.** A person expects `INVALID_REQUEST` reason `cursor.invalid`, not a 500. Pinned in Task 4 (`test_cursor_rejects_tampered_tokens`).
5. **A public benchmark document changed upstream or a download is truncated.** A person expects `fetch` to refuse that document by name and the run to stop, never to benchmark different bytes silently. Pinned in Task 14 (`test_fetch_refuses_changed_bytes`).

## File Structure

| Path | Responsibility |
| --- | --- |
| `src/tychora/contracts/error_reasons.py` (create) | Closed registry of `Reason` entries and `reason_for` |
| `src/tychora/contracts/errors.py` (modify) | `ErrorEnvelope` v2, `TychoraError(reason=, diagnostic_id=)` |
| `src/tychora/contracts/artifacts.py` (modify) | `ArtifactRef.from_row` (the one row-to-reference constructor) |
| `src/tychora/ports/repositories.py` (create) | `Cursor`, `WorkspaceRepository`, `ArtifactRepository`, `ReadTransaction`, `WriteTransaction`, `UnitOfWork` |
| `src/tychora/adapters/sqlite/repositories/__init__.py`, `workspace.py`, `artifacts.py`, `unit_of_work.py` (create) | SQLite implementations, `artifact_ref`, `SqliteUnitOfWork` |
| `src/tychora/adapters/sqlite/migrations.py`, `connection.py` (modify) | Format 2 receipt, `workspace.format_old` refusals |
| `src/tychora/adapters/sqlite/workspace_inventory.py` (create) | Read-only inventory of an older workspace; collection seeding in a new one |
| `src/tychora/adapters/sqlite/gold_labels.py` (create) | `SqliteGoldLabelResolver` |
| `src/tychora/application/workspace_rebuild.py` (create) | `rebuild_workspace` orchestration over a `RebuildTarget` port |
| `src/tychora/api.py` (modify) | `open_rebuild_target` composition |
| `src/tychora/interfaces/commands/manage.py` (modify) | `manage rebuild native …` and `manage rebuild workspace --from` |
| `src/tychora/evaluation/dataset.py`, `runner.py` (modify), `src/tychora/contracts/operations.py` (modify) | `GoldLabel`, anchors, resolution, `LabelFailure` in reports |
| `src/tychora/evaluation/bench/` (create: `__init__`, `budgets`, `stages`, `results`, `corpus`, `synth`, `curation`, `harness`) | Pure benchmark corpus, measurement and reporting code |
| `src/tychora/interfaces/bench.py` (create) | `python -m tychora.interfaces.bench` entry point and `TychoraBenchClient` |
| `benchmarks/` (create: `bench-small.toml`, `bench-v1.toml`, `queries/bench-v1.jsonl`, `reference-machine.toml`, `online-workspace.toml`, `curate_bench_v1.py`, `evaluation_baseline.py`) | Corpus definitions, curation and baseline scripts |
| `bench-results/baseline/evaluation-b91d808.json` (create) | E6 baseline |
| `.github/workflows/bench.yml` (create), `.gitignore` (modify) | Scheduled benchmark runs, ignored local results |
| `tests/architecture/test_ports_neutrality.py`, `tests/architecture/test_sql_free_modules.py` (create) | Port and layering guards |
| `docs/cli.md`, `docs/evaluation.md`, `docs/performance.md`, `docs/workspaces.md` (modify) | User-facing documentation of the new commands and formats |

Entry-point deviation from the index: the index names `python -m tychora.evaluation.bench`. The layering contract of code health (`interfaces | clients > api > evaluation`) forbids the evaluation layer from opening a workspace through `tychora.Tychora`, so the runnable entry point is `python -m tychora.interfaces.bench` with the same sub-commands and flags; the measurement code stays in `tychora.evaluation.bench`. The index's gate commands are to be read with this module path.

---

### Task 1: Error reason registry and envelope v2

**Files:**
- Create: `src/tychora/contracts/error_reasons.py`
- Modify: `src/tychora/contracts/errors.py:1-77` (whole module)
- Test: `tests/contracts/test_error_reasons.py`

**Interfaces:**
- Consumes: `ERROR_HTTP_STATUS` (existing, `contracts/errors.py:5-26`).
- Produces: `Reason`, `REASONS: Mapping[str, Reason]`, `reason_for(code: str, reason: str) -> Reason`; `ErrorEnvelope` with `schema_version="2"` and `reason: str | None`; `TychoraError(code, message, *, retryable: bool | None = None, reason: str | None = None, diagnostic_id: str | None = None)` with attributes `code`, `message`, `retryable`, `reason`, `diagnostic_id`, property `http_status`, methods `envelope() -> ErrorEnvelope`, `to_json() -> str`.

- [ ] **Step 1: Write the failing tests**

```python
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
    "CAPABILITY_MISSING": {"vectors.cache_absent", "model_asset.absent", "graph.disabled"},
    "INVALID_REQUEST": {
        "range.unsatisfiable",
        "range.multiple",
        "cursor.invalid",
        "filter.unknown_field",
        "label.missing",
        "label.ambiguous",
        "rebuild.destination_not_empty",
    },
    "PROVIDER_FAILURE": {
        "provider.auth",
        "provider.unreachable",
        "provider.rate_limited",
        "provider.timeout",
    },
}


def test_spec_reasons_are_registered_under_their_codes():
    for code, reasons in SPEC_REASONS.items():
        for reason in reasons:
            assert REASONS[reason].code == code


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
    envelope = json.loads(TychoraError("NOT_FOUND", "Missing.", retryable=True).to_json())
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_error_reasons.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.contracts.error_reasons'`.

- [ ] **Step 3: Write the registry**

```python
# src/tychora/contracts/error_reasons.py
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
    Reason("WORKSPACE_BUSY", "queue.full", True, "Retry shortly; the workspace queue is full."),
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
```

- [ ] **Step 4: Rewrite `contracts/errors.py` around the registry**

Replace lines 29-77 (the `ErrorEnvelope` and `TychoraError` classes) with:

```python
class ErrorEnvelope(Contract):
    schema_version: str = "2"
    code: NonEmptyStr
    message: str
    retryable: bool = False
    reason: str | None = None
    run_or_job_id: Identifier | None = None
    diagnostic_refs: tuple[Identifier, ...] = ()


class TychoraError(Exception):
    """Accept only a sanitized public message, never an SDK exception/body."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        reason: str | None = None,
        diagnostic_id: str | None = None,
    ) -> None:
        if code not in ERROR_HTTP_STATUS:
            raise ValueError("Unregistered error code")
        if not isinstance(message, str):
            raise TypeError("Public error message must be a string")
        registered = reason_for(code, reason) if reason is not None else None
        if (
            registered is not None
            and retryable is not None
            and retryable != registered.retryable
        ):
            raise ValueError("Retryable flag differs from the registered reason")
        self.code = code
        self.message = message
        self.reason = reason
        self.retryable = (
            registered.retryable if registered is not None else bool(retryable)
        )
        self.diagnostic_id = diagnostic_id
        super().__init__(message)

    @property
    def http_status(self) -> int:
        if self.reason is not None:
            override = REASONS[self.reason].http_status
            if override is not None:
                return override
        return ERROR_HTTP_STATUS[self.code]

    @property
    def cli_exit_code(self) -> int:
        if self.code == "CANCELLED":
            return 130
        if self.code in {
            "INVALID_REQUEST",
            "BUDGET_TOO_SMALL",
            "OUTPUT_BUDGET_EXCEEDED",
            "LIMIT_EXCEEDED",
        }:
            return 2
        return 1

    def envelope(self) -> ErrorEnvelope:
        """Return the public envelope of this error."""
        return ErrorEnvelope(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            reason=self.reason,
            diagnostic_refs=(self.diagnostic_id,) if self.diagnostic_id else (),
        )

    def to_json(self) -> str:
        """Serialize the envelope, halving the message until it fits in 8 KiB."""
        envelope = self.envelope()
        while True:
            encoded = envelope.model_dump_json()
            if len(encoded.encode("utf-8")) < 8192:
                return encoded
            envelope = envelope.model_copy(
                update={"message": envelope.message[: len(envelope.message) // 2]}
            )
```

and change the import block at the top of the module to:

```python
"""Transport-independent, bounded public error envelopes."""

from .base import Contract, Identifier, NonEmptyStr
from .error_reasons import REASONS, reason_for
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/contracts/test_error_reasons.py tests/contracts/test_models.py -v`
Expected: PASS.

- [ ] **Step 6: Run the whole offline suite for envelope regressions**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release' -x`
Expected: PASS. Any failure comparing a serialized envelope with `"schema_version":"1"` is a fixture to update to `"2"` plus `"reason": null`; update it in the same commit.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/contracts/error_reasons.py src/tychora/contracts/errors.py tests/contracts/test_error_reasons.py
git commit -m "feat: add the closed error reason registry and envelope version 2"
```

---

### Task 2: Reasons survive every transport and process boundary

**Files:**
- Modify: `src/tychora/interfaces/http/client.py:62-72` (`_checked`)
- Modify: `src/tychora/application/pdf_intake.py:888-898` (`_analysis_child`) and `:928-929` (`_isolated_analysis`)
- Modify: `dashboard/src/api/client.ts` only if it validates the envelope's `schema_version` (it does not today; leave it unchanged when `grep -n schema_version dashboard/src/api/client.ts` prints nothing)
- Test: `tests/transports/test_error_reasons.py`

**Interfaces:**
- Consumes: `TychoraError(..., reason=)` from Task 1; `error_response` (`interfaces/http/app.py:33`), `_tool_error` (`interfaces/mcp/server.py:36`), `fail` (`interfaces/commands/_common.py:112`).
- Produces: `_child_error(result: tuple[str, str, str | None]) -> TychoraError` in `application/pdf_intake.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/transports/test_error_reasons.py
"""Error reasons reach HTTP, MCP, CLI, the HTTP client and the PDF child."""

import json

import httpx
import pytest
import typer

from tychora.application import pdf_intake
from tychora.contracts.errors import TychoraError
from tychora.interfaces.commands._common import fail
from tychora.interfaces.http.app import error_response
from tychora.interfaces.http.client import _checked
from tychora.interfaces.mcp.server import _tool_error


def _ranged() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST", "Range is outside the content.", reason="range.unsatisfiable"
    )


def test_http_error_response_carries_reason_and_status_override():
    response = error_response(_ranged())
    assert response.status_code == 416
    assert json.loads(response.body)["reason"] == "range.unsatisfiable"


def test_http_client_rebuilds_reason():
    response = httpx.Response(
        409,
        json={
            "schema_version": "2",
            "code": "WORKSPACE_BUSY",
            "message": "Busy.",
            "retryable": True,
            "reason": "owner.live",
        },
    )
    with pytest.raises(TychoraError) as error:
        _checked(response)
    assert (error.value.code, error.value.reason, error.value.retryable) == (
        "WORKSPACE_BUSY",
        "owner.live",
        True,
    )


def test_http_client_treats_unknown_reason_as_invalid_response():
    response = httpx.Response(
        400, json={"code": "INVALID_REQUEST", "message": "x", "reason": "nope.never"}
    )
    with pytest.raises(TychoraError) as error:
        _checked(response)
    assert error.value.code == "PROVIDER_FAILURE"


def test_mcp_tool_error_carries_reason():
    result = _tool_error(_ranged())
    assert result.is_error
    assert json.loads(result.content[0].text)["reason"] == "range.unsatisfiable"


def test_cli_failure_prints_reason(capsys):
    with pytest.raises(typer.Exit) as exited:
        fail(_ranged())
    assert exited.value.exit_code == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "range.unsatisfiable"


def test_pdf_analysis_child_keeps_reason(monkeypatch):
    import tychora.ingestion.pdf_analysis as analysis

    def broken(path, limits):
        raise TychoraError(
            "INTEGRITY_ERROR", "Unexpected.", reason="invariant.unexpected"
        )

    monkeypatch.setattr(analysis, "analyse_pdf", broken)

    class Sender:
        sent: object = None

        def send(self, value):
            self.sent = value

        def close(self):
            pass

    sender = Sender()
    pdf_intake._analysis_child("unused.pdf", {}, sender)
    assert sender.sent == (False, ("INTEGRITY_ERROR", "Unexpected.", "invariant.unexpected"))
    rebuilt = pdf_intake._child_error(sender.sent[1])
    assert (rebuilt.code, rebuilt.reason) == ("INTEGRITY_ERROR", "invariant.unexpected")
```

- [ ] **Step 2: Run the tests to verify which fail**

Run: `uv run --locked pytest tests/transports/test_error_reasons.py -v`
Expected: `test_http_error_response_carries_reason_and_status_override`, `test_mcp_tool_error_carries_reason` and `test_cli_failure_prints_reason` PASS already (they serialize through `to_json` and `http_status`); `test_http_client_rebuilds_reason` FAILS (reason is `None`) and `test_pdf_analysis_child_keeps_reason` FAILS (`AttributeError: _child_error`).

- [ ] **Step 3: Rebuild the reason in the HTTP client**

Replace `_checked` in `src/tychora/interfaces/http/client.py`:

```python
def _checked(response: httpx.Response) -> httpx.Response:
    if response.status_code >= 400:
        try:
            value = response.json()
            reason = value.get("reason")
            raise TychoraError(
                value["code"],
                value["message"],
                retryable=None if reason else value.get("retryable", False),
                reason=reason,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TychoraError(
                "PROVIDER_FAILURE", "Local HTTP response is invalid."
            ) from None
    return response
```

- [ ] **Step 4: Keep the reason across the PDF analysis child**

In `src/tychora/application/pdf_intake.py`, change the `except TychoraError` branch of `_analysis_child`:

```python
    except TychoraError as error:
        sender.send((False, (error.code, error.message, error.reason)))
    except Exception:  # noqa: BLE001 - sanitize failures at the child boundary
        sender.send((False, ("INVALID_REQUEST", "PDF analysis failed.", None)))
```

add above `_isolated_analysis`:

```python
def _child_error(result: tuple[str, str, str | None]) -> TychoraError:
    """Rebuild a child's typed failure, reason included."""
    code, message, reason = result
    return TychoraError(code, message, reason=reason)
```

and replace `raise TychoraError(*result)` with `raise _child_error(result)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/transports/test_error_reasons.py tests/transports/test_pdf_intake.py tests/ingestion/test_pdf_analysis.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/interfaces/http/client.py src/tychora/application/pdf_intake.py tests/transports/test_error_reasons.py
git commit -m "feat: carry error reasons across HTTP, MCP, CLI and the PDF analysis child"
```

---

### Task 3: A live owner without an endpoint returns WORKSPACE_BUSY

**Files:**
- Modify: `src/tychora/interfaces/http/client.py:905-971` (`connect_local`)
- Test: `tests/transports/test_delegation.py` (add one test and one helper)

**Interfaces:**
- Consumes: reason `owner.live` (Task 1).
- Produces: `connect_local` raises `TychoraError("WORKSPACE_BUSY", …, reason="owner.live")` when the runtime record has no `authority`.

- [ ] **Step 1: Write the failing two-process test**

Append to `tests/transports/test_delegation.py`:

```python
def _hold_standalone(path: Path):
    """Open the workspace in a child process through the Python API, no server."""
    script = """
import sys
from pathlib import Path
from tychora import Tychora
class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"
    def count(self, text):
        return len(text)
with Tychora.open(Path(sys.argv[1]), token_counter=Counter()):
    print("ready", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready", process.stderr
    return process


def test_standalone_owner_without_endpoint_is_busy_not_integrity(tmp_path):
    workspace = tmp_path / "workspace"
    process = _hold_standalone(workspace)
    try:
        with pytest.raises(TychoraError) as error:
            connect_local(workspace)
        assert error.value.code == "WORKSPACE_BUSY"
        assert error.value.reason == "owner.live"
        assert error.value.retryable is True
    finally:
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=30)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/transports/test_delegation.py::test_standalone_owner_without_endpoint_is_busy_not_integrity -v`
Expected: FAIL with `assert 'INTEGRITY_ERROR' == 'WORKSPACE_BUSY'`.

- [ ] **Step 3: Return the busy reason**

In `connect_local`, immediately after `record = json.load(stream)` has been read and the descriptor closed (after the `finally` that closes `fd`), insert:

```python
        if "authority" not in record:
            raise TychoraError(
                "WORKSPACE_BUSY",
                "The workspace is open in another process without an HTTP "
                "endpoint; retry when it closes or start it with `tychora serve`.",
                reason="owner.live",
            )
```

The existing `except TychoraError: raise` clause re-raises it unchanged.

- [ ] **Step 4: Run the delegation tests**

Run: `uv run --locked pytest tests/transports/test_delegation.py tests/cli/test_output.py::test_busy_owner_cli_delegates_discovery -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tychora/interfaces/http/client.py tests/transports/test_delegation.py
git commit -m "fix: report a live owner without an endpoint as retryable WORKSPACE_BUSY"
```

---
### Task 4: Repository ports, keyset cursor and architecture guards

**Files:**
- Create: `src/tychora/ports/repositories.py`
- Create: `tests/architecture/test_ports_neutrality.py`
- Create: `tests/architecture/test_sql_free_modules.py`
- Test: `tests/contracts/test_cursor.py`

**Interfaces:**
- Consumes: `TychoraError(..., reason="cursor.invalid")` (Task 1), `ArtifactRef` (`contracts/artifacts.py:10`), `canonical_bytes` (`contracts/identity.py:28`).
- Produces (exactly as in the index): `Cursor(sort_key: tuple[str | int, ...], last_id: str)` with `encode() -> str` and `Cursor.decode(token: str) -> Cursor`; `WorkspaceRepository`, `ArtifactRepository`, `ReadTransaction`, `WriteTransaction`, `UnitOfWork` protocols; `SQL_FREE_MODULES: tuple[str, ...]` in `tests/architecture/test_sql_free_modules.py`.
- Name clash to know about: `contracts/operations.py:38` already defines `Cursor` as an opaque string alias for transport fields. Modules that need both import this one as `from tychora.ports.repositories import Cursor as KeysetCursor`.

- [ ] **Step 1: Write the failing cursor tests**

```python
# tests/contracts/test_cursor.py
"""Keyset cursors round-trip and reject anything they did not produce."""

import base64

import pytest

from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import Cursor


def test_cursor_round_trips():
    cursor = Cursor(sort_key=("guide", 3), last_id="source-1")
    token = cursor.encode()
    assert "=" not in token
    assert Cursor.decode(token) == cursor


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        "x" * 1025,
        base64.urlsafe_b64encode(b'{"k":[],"id":""}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"k":[1.5],"id":"a"}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"k":[],"id":"a","x":1}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"id":"a","k":[]}').decode(),
        base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("="),
    ],
)
def test_cursor_rejects_tampered_tokens(token):
    with pytest.raises(TychoraError) as error:
        Cursor.decode(token)
    assert (error.value.code, error.value.reason) == ("INVALID_REQUEST", "cursor.invalid")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_cursor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.ports.repositories'`.

- [ ] **Step 3: Write the ports module**

```python
# src/tychora/ports/repositories.py
"""Store-agnostic repository ports and the keyset cursor they page with.

Ports speak in contracts, identifiers, scalars, tuples and cursors. Each store
adapter binds lists and transactions its own way, so a PostgreSQL adapter can
implement the same protocols as the SQLite one.
"""

from __future__ import annotations

import base64
import json
from contextlib import AbstractContextManager
from typing import Protocol

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, NonEmptyStr
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes

MAX_CURSOR_CHARS = 1024


class Cursor(Contract):
    """Keyset position: the sort key of the last returned row and its ID."""

    sort_key: tuple[str | int, ...]
    last_id: NonEmptyStr

    def encode(self) -> str:
        """Return an opaque URL-safe token for this position."""
        raw = canonical_bytes({"id": self.last_id, "k": list(self.sort_key)})
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        """Parse a token produced by ``encode``; anything else is refused."""
        try:
            if not isinstance(token, str) or not 0 < len(token) <= MAX_CURSOR_CHARS:
                raise ValueError("cursor length")
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"id", "k"}:
                raise ValueError("cursor shape")
            if not isinstance(value["k"], list):
                raise ValueError("cursor key")
            cursor = cls(sort_key=tuple(value["k"]), last_id=value["id"])
            if cursor.encode() != token:
                raise ValueError("cursor is not canonical")
            return cursor
        except ValueError:
            raise TychoraError(
                "INVALID_REQUEST", "Cursor is invalid.", reason="cursor.invalid"
            ) from None


class WorkspaceRepository(Protocol):
    """The single workspace row: identity, format, epochs and decision watermark."""

    def workspace_id(self) -> str: ...
    def format_version(self) -> int: ...
    def policy_epoch(self) -> int: ...
    def lexical_epoch(self) -> int: ...
    def decision_watermark(self) -> int: ...
    def bump_policy_epoch(self) -> int: ...
    def bump_lexical_epoch(self) -> int: ...


class ArtifactRepository(Protocol):
    """Registered content-addressed artifacts."""

    def get(self, sha256: str) -> ArtifactRef | None: ...
    def require(self, sha256: str) -> ArtifactRef: ...
    def register(self, ref: ArtifactRef) -> bool: ...


class ReadTransaction(Protocol):
    """Repositories bound to one consistent read."""

    workspace: WorkspaceRepository
    artifacts: ArtifactRepository


class WriteTransaction(ReadTransaction, Protocol):
    """Repositories bound to one write transaction."""


class UnitOfWork(Protocol):
    """Opens read and write transactions over one store."""

    def read(self) -> AbstractContextManager[ReadTransaction]: ...
    def write(self) -> AbstractContextManager[WriteTransaction]: ...
```

Note on `except ValueError`: `json.JSONDecodeError`, `binascii.Error`, `UnicodeDecodeError` and Pydantic's `ValidationError` are all `ValueError` subclasses, so one clause covers every malformed token.

- [ ] **Step 4: Run the cursor tests**

Run: `uv run --locked pytest tests/contracts/test_cursor.py -v`
Expected: PASS.

- [ ] **Step 5: Write the port-neutrality test (spec §6, §4.16)**

```python
# tests/architecture/test_ports_neutrality.py
"""No port names a store type or takes store query syntax."""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import tychora.ports as ports

STORE_MODULES = ("sqlite3", "chromadb", "ladybug")
QUERY_PARAMETERS = {"sql", "where", "match"}


def _protocols():
    for info in pkgutil.iter_modules(ports.__path__):
        module = importlib.import_module(f"tychora.ports.{info.name}")
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and getattr(value, "_is_protocol", False)
                and value.__module__ == module.__name__
            ):
                yield value


def test_ports_modules_do_not_import_store_packages():
    for path in Path(ports.__path__[0]).glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert name.split(".")[0] not in STORE_MODULES, (path.name, name)


def test_protocol_signatures_are_store_neutral():
    offenders = []
    for protocol in _protocols():
        for name, member in vars(protocol).items():
            if name.startswith("_") or not callable(member):
                continue
            for parameter, annotation in inspect.get_annotations(member).items():
                text = annotation if isinstance(annotation, str) else repr(annotation)
                if any(store in text for store in STORE_MODULES):
                    offenders.append((protocol.__name__, name, parameter, text))
                if parameter in QUERY_PARAMETERS and text in ("str", "<class 'str'>"):
                    offenders.append((protocol.__name__, name, parameter, "query text"))
    assert offenders == []


def test_repository_protocols_are_discovered():
    names = {protocol.__name__ for protocol in _protocols()}
    assert {"WorkspaceRepository", "ArtifactRepository", "UnitOfWork"} <= names
```

`inspect.get_annotations` returns the raw strings that `from __future__ import annotations` leaves, so names imported only under `TYPE_CHECKING` (for example `WorkspaceOwner` in `ports/query.py`) do not break the walk.

- [ ] **Step 6: Write the SQL-free module guard**

```python
# tests/architecture/test_sql_free_modules.py
"""Modules that must hold no raw SQL; later engine-core phases extend the list."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
SQL_FREE_MODULES: tuple[str, ...] = ("ports/repositories.py",)
SQL_CALLS = {"execute", "executemany", "executescript"}


def _violations(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_CALLS
        ):
            found.append(f"{path.name}:{node.lineno} .{node.func.attr}(")
        if isinstance(node, ast.Import) and any(
            alias.name == "sqlite3" for alias in node.names
        ):
            found.append(f"{path.name}:{node.lineno} import sqlite3")
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            found.append(f"{path.name}:{node.lineno} from sqlite3")
    return found


def test_listed_modules_hold_no_sql():
    violations = []
    for relative in SQL_FREE_MODULES:
        path = SOURCE / relative
        assert path.is_file(), relative
        violations.extend(_violations(path))
    assert violations == []
```

Later tasks of this phase append `application/workspace_rebuild.py` and the `evaluation/bench/` modules to `SQL_FREE_MODULES`.

- [ ] **Step 7: Run the architecture tests**

Run: `uv run --locked pytest tests/architecture tests/contracts/test_cursor.py -v`
Expected: PASS (today's ports already satisfy both guards).

- [ ] **Step 8: Commit**

```bash
git add src/tychora/ports/repositories.py tests/contracts/test_cursor.py tests/architecture
git commit -m "feat: add repository ports, keyset cursor and port-neutrality guards"
```

---

### Task 5: SQLite unit of work, workspace and artifact repositories

**Files:**
- Modify: `src/tychora/contracts/artifacts.py:10-16` (add `ArtifactRef.from_row`)
- Create: `src/tychora/adapters/sqlite/repositories/__init__.py`
- Create: `src/tychora/adapters/sqlite/repositories/workspace.py`
- Create: `src/tychora/adapters/sqlite/repositories/artifacts.py`
- Create: `src/tychora/adapters/sqlite/repositories/unit_of_work.py`
- Modify: `src/tychora/application/owner.py:175-235` (`_initialize`: attach `self.uow`; add `policy_epoch()` and `decision_watermark()` helpers to `WorkspaceOwner`)
- Modify: `src/tychora/adapters/sqlite/corpus.py:49-70` (`CorpusStore.__init__`: attach `self.uow`)
- Test: `tests/storage/test_repositories.py`

**Interfaces:**
- Consumes: protocols of Task 4.
- Produces: `ArtifactRef.from_row(row: Sequence[object]) -> ArtifactRef`; `artifact_ref(row: Sequence[object]) -> ArtifactRef` (re-export of `from_row` for adapters, as the index names it); `SqliteWorkspaceRepository(db)`, `SqliteArtifactRepository(db)`, `SqliteTransaction(db)` with attributes `workspace` and `artifacts`; `SqliteUnitOfWork(db)` with `read()` and `write()`; `WorkspaceOwner.uow`, `WorkspaceOwner.policy_epoch() -> int`, `WorkspaceOwner.decision_watermark() -> int`; `CorpusStore.uow`.
- Why `from_row` lives on the contract: `indexing/coordinator.py` and `evaluation/` may not import `tychora.adapters` (code-health contract 3), and the duplication ratchet wants one constructor. Adapters use the `artifact_ref` alias; upper layers call `ArtifactRef.from_row`.
- Transaction semantics of `SqliteUnitOfWork` over the owner's single connection (phase 4 moves reads to a pool): repositories handed out by `read()` refuse every mutation (`writable=False`); `read()` joins a transaction that is already open, otherwise opens a deferred `BEGIN` and always ends with `ROLLBACK`; `write()` refuses to nest (`INTEGRITY_ERROR` reason `invariant.unexpected`), opens `BEGIN IMMEDIATE`, commits on success and rolls back on any exception.

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_repositories.py
"""SQLite repositories over one canonical connection."""

import sqlite3

import pytest

from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.repositories import (
    SqliteUnitOfWork,
    artifact_ref,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError

SHA = "a" * 64


@pytest.fixture
def db(tmp_path):
    connection = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    connection.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('workspace',2,'2026-01-01T00:00:00+00:00')"
    )
    yield connection
    connection.close()


def test_artifact_ref_from_row():
    ref = artifact_ref((SHA, 3, "text/plain", SHA))
    assert ref == ArtifactRef(
        sha256=SHA, size_bytes=3, media_type="text/plain", storage_key=SHA
    )
    assert ArtifactRef.from_row((SHA, 3, "text/plain", SHA, "present")) == ref


def test_workspace_reads_and_epoch_bumps(db):
    uow = SqliteUnitOfWork(db)
    with uow.read() as tx:
        assert tx.workspace.workspace_id() == "workspace"
        assert tx.workspace.format_version() == 2
        assert (tx.workspace.policy_epoch(), tx.workspace.lexical_epoch()) == (0, 0)
        assert tx.workspace.decision_watermark() == 0
    assert not db.in_transaction
    with uow.write() as tx:
        assert tx.workspace.bump_policy_epoch() == 1
        assert tx.workspace.bump_lexical_epoch() == 1
    with uow.read() as tx:
        assert tx.workspace.policy_epoch() == 1


def test_epoch_bump_requires_a_write_transaction(db):
    with SqliteUnitOfWork(db).read() as tx, pytest.raises(TychoraError) as error:
        tx.workspace.bump_policy_epoch()
    assert error.value.reason == "invariant.unexpected"


def test_write_rolls_back_on_error_and_refuses_nesting(db):
    uow = SqliteUnitOfWork(db)
    with pytest.raises(RuntimeError), uow.write() as tx:
        tx.workspace.bump_policy_epoch()
        raise RuntimeError("boom")
    with uow.read() as tx:
        assert tx.workspace.policy_epoch() == 0
    db.execute("BEGIN")
    try:
        with pytest.raises(TychoraError) as error, uow.write():
            pass
        assert error.value.reason == "invariant.unexpected"
        with uow.read() as tx:
            assert tx.workspace.policy_epoch() == 0
        assert db.in_transaction
    finally:
        db.rollback()


def test_artifact_register_get_require(db):
    uow = SqliteUnitOfWork(db)
    ref = ArtifactRef(sha256=SHA, size_bytes=3, media_type="text/plain", storage_key=SHA)
    with uow.write() as tx:
        assert tx.artifacts.register(ref) is True
        assert tx.artifacts.register(ref) is False
    with uow.read() as tx:
        assert tx.artifacts.get(SHA) == ref
        assert tx.artifacts.get("b" * 64) is None
        with pytest.raises(TychoraError) as error:
            tx.artifacts.require("b" * 64)
        assert error.value.reason == "payload.missing"
    conflicting = ref.model_copy(update={"size_bytes": 4})
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.artifacts.register(conflicting)
    assert error.value.reason == "canonical.binding_mismatch"


def test_ambiguous_workspace_row_is_a_typed_error(db):
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('second',2,'2026-01-01T00:00:00+00:00')"
    )
    with SqliteUnitOfWork(db).read() as tx, pytest.raises(TychoraError) as error:
        tx.workspace.workspace_id()
    assert error.value.reason == "canonical.row_invalid"


def test_no_sqlite_type_escapes(db):
    with SqliteUnitOfWork(db).read() as tx:
        assert not isinstance(tx.workspace.policy_epoch(), sqlite3.Row)
```

This fixture inserts `schema_version` 2. Until Task 7 adds `CHECK(schema_version=2)` the value is unconstrained, so the test passes before and after that task.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_repositories.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.adapters.sqlite.repositories'`.

- [ ] **Step 3: Add `ArtifactRef.from_row`**

In `src/tychora/contracts/artifacts.py`, change the imports to

```python
from collections.abc import Sequence
from typing import Literal, Self
```

and add to `ArtifactRef`:

```python
    @classmethod
    def from_row(cls, row: Sequence[object]) -> Self:
        """Build a reference from a (sha256, size_bytes, media_type, storage_key) row."""
        if len(row) < 4:
            raise ValueError("Artifact row needs four columns")
        return cls.model_validate(
            {
                "sha256": row[0],
                "size_bytes": row[1],
                "media_type": row[2],
                "storage_key": row[3],
            }
        )
```

- [ ] **Step 4: Write the SQLite repositories**

```python
# src/tychora/adapters/sqlite/repositories/workspace.py
"""The single workspace row and the assertion decision watermark."""

import sqlite3

from tychora.contracts.errors import TychoraError

_ROW = (
    "SELECT workspace_id,schema_version,policy_epoch,lexical_statistics_epoch "
    "FROM workspaces"
)


def require_write(db: sqlite3.Connection, writable: bool) -> None:
    """Refuse a mutation in a read transaction or outside any transaction."""
    if not writable or not db.in_transaction:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "A workspace mutation requires a write transaction.",
            reason="invariant.unexpected",
        )


class SqliteWorkspaceRepository:
    """Reads and bumps the one row of ``workspaces``."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def _row(self) -> tuple[str, int, int, int]:
        rows = self._db.execute(_ROW).fetchall()
        if len(rows) != 1:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Workspace identity is missing or ambiguous.",
                reason="canonical.row_invalid",
            )
        return rows[0]

    def workspace_id(self) -> str:
        return self._row()[0]

    def format_version(self) -> int:
        return self._row()[1]

    def policy_epoch(self) -> int:
        return self._row()[2]

    def lexical_epoch(self) -> int:
        return self._row()[3]

    def decision_watermark(self) -> int:
        return self._db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]

    def bump_policy_epoch(self) -> int:
        require_write(self._db, self._writable)
        return self._db.execute(
            "UPDATE workspaces SET policy_epoch=policy_epoch+1 RETURNING policy_epoch"
        ).fetchone()[0]

    def bump_lexical_epoch(self) -> int:
        require_write(self._db, self._writable)
        return self._db.execute(
            "UPDATE workspaces SET lexical_statistics_epoch=lexical_statistics_epoch+1 "
            "RETURNING lexical_statistics_epoch"
        ).fetchone()[0]
```

```python
# src/tychora/adapters/sqlite/repositories/artifacts.py
"""Registered artifacts as ``ArtifactRef`` values."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError

from .workspace import require_write


def artifact_ref(row: Sequence[object]) -> ArtifactRef:
    """Build a reference from a (sha256, size_bytes, media_type, storage_key) row."""
    return ArtifactRef.from_row(row)


class SqliteArtifactRepository:
    """Lookups and idempotent registration in ``artifacts``."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def get(self, sha256: str) -> ArtifactRef | None:
        row = self._db.execute(
            "SELECT artifact_sha,size_bytes,media_type,storage_key FROM artifacts "
            "WHERE artifact_sha=? AND payload_state='present'",
            (sha256,),
        ).fetchone()
        return None if row is None else artifact_ref(row)

    def require(self, sha256: str) -> ArtifactRef:
        ref = self.get(sha256)
        if ref is None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "A required artifact is unavailable.",
                reason="payload.missing",
            )
        return ref

    def register(self, ref: ArtifactRef) -> bool:
        require_write(self._db, self._writable)
        row = self._db.execute(
            "SELECT size_bytes,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        if row is not None:
            if row != (ref.size_bytes, ref.storage_key, "present"):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "A registered artifact differs from its reference.",
                    reason="canonical.binding_mismatch",
                )
            return False
        self._db.execute(
            "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
                ref.storage_key,
                datetime.now(UTC).isoformat(),
            ),
        )
        return True
```

```python
# src/tychora/adapters/sqlite/repositories/unit_of_work.py
"""Read and write transactions over one SQLite connection."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from tychora.contracts.errors import TychoraError

from .artifacts import SqliteArtifactRepository
from .workspace import SqliteWorkspaceRepository


class SqliteTransaction:
    """Repositories bound to the connection's current transaction."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool) -> None:
        self.workspace = SqliteWorkspaceRepository(db, writable=writable)
        self.artifacts = SqliteArtifactRepository(db, writable=writable)


class SqliteUnitOfWork:
    """Opens transactions on the owner's connection; the owner thread calls it."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    @contextmanager
    def read(self) -> Iterator[SqliteTransaction]:
        """Join the open transaction, or read inside a deferred one and roll back."""
        if self._db.in_transaction:
            yield SqliteTransaction(self._db, writable=False)
            return
        self._db.execute("BEGIN")
        try:
            yield SqliteTransaction(self._db, writable=False)
        finally:
            self._db.rollback()

    @contextmanager
    def write(self) -> Iterator[SqliteTransaction]:
        """Run one immediate transaction; commit on success, roll back otherwise."""
        if self._db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "A write transaction cannot nest in another transaction.",
                reason="invariant.unexpected",
            )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield SqliteTransaction(self._db, writable=True)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
```

```python
# src/tychora/adapters/sqlite/repositories/__init__.py
"""SQLite implementations of the repository ports."""

from .artifacts import SqliteArtifactRepository, artifact_ref
from .unit_of_work import SqliteTransaction, SqliteUnitOfWork
from .workspace import SqliteWorkspaceRepository

__all__ = [
    "SqliteArtifactRepository",
    "SqliteTransaction",
    "SqliteUnitOfWork",
    "SqliteWorkspaceRepository",
    "artifact_ref",
]
```

- [ ] **Step 5: Attach the unit of work to the owner and the corpus store**

In `src/tychora/application/owner.py` add the module-level import next to the existing SQLite adapter imports:

```python
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
```

In `_initialize`, directly after the `try: self.db = open_corpus(...)` block, add:

```python
        self.uow = SqliteUnitOfWork(self.db)
```

and add these methods to `WorkspaceOwner` (after `maintenance_barrier`):

```python
    def policy_epoch(self) -> int:
        """Read the current policy epoch; call on the owner thread."""
        with self.uow.read() as tx:
            return tx.workspace.policy_epoch()

    def decision_watermark(self) -> int:
        """Read the latest assertion decision ID; call on the owner thread."""
        with self.uow.read() as tx:
            return tx.workspace.decision_watermark()
```

In `src/tychora/adapters/sqlite/corpus.py` add `from .repositories import SqliteUnitOfWork` to the imports and, at the end of `CorpusStore.__init__`, `self.uow = SqliteUnitOfWork(db)`.

- [ ] **Step 6: Run the tests**

Run: `uv run --locked pytest tests/storage/test_repositories.py tests/storage tests/runtime -v`
Expected: PASS.

- [ ] **Step 7: Add the repository package to the guards and commit**

The package holds SQL by design, so it stays out of `SQL_FREE_MODULES`; the port-neutrality test already covers its protocols.

```bash
git add src/tychora/contracts/artifacts.py src/tychora/adapters/sqlite/repositories src/tychora/application/owner.py src/tychora/adapters/sqlite/corpus.py tests/storage/test_repositories.py
git commit -m "feat: add SQLite unit of work with workspace and artifact repositories"
```

---

### Task 6: One epoch read, one watermark read, one artifact-row constructor

**Files (each site with the exact text it replaces; line numbers are from `b91d808` and may have shifted by a few lines):**

Decision watermark, `SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions`:
- Modify: `src/tychora/adapters/sqlite/graph.py:47-49`
- Modify: `src/tychora/adapters/sqlite/publication.py:592-594`
- Modify: `src/tychora/indexing/coordinator.py:840-842` and `:1247-1249`
- Modify: `src/tychora/application/sources.py:296-298`

Policy epoch, `SELECT policy_epoch FROM workspaces WHERE workspace_id=?` (and the two-epoch variant):
- Modify: `src/tychora/adapters/sqlite/evaluations.py:193-196`, `:263-266`, `:296-299`
- Modify: `src/tychora/adapters/sqlite/eligibility.py:225-230`, `:609-616`
- Modify: `src/tychora/evaluation/runner.py:288-295`
- Modify: `src/tychora/application/owner.py:914-917`
- Modify: `src/tychora/application/reranking.py:675-678`
- Modify: `src/tychora/application/default_bindings.py:356-359`, `:384-387`
- Modify: `src/tychora/application/sources.py:887-893`, `:1930-1936`
- Modify: `src/tychora/application/collections.py:37-40`
- Modify: `src/tychora/application/visual.py:324-327`
- Modify: `src/tychora/application/consent.py:130-133`
- Modify: `src/tychora/interfaces/http/routes.py:204-209`

Artifact rows built field by field from a `(sha, size, media_type, storage_key)` row:
- Modify: `src/tychora/adapters/sqlite/history.py:263-270`, `src/tychora/adapters/sqlite/eligibility.py:295-302`, `src/tychora/adapters/files/references.py:255-258`, `src/tychora/adapters/sqlite/graph.py:77-83`, `src/tychora/adapters/sqlite/jobs.py:200-202`, `src/tychora/adapters/sqlite/policy.py:152-158`, `src/tychora/adapters/sqlite/corpus.py:117-123`, `:150-156`, `src/tychora/adapters/sqlite/evaluations.py:66-68`, `src/tychora/adapters/sqlite/publication.py:478-484`, `:492-499`, `:701-706`
- Modify: `src/tychora/indexing/coordinator.py:228-230`, `:633-639`, `:818-824`
- Modify: `src/tychora/application/query.py:1312-1314`, `src/tychora/application/removal.py:282-288`, `:530-536`, `:556-558`, `src/tychora/application/sources.py:446-451`, `:1044-1049`, `:1222-1228`, `:1257-1259`, `:1520-1526`, `src/tychora/application/discovery.py:565-570`, `:802-804`, `:1096-1101`, `src/tychora/application/restore.py:182-187`, `:256-258`, `src/tychora/application/inspection.py:138-140`, `src/tychora/application/vision_selection.py:97-99`

Not touched (they build a reference from computed values, not from a row): `ingestion/composition.py:60`, `adapters/code/normalize.py:197,827`, `adapters/docling/pdf_batches.py:33`, `adapters/files/cas.py:194`, `application/pdf_processing.py:239`, `adapters/sqlite/eligibility.py:193` (builds from a JSON mapping).

- Test: `tests/architecture/test_single_row_readers.py`

**Interfaces:**
- Consumes: `SqliteWorkspaceRepository`, `artifact_ref`, `WorkspaceOwner.policy_epoch()`, `WorkspaceOwner.decision_watermark()`, `CorpusStore.uow`, `ArtifactRef.from_row` (Task 5).
- Produces: no new names; the duplicated statements exist once, in `adapters/sqlite/repositories/workspace.py`.

- [ ] **Step 1: Write the failing duplication guard**

```python
# tests/architecture/test_single_row_readers.py
"""The epoch, watermark and artifact-row reads each have one implementation."""

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
OWNER = "adapters/sqlite/repositories/workspace.py"
PATTERNS = {
    re.compile(r"MAX\(decision_id\),0\)"): [OWNER],
    re.compile(r"SELECT policy_epoch(,lexical_statistics_epoch)? FROM workspaces"): [],
}


def test_epoch_and_watermark_statements_live_in_the_repository():
    for pattern, expected in PATTERNS.items():
        sites = sorted(
            str(path.relative_to(SOURCE))
            for path in SOURCE.rglob("*.py")
            if pattern.search(path.read_text())
        )
        assert sites == expected, (pattern.pattern, sites)


def _row_constructions(path: Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "ArtifactRef"
            and {kw.arg for kw in node.keywords}
            == {"sha256", "size_bytes", "media_type", "storage_key"}
            and any(
                isinstance(kw.value, ast.Subscript)
                and isinstance(kw.value.slice, ast.Constant)
                and isinstance(kw.value.slice.value, int)
                for kw in node.keywords
            )
        ):
            lines.append(node.lineno)
    return lines


def test_no_field_by_field_artifact_row_construction():
    offenders = {
        str(path.relative_to(SOURCE)): lines
        for path in SOURCE.rglob("*.py")
        if (lines := _row_constructions(path))
    }
    assert offenders == {}
```

The second test matches `ArtifactRef(sha256=row[0], …)` shapes (a keyword value indexing a row by an integer) and ignores constructions from computed values or JSON mappings. The repository's own reads use one statement over all workspace columns, so the epoch pattern must match nowhere.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `uv run --locked pytest tests/architecture/test_single_row_readers.py -v`
Expected: FAIL listing the five watermark sites, the sixteen epoch sites and the row constructions above.

- [ ] **Step 3: Replace the watermark reads**

- `adapters/sqlite/graph.py`: replace `maximum = db.execute("SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions").fetchone()[0]` with `maximum = SqliteWorkspaceRepository(db).decision_watermark()` and import `from .repositories import SqliteWorkspaceRepository`.
- `adapters/sqlite/publication.py` (inside the publication transaction): `watermark = SqliteWorkspaceRepository(store.db).decision_watermark()` with the same import.
- `indexing/coordinator.py` (both sites): 

```python
        with self.store.uow.read() as tx:
            watermark = tx.workspace.decision_watermark()
```

- `application/sources.py:296`: `return self.owner.decision_watermark()`.

- [ ] **Step 4: Replace the policy-epoch reads**

- `adapters/sqlite/evaluations.py:193`:

```python
        workspace = SqliteWorkspaceRepository(self.db)
        current = (workspace.policy_epoch(), workspace.lexical_epoch())
```

- `adapters/sqlite/evaluations.py:263` and `:296`: `epoch = SqliteWorkspaceRepository(self.db).policy_epoch()` and `current_epoch = SqliteWorkspaceRepository(self.db).policy_epoch()`.
- `adapters/sqlite/eligibility.py:225` (already inside `BEGIN`): replace the `epoch_row` query and its `None` check with `policy_epoch_value = SqliteWorkspaceRepository(db).policy_epoch()` and rename the later uses of `epoch_row[0]` in the same function to `policy_epoch_value` (`grep -n "epoch_row" src/tychora/adapters/sqlite/eligibility.py` lists them).
- `adapters/sqlite/eligibility.py:609` (`policy_epoch` helper): body becomes `return SqliteWorkspaceRepository(store.db).policy_epoch()`.
- `evaluation/runner.py:288` (`_epochs`):

```python
    def _epochs(self) -> tuple[int, int]:
        with self.owner.uow.read() as tx:
            return tx.workspace.policy_epoch(), tx.workspace.lexical_epoch()
```

- `application/owner.py:914` (`_check_epoch`):

```python
    def _check_epoch(self, policy_epoch: int) -> None:
        if (
            type(policy_epoch) is not int
            or self._owner.policy_epoch() != policy_epoch
        ):
            raise TychoraError(
                "SCOPE_CHANGED",
                "Workspace policy changed; retry authorization",
                retryable=True,
            )
```

- `application/reranking.py:675` and `application/visual.py:324`: replace the query and the `row is None or` half of the condition:

```python
        epoch = owner.policy_epoch()
        if runtime.workspace_id != owner.workspace_id:
            raise TychoraError("INTEGRITY_ERROR", "Reranker runtime workspace differs.")
```

(`"Visual runtime workspace differs."` in `visual.py`), and pass `epoch` where `row[0]` was passed (`OwnerRerankAccess(owner, granted, epoch)`, `OwnerVisualAccess(owner, granted, epoch)`).
- `application/default_bindings.py:356` and `:384`: `epoch = owner.policy_epoch()`.
- `application/sources.py:887` and `:1930`: `epoch = await self.owner.run_blocking(self.owner.policy_epoch, ctx)`.
- `application/collections.py:37`: `return self.owner.policy_epoch()`.
- `application/consent.py:130`: `epoch = owner.policy_epoch()` (the surrounding explicit transaction is joined by `read()`).
- `interfaces/http/routes.py:204`: `"policy_epoch": await owner.run_blocking(owner.policy_epoch, ExecutionContext()),`.

- [ ] **Step 5: Replace the artifact-row constructions**

Each listed site becomes one call. Two shapes occur:

```python
# row already holds (sha, size, media_type, storage_key, ...):
ref = ArtifactRef.from_row(row)            # upper layers
ref = artifact_ref(row)                    # adapters (from .repositories import artifact_ref)

# sha known separately and row holds (size, media_type, storage_key, ...):
ref = ArtifactRef.from_row((sha, *row[:3]))
```

Use the second shape at `adapters/sqlite/evaluations.py:66`, `jobs.py:200`, `policy.py:152`, `corpus.py:117` (`snapshot.config_sha`), `corpus.py:150` (`config_sha`), `indexing/coordinator.py:633` (`asset.artifact_sha`), `application/removal.py:282,530,556` (`sha`), `restore.py:182` (`row[0]` with `ref_row`), `restore.py:256` (`sha`), `inspection.py:138` (`sha`), `discovery.py:1096` (`row[1]` with `ref_row`), and `history.py:263` (`row[0]` with `artifact`). Use the first shape everywhere else. At `application/query.py:1312` the row columns are not contiguous: `ArtifactRef.from_row((row[2], row[9], row[10], row[11]))`. At `adapters/files/references.py:255` keep the surrounding `try/except ValueError`, building `ref = artifact_ref((sha, size, media, key))`.

- [ ] **Step 6: Run the guard and the affected suites**

Run: `uv run --locked pytest tests/architecture tests/storage tests/indexing tests/retrieval tests/evaluation tests/operations tests/application tests/security tests/transports -q`
Expected: PASS.

- [ ] **Step 7: Run the ratchet report to confirm duplication and raw SQL went down**

Run: `uv run --locked python -m tools.health report`
Expected: G8 (raw SQL outside adapters) lower by at least 14 (the application, evaluation, indexing and interfaces sites above), G9 clone count not higher.

- [ ] **Step 8: Commit**

```bash
git add -A src/tychora tests/architecture/test_single_row_readers.py
git commit -m "refactor: read epochs, watermark and artifact rows through one implementation"
```

---

### Task 7: Workspace format 2

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql:11` (`schema_version INTEGER NOT NULL` → `schema_version INTEGER NOT NULL CHECK(schema_version=2)`)
- Modify: `src/tychora/adapters/sqlite/migrations.py:10-11` (`SCHEMA_VERSION`, `SCHEMA_CHECKSUM`), `:21-25` (`supported_migration_target`), `:28-80` (`open_verified_import`)
- Modify: `src/tychora/adapters/sqlite/connection.py:16-55` (`open_corpus`)
- Modify: `src/tychora/application/owner.py:206` (`VALUES (?,1,?)` → `VALUES (?,2,?)`)
- Modify tests that insert format-1 workspace rows: `tests/ingestion/test_acquisition.py:169`, `tests/jobs/conftest.py:49` (`('workspace',1,NULL,0,0,'2026-01-01')` → `('workspace',2,NULL,0,0,'2026-01-01')`), `tests/runtime/test_upload_policy.py:149` (`'foreign',1,'2024'` → `'foreign',2,'2024'`), `tests/operations/test_history.py:22` (`("workspace", 1, …)` → `("workspace", 2, …)`), `tests/retrieval/test_eligibility.py:432` and `tests/indexing/test_publication.py:347` (`1` → `2`)
- Modify migration-target tests: `tests/operations/test_migrations.py:11-16,58,88,99`, `tests/transports/test_http.py:62`, `tests/operations/test_restore.py:789`, `tests/cli/test_commands.py:82-92`, `tests/storage/test_schema.py:42`
- Create: `tests/helpers/legacy.py`
- Test: `tests/storage/test_format.py`

**Interfaces:**
- Consumes: reason `workspace.format_old` (Task 1).
- Produces: `SCHEMA_VERSION = 2`; `SCHEMA_CHECKSUM = "5899bf5b00ec52d686602369801491bdc6e50d6e9dd803e790bb1cd32d15cabe"`; `FORMAT_ONE_CHECKSUM = "b98d888098fa0ea790484f37ab8cf746afc2299576411767012c9e3a3c5642a9"` (the last format-1 receipt, kept only so tests can fabricate an old workspace); `format_old_error() -> TychoraError`; test helper `make_format_one(root: Path) -> None`.

The DDL change is deliberate: it makes format 2 a different schema object list, so `open_verified_import`'s exact schema comparison also separates the formats, and a hand-edited version receipt cannot pass as format 2.

- [ ] **Step 1: Write the legacy helper and the failing format tests**

```python
# tests/helpers/legacy.py
"""Fabricate format-1 databases for refusal and rebuild tests."""

import sqlite3
from pathlib import Path

from tychora.adapters.sqlite.migrations import FORMAT_ONE_CHECKSUM, migration_sql


def make_format_one(root: Path) -> None:
    """Rewrite the migration receipt of a closed workspace to format 1."""
    with sqlite3.connect(root / "corpus.sqlite3") as db:
        db.execute(
            "UPDATE schema_migrations SET version=1, checksum=?", (FORMAT_ONE_CHECKSUM,)
        )


class OutputCounter:
    """Character-count output counter so tests need no tokenizer bundle."""

    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def add_text(kb, key: str, text: str):
    """Add one text document to the default collection and wait for success."""
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "text",
            "text": text,
            "metadata": {"title": key},
        }
    )
    status = kb.jobs.wait(job.id)
    assert status.state == "succeeded", status.error_code
    return status


def make_format_one_database(path: Path) -> None:
    """Create an empty database with the format-1 DDL and receipt."""
    legacy = migration_sql().replace(" CHECK(schema_version=2)", "")
    db = sqlite3.connect(path)
    try:
        db.executescript(legacy)
        db.execute(
            "INSERT INTO schema_migrations VALUES (1, ?, '2026-01-01T00:00:00+00:00')",
            (FORMAT_ONE_CHECKSUM,),
        )
        db.commit()
    finally:
        db.close()
```

```python
# tests/storage/test_format.py
"""Format-2 workspaces open; format-1 workspaces and archives are refused untouched."""

from hashlib import sha256

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import make_format_one, make_format_one_database
from tychora import Tychora
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.migrations import (
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    migration_sql,
    open_verified_import,
)
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _format_version(kb) -> int:
    owner = kb._workspace.owner

    def read() -> int:
        with owner.uow.read() as tx:
            return tx.workspace.format_version()

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


def test_packaged_migration_is_format_two():
    assert SCHEMA_VERSION == 2
    assert "schema_version INTEGER NOT NULL CHECK(schema_version=2)" in migration_sql()
    assert sha256(migration_sql().encode()).hexdigest() == SCHEMA_CHECKSUM


def test_new_workspace_records_format_two(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=Counter()) as kb:
        assert _format_version(kb) == 2


def test_format_one_workspace_is_refused_untouched(tmp_path):
    root = tmp_path / "ws"
    with Tychora.open(root, token_counter=Counter()):
        pass
    make_format_one(root)
    before = (root / "corpus.sqlite3").read_bytes()
    with pytest.raises(TychoraError) as error:
        Tychora.open(root, token_counter=Counter())
    assert (error.value.code, error.value.reason) == (
        "PROFILE_INCOMPATIBLE",
        "workspace.format_old",
    )
    assert "manage rebuild workspace --from" in error.value.message
    assert (root / "corpus.sqlite3").read_bytes() == before


@pytest.mark.parametrize("genuine_ddl", [True, False])
def test_format_one_archive_import_is_refused(tmp_path, genuine_ddl):
    database = tmp_path / "corpus.sqlite3"
    if genuine_ddl:
        make_format_one_database(database)
    else:
        open_corpus(database, initialize=True).close()
        make_format_one(tmp_path)
    with pytest.raises(TychoraError) as error:
        open_verified_import(database)
    assert error.value.reason == "workspace.format_old"


def test_unknown_receipt_is_still_incompatible(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    db.execute("UPDATE schema_migrations SET version=7")
    db.close()
    with pytest.raises(TychoraError) as error:
        open_corpus(path)
    assert error.value.code == "PROFILE_INCOMPATIBLE"
    assert error.value.reason is None
```

Restore reads the archive's `corpus.sqlite3` only through `open_verified_import` (`application/restore.py:484,592`), so refusing there refuses format-1 archive imports; testing the function directly avoids having to re-sign a doctored archive manifest.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_format.py -v`
Expected: FAIL with `ImportError: cannot import name 'FORMAT_ONE_CHECKSUM'`.

- [ ] **Step 3: Change the DDL and the receipt constants**

In `0001.sql` line 11 write `  schema_version INTEGER NOT NULL CHECK(schema_version=2),`. Then confirm the checksum:

Run: `uv run --locked python -c "from hashlib import sha256; print(sha256(open('src/tychora/resources/migrations/0001.sql','rb').read()).hexdigest())"`
Expected: `5899bf5b00ec52d686602369801491bdc6e50d6e9dd803e790bb1cd32d15cabe`

In `migrations.py`:

```python
SCHEMA_VERSION = 2
SCHEMA_CHECKSUM = "5899bf5b00ec52d686602369801491bdc6e50d6e9dd803e790bb1cd32d15cabe"
FORMAT_ONE_CHECKSUM = "b98d888098fa0ea790484f37ab8cf746afc2299576411767012c9e3a3c5642a9"


def format_old_error() -> TychoraError:
    """The refusal for a workspace or archive written by an older format."""
    return TychoraError(
        "PROFILE_INCOMPATIBLE",
        "This workspace uses an older storage format. Recreate it with "
        "`tychora --workspace NEW manage rebuild workspace --from OLD`.",
        reason="workspace.format_old",
    )


def receipt_is_older(rows: list[tuple[int, str]]) -> bool:
    """True when a migration receipt names format 1, or format 2 with older DDL."""
    return len(rows) == 1 and (
        rows[0][0] < SCHEMA_VERSION
        or (rows[0][0] == SCHEMA_VERSION and rows[0][1] != SCHEMA_CHECKSUM)
    )


def supported_migration_target(target_version: int) -> int:
    """Format 2 is the sole packaged version; never infer an upgrade from archive SQL."""
    if type(target_version) is not int or target_version != SCHEMA_VERSION:
        raise TychoraError("PROFILE_INCOMPATIBLE", "Unsupported migration target")
    return target_version
```

- [ ] **Step 4: Refuse older formats in `open_corpus`**

Replace the receipt check in `connection.py`:

```python
        rows = db.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if receipt_is_older(rows):
            raise format_old_error()
        if rows != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
            raise TychoraError("PROFILE_INCOMPATIBLE", "Corpus schema is incompatible")
```

and add `format_old_error, receipt_is_older` to the import from `.migrations`. The check happens before `PRAGMA journal_mode=WAL`, so a refused file is not converted to WAL and stays byte-identical.

- [ ] **Step 5: Refuse older formats in `open_verified_import`**

The schema comparison runs first and an older format fails it. Before raising "Imported schema differs from packaged schema", read only the receipt, which is safe because the connection is `mode=ro&immutable=1`, `trusted_schema=OFF` and `query_only=ON`:

```python
            if (
                db.execute(schema_query).fetchall()
                != expected.execute(schema_query).fetchall()
            ):
                if _imported_receipt_is_older(db):
                    raise format_old_error()
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Imported schema differs from packaged schema",
                )
```

and replace the later receipt comparison (the "Imported migration receipt differs" check) with:

```python
        rows = db.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if receipt_is_older(rows):
            raise format_old_error()
        if rows != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE", "Imported migration receipt differs"
            )
```

with the helper:

```python
def _imported_receipt_is_older(db: sqlite3.Connection) -> bool:
    """Read only the migration receipt of an untrusted, read-only database."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return False
    try:
        rows = db.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    except sqlite3.DatabaseError:
        return False
    return receipt_is_older(rows)
```

- [ ] **Step 6: Write format 2 on creation and update the format-1 test fixtures**

`application/owner.py:206`: `"INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES (?,2,?)",`. Then apply the fixture edits listed under **Files**. In `tests/storage/test_schema.py:42` change the `version` tamper to `UPDATE schema_migrations SET version=1` and add after the `assert error.value.code == "PROFILE_INCOMPATIBLE"` line:

```python
    if tamper in ("checksum", "version"):
        assert error.value.reason == "workspace.format_old"
```

In `tests/operations/test_migrations.py` replace the first test with:

```python
def test_v2_is_the_only_packaged_migration_target():
    assert supported_migration_target(2) == 2
    for unsupported in (1, 3):
        with pytest.raises(TychoraError) as error:
            supported_migration_target(unsupported)
        assert error.value.code == "PROFILE_INCOMPATIBLE"
```

and change `"target_version": "2"` to `"3"` (lines 58, 88) and `"1"` to `"2"` (line 99). Same edits: `tests/transports/test_http.py:62` `"2"` → `"3"`; `tests/operations/test_restore.py:789` `"1"` → `"2"`; `tests/cli/test_commands.py` `migrate "1"` → `"2"` and `"2"` → `"3"`, renaming the test to `test_manage_migrate_uses_packaged_v2_target_and_rejects_unknown_version`.

- [ ] **Step 7: Run the format, schema, restore and migration suites**

Run: `uv run --locked pytest tests/storage tests/operations tests/security/test_restore.py tests/cli/test_commands.py tests/transports/test_http.py tests/jobs tests/ingestion/test_acquisition.py tests/runtime -v`
Expected: PASS.

- [ ] **Step 8: Run the whole offline suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS. A remaining failure that inserts `schema_version` 1 is a fixture missed above: `grep -rn "schema_version,created_at) VALUES\|INTO workspaces VALUES" tests` lists every candidate.

- [ ] **Step 9: Document and commit**

In `docs/workspaces.md`, add a section "Storage format":

```markdown
## Storage format

Workspaces use storage format 2. A workspace or export archive written by an
older Tychora is refused with `PROFILE_INCOMPATIBLE` and reason
`workspace.format_old`. Recreate it from its retained originals:

    tychora --workspace ./knowledge-v2 manage rebuild workspace --from ./knowledge

The old workspace is opened read-only and is never modified.
```

```bash
git add src/tychora/resources/migrations/0001.sql src/tychora/adapters/sqlite/migrations.py src/tychora/adapters/sqlite/connection.py src/tychora/application/owner.py tests docs/workspaces.md
git commit -m "feat: introduce workspace format 2 and refuse older formats with a rebuild hint"
```

---
### Task 8: Read-only inventory of an older workspace

**Files:**
- Create: `src/tychora/adapters/sqlite/workspace_inventory.py`
- Modify: `src/tychora/adapters/files/cas.py:243-266` (reasons `payload.hash_mismatch` and `payload.missing`)
- Test: `tests/storage/test_workspace_inventory.py`

**Interfaces:**
- Consumes: `ArtifactRef.from_row` (Task 5), `SqliteWorkspaceRepository` (Task 5), `LocalArtifacts` (`adapters/files/cas.py:50`), `make_format_one` (Task 7).
- Produces:

```python
class InventoryCollection(Contract): collection_id: Identifier; slug: NonEmptyStr; title: NonEmptyStr; access_floor: NonNegativeInt; policy_json: NonEmptyStr
class InventoryRevision(Contract): revision_id: Identifier; kind_id: Identifier; metadata_json: NonEmptyStr; captured_at: NonEmptyStr; original: ArtifactRef | None
class InventorySource(Contract): source_id: Identifier; collection_id: Identifier; source_key: NonEmptyStr; source_type: Literal["document", "repository"]; registration_json: NonEmptyStr; withdrawn: bool; revisions: tuple[InventoryRevision, ...]
class InventoryReview(Contract): revision_id: Identifier; state: NonEmptyStr; actor_id: NonEmptyStr
class WorkspaceInventory(Contract): workspace_id: Identifier; collections: tuple[InventoryCollection, ...]; sources: tuple[InventorySource, ...]; reviews: tuple[InventoryReview, ...]; finer_reviews: NonNegativeInt
def read_inventory(root: Path) -> WorkspaceInventory
def read_original(root: Path, ref: ArtifactRef, max_bytes: int = 1024**3) -> bytes | None   # None when the payload file is missing
def seed_collections(db: sqlite3.Connection, workspace_id: str, collections: tuple[InventoryCollection, ...]) -> int
```

The inventory reads only tables whose columns are unchanged since format 1 (`workspaces`, `collections`, `sources`, `revisions`, `artifacts`, `original_payload_tombstones`, `review_events`). It opens the database `mode=ro&immutable=1` after checking that no owner holds the workspace lock, so the old file is never written.

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_workspace_inventory.py
"""An older workspace is enumerated read-only; collections seed a new one."""

import sqlite3
from hashlib import sha256

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text as _add
from tests.helpers.legacy import make_format_one
from tychora import Tychora
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.workspace_inventory import (
    InventoryCollection,
    read_inventory,
    read_original,
    seed_collections,
)
from tychora.contracts.errors import TychoraError


@pytest.fixture
def old_workspace(tmp_path):
    root = tmp_path / "old"
    with Tychora.open(root, token_counter=Counter()) as kb:
        _add(kb, "alpha", "Alpha text.")
        _add(kb, "beta", "Beta first edition.")
        _add(kb, "beta", "Beta second edition.")
    make_format_one(root)
    return root


def test_inventory_lists_sources_revisions_and_originals(old_workspace):
    before = sha256((old_workspace / "corpus.sqlite3").read_bytes()).hexdigest()
    inventory = read_inventory(old_workspace)
    keys = [(source.source_key, len(source.revisions)) for source in inventory.sources]
    assert keys == [("alpha", 1), ("beta", 2)]
    assert inventory.collections[0].slug == "default"
    first = inventory.sources[1].revisions[0]
    assert first.original is not None
    assert read_original(old_workspace, first.original) == b"Beta first edition."
    assert sha256((old_workspace / "corpus.sqlite3").read_bytes()).hexdigest() == before


def test_inventory_marks_purged_originals(old_workspace):
    inventory = read_inventory(old_workspace)
    purged = inventory.sources[1].revisions[0].original
    assert purged is not None
    with sqlite3.connect(old_workspace / "corpus.sqlite3") as db:
        db.execute(
            "UPDATE artifacts SET payload_state='deleted', "
            "payload_deleted_at='2026-01-01T00:00:00+00:00' WHERE artifact_sha=?",
            (purged.sha256,),
        )
    assert read_inventory(old_workspace).sources[1].revisions[0].original is None


def test_inventory_refuses_a_live_owner(tmp_path):
    root = tmp_path / "live"
    with Tychora.open(root, token_counter=Counter()):
        with pytest.raises(TychoraError) as error:
            read_inventory(root)
    assert error.value.reason == "owner.live"


def test_seed_collections_inserts_and_refreshes(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('ws',2,'2026-01-01T00:00:00+00:00')"
    )
    rows = (
        InventoryCollection(
            collection_id="00000000-0000-4000-8000-000000000001",
            slug="default",
            title="Default",
            access_floor=0,
            policy_json='{"schema_version":1,"upload_providers":["voyage"]}',
        ),
        InventoryCollection(
            collection_id="55825936-6a6a-4d29-945a-327d771a76b7",
            slug="manuals",
            title="Manuals",
            access_floor=1,
            policy_json='{"schema_version":2,"upload_providers":[]}',
        ),
    )
    assert seed_collections(db, "ws", rows) == 2
    assert seed_collections(db, "ws", rows) == 0
    assert db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0] == 2
    db.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_workspace_inventory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.adapters.sqlite.workspace_inventory'`.

- [ ] **Step 3: Write the adapter**

```python
# src/tychora/adapters/sqlite/workspace_inventory.py
"""Read-only inventory of an older workspace, and collection seeding in a new one.

Only tables whose columns are unchanged since storage format 1 are read, so the
rebuild can enumerate any older workspace's retained originals without a
format-specific reader. The database is opened immutable and read-only.
"""

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from tychora.contracts.errors import TychoraError

from .repositories import SqliteWorkspaceRepository


class InventoryCollection(Contract):
    collection_id: Identifier
    slug: NonEmptyStr
    title: NonEmptyStr
    access_floor: NonNegativeInt
    policy_json: NonEmptyStr


class InventoryRevision(Contract):
    revision_id: Identifier
    kind_id: Identifier
    metadata_json: NonEmptyStr
    captured_at: NonEmptyStr
    original: ArtifactRef | None


class InventorySource(Contract):
    source_id: Identifier
    collection_id: Identifier
    source_key: NonEmptyStr
    source_type: Literal["document", "repository"]
    registration_json: NonEmptyStr
    withdrawn: bool
    revisions: tuple[InventoryRevision, ...]


class InventoryReview(Contract):
    revision_id: Identifier
    state: NonEmptyStr
    actor_id: NonEmptyStr


class WorkspaceInventory(Contract):
    workspace_id: Identifier
    collections: tuple[InventoryCollection, ...]
    sources: tuple[InventorySource, ...]
    reviews: tuple[InventoryReview, ...]
    finer_reviews: NonNegativeInt


_REVISIONS = (
    "SELECT r.source_id,r.revision_id,r.kind_id,r.metadata_json,r.captured_at,"
    "a.artifact_sha,a.size_bytes,a.media_type,a.storage_key,a.payload_state,"
    "EXISTS(SELECT 1 FROM original_payload_tombstones t WHERE t.revision_id=r.revision_id) "
    "FROM revisions r JOIN artifacts a ON a.artifact_sha=r.original_sha "
    "ORDER BY r.captured_at,r.revision_id"
)
_LATEST_REVIEWS = (
    "SELECT e.revision_id,e.state,e.actor_id FROM review_events e JOIN "
    "(SELECT revision_id,MAX(review_event_id) AS latest FROM review_events "
    "WHERE revision_id IS NOT NULL GROUP BY revision_id) l "
    "ON l.latest=e.review_event_id ORDER BY e.review_event_id"
)


def _require_no_live_owner(root: Path) -> None:
    lock_path = root / ".runtime" / "owner.lock"
    if not lock_path.exists():
        return
    fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TychoraError(
                "WORKSPACE_BUSY",
                "The old workspace is open in another process; close it first.",
                reason="owner.live",
            ) from None
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _read_only(root: Path) -> Iterator[sqlite3.Connection]:
    database = root / "corpus.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise TychoraError("NOT_FOUND", "The old workspace has no corpus database.")
    db = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        yield db
    except sqlite3.DatabaseError:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "The old workspace database cannot be read.",
            reason="canonical.row_invalid",
        ) from None
    finally:
        db.close()


def _revisions(db: sqlite3.Connection) -> dict[str, list[InventoryRevision]]:
    grouped: dict[str, list[InventoryRevision]] = {}
    for row in db.execute(_REVISIONS):
        retained = row[9] == "present" and not row[10]
        grouped.setdefault(row[0], []).append(
            InventoryRevision(
                revision_id=row[1],
                kind_id=row[2],
                metadata_json=row[3],
                captured_at=row[4],
                original=ArtifactRef.from_row(row[5:9]) if retained else None,
            )
        )
    return grouped


def _sources(db: sqlite3.Connection) -> tuple[InventorySource, ...]:
    revisions = _revisions(db)
    return tuple(
        InventorySource(
            source_id=row[0],
            collection_id=row[1],
            source_key=row[2],
            source_type=row[3],
            registration_json=row[4],
            withdrawn=row[5] is not None,
            revisions=tuple(revisions.get(row[0], ())),
        )
        for row in db.execute(
            "SELECT source_id,collection_id,source_key,source_type,registration_json,"
            "withdrawn_at FROM sources ORDER BY created_at,source_id"
        )
    )


def read_inventory(root: Path) -> WorkspaceInventory:
    """Enumerate an older workspace's collections, sources, originals and reviews."""
    _require_no_live_owner(root)
    with _read_only(root) as db:
        identities = db.execute("SELECT workspace_id FROM workspaces").fetchall()
        if len(identities) != 1:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "The old workspace identity is missing or ambiguous.",
                reason="canonical.row_invalid",
            )
        collections = tuple(
            InventoryCollection(
                collection_id=row[0],
                slug=row[1],
                title=row[2],
                access_floor=row[3],
                policy_json=row[4],
            )
            for row in db.execute(
                "SELECT collection_id,slug,title,access_floor,policy_json "
                "FROM collections ORDER BY slug"
            )
        )
        reviews = tuple(
            InventoryReview(revision_id=row[0], state=row[1], actor_id=row[2])
            for row in db.execute(_LATEST_REVIEWS)
        )
        finer = db.execute(
            "SELECT COUNT(*) FROM review_events WHERE revision_id IS NULL"
        ).fetchone()[0]
        return WorkspaceInventory(
            workspace_id=identities[0][0],
            collections=collections,
            sources=_sources(db),
            reviews=reviews,
            finer_reviews=finer,
        )


def read_original(root: Path, ref: ArtifactRef, max_bytes: int = 1024**3) -> bytes | None:
    """Return a retained original's verified bytes, or None when the file is gone."""
    if ref.size_bytes > max_bytes:
        raise TychoraError("LIMIT_EXCEEDED", "A retained original exceeds the rebuild limit.")
    try:
        with LocalArtifacts(root / "artifacts").open_verified(ref) as stream:
            return stream.read()
    except TychoraError as error:
        if error.reason == "payload.missing":
            return None
        raise


def seed_collections(
    db: sqlite3.Connection, workspace_id: str, collections: tuple[InventoryCollection, ...]
) -> int:
    """Insert missing collections and refresh existing ones; return inserted count."""
    db.execute("BEGIN IMMEDIATE")
    try:
        inserted = 0
        for item in collections:
            values = (item.title, item.access_floor, item.policy_json, item.collection_id)
            if db.execute(
                "UPDATE collections SET title=?,access_floor=?,policy_json=? "
                "WHERE collection_id=? AND slug=?",
                (*values, item.slug),
            ).rowcount:
                continue
            db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,"
                "access_floor,policy_json) VALUES (?,?,?,?,?,?)",
                (item.collection_id, workspace_id, item.slug, *values[:3]),
            )
            inserted += 1
        SqliteWorkspaceRepository(db).bump_policy_epoch()
        db.commit()
        return inserted
    except BaseException:
        db.rollback()
        raise
```

`read_original` tells a missing file from a damaged one by reason, so classify the CAS site in the same commit: in `src/tychora/adapters/files/cas.py:263-266` (`open_verified`) change the raise to `raise TychoraError("INTEGRITY_ERROR", "Required artifact payload is missing", reason="payload.missing") from None`. The hash and size failures in `_verified` (`cas.py:243-255`) get `reason="payload.hash_mismatch"` in the same edit.

A collection whose ID exists under another slug violates `UNIQUE(workspace_id, slug)` or the primary key on insert; the `sqlite3.IntegrityError` propagates and the caller reports it. The rebuild only seeds into a fresh workspace, so that can only happen with a corrupted old inventory.

- [ ] **Step 4: Run the tests**

Run: `uv run --locked pytest tests/storage/test_workspace_inventory.py tests/storage -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tychora/adapters/sqlite/workspace_inventory.py src/tychora/adapters/files/cas.py tests/storage/test_workspace_inventory.py
git commit -m "feat: enumerate an older workspace read-only for rebuilds"
```

---

### Task 9: Rebuild a workspace from an older one

**Files:**
- Create: `src/tychora/application/workspace_rebuild.py`
- Modify: `src/tychora/api.py` (add `open_rebuild_target` and `rebuild_workspace_from` after `LocalWorkspace`)
- Modify: `tests/architecture/test_sql_free_modules.py` (append `"application/workspace_rebuild.py"`)
- Test: `tests/application/test_workspace_rebuild.py`

**Interfaces:**
- Consumes: `read_inventory`, `read_original`, `seed_collections`, `InventoryCollection`, `InventorySource`, `InventoryRevision` (Task 8); `SourceInput` (`contracts/requests.py:300`); `ReviewTarget` (`contracts/corpus.py:198`); `LocalWorkspace` (`api.py:86`).
- Produces:

```python
# application/workspace_rebuild.py
RebuildSkipReason = Literal["withdrawn", "repository_source", "unsupported_kind",
                            "original_removed", "original_missing", "job_failed", "finer_review"]
class RebuildSkip(Contract): subject: Literal["source", "revision", "review"]; key: NonEmptyStr; reason: RebuildSkipReason; detail: str = ""
class RebuiltSource(Contract): source_key: NonEmptyStr; collection_id: Identifier; revisions: NonNegativeInt; job_ids: tuple[Identifier, ...]
class WorkspaceRebuildReport(Contract): old_workspace_id; new_workspace_id; collections: NonNegativeInt; sources: tuple[RebuiltSource, ...]; skipped: tuple[RebuildSkip, ...]; reviews_replayed: NonNegativeInt
class RebuildTarget(Protocol):
    workspace_id: str
    def kinds(self) -> frozenset[str]: ...
    def seed_collections(self, collections: tuple[InventoryCollection, ...]) -> int: ...
    def add(self, source: SourceInput) -> str: ...
    def wait(self, job_id: str) -> str: ...
    def record_revision_review(self, revision_id: str, state: str, actor: str) -> None: ...
    def close(self) -> None: ...
def rebuild_scope(collections: tuple[InventoryCollection, ...]) -> RequestScope
def rebuild_workspace(old: Path, destination: Path, ctx: ExecutionContext, *,
                      open_target: Callable[[Path, RequestScope], RebuildTarget],
                      kind_map: Mapping[str, str] | None = None) -> WorkspaceRebuildReport
# api.py
def open_rebuild_target(path: Path, scope: RequestScope, *, providers: ProviderBindings | None = None,
                        token_counter: TokenCounter | None = None) -> RebuildTarget
def rebuild_workspace_from(old: Path, destination: Path, *, kind_map: Mapping[str, str] | None = None,
                           providers: ProviderBindings | None = None,
                           token_counter: TokenCounter | None = None) -> WorkspaceRebuildReport
```

The index lists `rebuild_workspace(old, destination, ctx)`; the keyword-only `open_target` and `kind_map` extend it without changing the positional signature. `open_target` exists because the application layer may not import `tychora.api` (code-health layering); `kind_map` is the one-time kind map of the programme's resolved decision 11, filled by sub-project 5.

Each retained original is re-added as the same source key in the same collection, so source IDs and, when metadata is unchanged, revision IDs are identical in the new workspace; revision-level reviews are replayed against them. Inline (`text`, `bytes`) registrations keep their type; file, URL and upload registrations become `bytes` sources carrying the retained original, so the rebuild never re-reads the file system or the network.

- [ ] **Step 1: Write the failing tests**

```python
# tests/application/test_workspace_rebuild.py
"""An older workspace is recreated from its originals; the old one is untouched."""

import sqlite3
from hashlib import sha256

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text as _add
from tests.helpers.legacy import make_format_one
from tychora import Tychora
from tychora.api import rebuild_workspace_from
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _revision_and_source(kb, key: str) -> tuple[str, str]:
    owner = kb._workspace.owner

    def read():
        return owner.db.execute(
            "SELECT r.revision_id,s.source_id FROM revisions r JOIN sources s USING(source_id) "
            "WHERE s.source_key=? ORDER BY r.captured_at LIMIT 1",
            (key,),
        ).fetchone()

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


@pytest.fixture
def old(tmp_path):
    root = tmp_path / "old"
    with Tychora.open(root, token_counter=Counter()) as kb:
        _add(kb, "alpha", "Alpha verification outcome.")
        _add(kb, "beta", "Beta first edition.")
        _add(kb, "beta", "Beta second edition.")
        _add(kb, "gamma", "Gamma withdrawn later.")
        alpha_revision, _ = _revision_and_source(kb, "alpha")
        beta_first, _ = _revision_and_source(kb, "beta")
        _, gamma_source = _revision_and_source(kb, "gamma")
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.store.record_review(
                    ReviewTarget(revision_id=alpha_revision), "accepted", "reviewer"
                ),
                ExecutionContext(),
            )
        )
    make_format_one(root)
    with sqlite3.connect(root / "corpus.sqlite3") as db:
        db.execute(
            "UPDATE sources SET withdrawn_at='2026-01-01T00:00:00+00:00' WHERE source_id=?",
            (gamma_source,),
        )
        db.execute(
            "UPDATE artifacts SET payload_state='deleted', "
            "payload_deleted_at='2026-01-01T00:00:00+00:00' "
            "WHERE artifact_sha=(SELECT original_sha FROM revisions WHERE revision_id=?)",
            (beta_first,),
        )
    return root, alpha_revision


def test_rebuild_reports_skipped_items_and_leaves_old_unchanged(old, tmp_path):
    root, alpha_revision = old
    before = sha256((root / "corpus.sqlite3").read_bytes()).hexdigest()
    report = rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    assert sha256((root / "corpus.sqlite3").read_bytes()).hexdigest() == before
    assert {(item.source_key, item.revisions) for item in report.sources} == {
        ("alpha", 1),
        ("beta", 1),
    }
    assert {(item.subject, item.reason) for item in report.skipped} == {
        ("source", "withdrawn"),
        ("revision", "original_removed"),
    }
    assert report.reviews_replayed == 1
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        result = kb.search({"question": "verification", "modalities": ["lexical"]})
        assert result.segments
        assert _revision_and_source(kb, "alpha")[0] == alpha_revision


def test_rebuild_applies_the_kind_map(old, tmp_path):
    root, _ = old
    report = rebuild_workspace_from(
        root, tmp_path / "new", kind_map={"document@1": "missing@1"}, token_counter=Counter()
    )
    assert report.sources == ()
    assert {item.reason for item in report.skipped} >= {"unsupported_kind"}


def test_rebuild_refuses_non_empty_or_same_destination(old, tmp_path):
    root, _ = old
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("x")
    for destination in (occupied, root, root / "nested"):
        with pytest.raises(TychoraError) as error:
            rebuild_workspace_from(root, destination, token_counter=Counter())
        assert error.value.reason == "rebuild.destination_not_empty"
    assert not (root / "nested").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/application/test_workspace_rebuild.py -v`
Expected: FAIL with `ImportError: cannot import name 'rebuild_workspace_from'`.

- [ ] **Step 3: Write the application module**

```python
# src/tychora/application/workspace_rebuild.py
"""Recreate a workspace in the current format from an older workspace's originals.

The old workspace is read through a read-only inventory. Every retained original
is re-added as a source job in the new workspace, so extraction, indexing and
publication run exactly as for a fresh add. Nothing is written to the old
workspace.
"""

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from tychora.adapters.sqlite.workspace_inventory import (
    InventoryCollection,
    InventoryRevision,
    InventorySource,
    WorkspaceInventory,
    read_inventory,
    read_original,
)
from tychora.contracts.base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ports.execution import ExecutionContext

RebuildSkipReason = Literal[
    "withdrawn",
    "repository_source",
    "unsupported_kind",
    "original_removed",
    "original_missing",
    "job_failed",
    "finer_review",
]
_INLINE_DROPPED = ("path", "url", "excludes", "content_sha256", "size_bytes", "sidecars")


class RebuildSkip(Contract):
    subject: Literal["source", "revision", "review"]
    key: NonEmptyStr
    reason: RebuildSkipReason
    detail: str = ""


class RebuiltSource(Contract):
    source_key: NonEmptyStr
    collection_id: Identifier
    revisions: NonNegativeInt
    job_ids: tuple[Identifier, ...]


class WorkspaceRebuildReport(Contract):
    old_workspace_id: Identifier
    new_workspace_id: Identifier
    collections: NonNegativeInt
    sources: tuple[RebuiltSource, ...]
    skipped: tuple[RebuildSkip, ...]
    reviews_replayed: NonNegativeInt


class RebuildTarget(Protocol):
    """The new workspace, opened with a grant over every rebuilt collection."""

    workspace_id: str

    def kinds(self) -> frozenset[str]: ...
    def seed_collections(self, collections: tuple[InventoryCollection, ...]) -> int: ...
    def add(self, source: SourceInput) -> str: ...
    def wait(self, job_id: str) -> str: ...
    def record_revision_review(self, revision_id: str, state: str, actor: str) -> None: ...
    def close(self) -> None: ...


@dataclass
class _Progress:
    sources: list[RebuiltSource] = field(default_factory=list)
    skipped: list[RebuildSkip] = field(default_factory=list)
    rebuilt_revisions: set[str] = field(default_factory=set)

    def skip(self, subject, key: str, reason: RebuildSkipReason, detail: str = "") -> None:
        self.skipped.append(RebuildSkip(subject=subject, key=key, reason=reason, detail=detail))


def _not_empty() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        "The rebuild destination must be a new, empty directory outside the old workspace.",
        reason="rebuild.destination_not_empty",
    )


def _checked_paths(old: Path, destination: Path) -> tuple[Path, Path]:
    old_root = old.resolve(strict=True)
    new_root = destination.resolve(strict=False)
    if new_root == old_root or new_root.is_relative_to(old_root) or old_root.is_relative_to(new_root):
        raise _not_empty()
    if new_root.exists() and (not new_root.is_dir() or any(new_root.iterdir())):
        raise _not_empty()
    return old_root, new_root


def rebuild_scope(collections: tuple[InventoryCollection, ...]) -> RequestScope:
    """Grant over every rebuilt collection, with the uploads they had consented to."""
    providers: set[str] = set()
    for item in collections:
        providers.update(json.loads(item.policy_json).get("upload_providers", ()))
    return RequestScope(
        principal_id="workspace-rebuild",
        collection_ids=frozenset(item.collection_id for item in collections),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage"}),
        upload_providers=frozenset(providers),
    )


def source_input(
    source: InventorySource, revision: InventoryRevision, kind: str, payload: bytes
) -> SourceInput:
    """The add request that reproduces one retained revision."""
    registration = json.loads(source.registration_json)
    for name in _INLINE_DROPPED:
        registration.pop(name, None)
    registration.update(
        kind=kind,
        metadata=json.loads(revision.metadata_json),
        source_key=source.source_key,
    )
    if registration.get("type") == "text":
        registration["text"] = payload.decode("utf-8")
        return SourceInput.from_mapping(registration)
    registration.update(type="bytes", data=payload)
    registration["collection_id"] = UUID(registration["collection_id"])
    return SourceInput.model_validate(registration)


def _replay_revision(
    old_root: Path,
    source: InventorySource,
    revision: InventoryRevision,
    target: RebuildTarget,
    kind: str,
    progress: _Progress,
) -> str | None:
    if revision.original is None:
        progress.skip("revision", revision.revision_id, "original_removed")
        return None
    payload = read_original(old_root, revision.original)
    if payload is None:
        progress.skip("revision", revision.revision_id, "original_missing")
        return None
    job_id = target.add(source_input(source, revision, kind, payload))
    state = target.wait(job_id)
    if state != "succeeded":
        progress.skip("revision", revision.revision_id, "job_failed", state)
        return None
    progress.rebuilt_revisions.add(revision.revision_id)
    return job_id


def _replay_source(
    old_root: Path,
    source: InventorySource,
    target: RebuildTarget,
    kind_map: Mapping[str, str],
    progress: _Progress,
    ctx: ExecutionContext,
) -> None:
    if source.withdrawn:
        progress.skip("source", source.source_key, "withdrawn")
        return
    if source.source_type == "repository":
        progress.skip("source", source.source_key, "repository_source", "re-add it from its path")
        return
    kinds = target.kinds()
    jobs: list[str] = []
    for revision in source.revisions:
        ctx.checkpoint()
        kind = kind_map.get(revision.kind_id, revision.kind_id)
        if kind not in kinds:
            progress.skip("revision", revision.revision_id, "unsupported_kind", kind)
            continue
        if job_id := _replay_revision(old_root, source, revision, target, kind, progress):
            jobs.append(job_id)
    if jobs:
        progress.sources.append(
            RebuiltSource(
                source_key=source.source_key,
                collection_id=source.collection_id,
                revisions=len(jobs),
                job_ids=tuple(jobs),
            )
        )


def _replay_reviews(
    inventory: WorkspaceInventory, target: RebuildTarget, progress: _Progress
) -> int:
    replayed = 0
    for review in inventory.reviews:
        if review.revision_id in progress.rebuilt_revisions:
            target.record_revision_review(review.revision_id, review.state, review.actor_id)
            replayed += 1
    if inventory.finer_reviews:
        progress.skip(
            "review",
            "units-and-assets",
            "finer_review",
            f"{inventory.finer_reviews} unit or asset review events are not replayed",
        )
    return replayed


def rebuild_workspace(
    old: Path,
    destination: Path,
    ctx: ExecutionContext,
    *,
    open_target: Callable[[Path, RequestScope], RebuildTarget],
    kind_map: Mapping[str, str] | None = None,
) -> WorkspaceRebuildReport:
    """Recreate ``destination`` in the current format from ``old``'s originals."""
    old_root, new_root = _checked_paths(old, destination)
    inventory = read_inventory(old_root)
    new_root.mkdir(parents=True, exist_ok=True)
    if (old_root / "workspace.toml").is_file():
        shutil.copyfile(old_root / "workspace.toml", new_root / "workspace.toml")
    target = open_target(new_root, rebuild_scope(inventory.collections))
    try:
        target.seed_collections(inventory.collections)
        progress = _Progress()
        for source in inventory.sources:
            _replay_source(old_root, source, target, kind_map or {}, progress, ctx)
        replayed = _replay_reviews(inventory, target, progress)
        return WorkspaceRebuildReport(
            old_workspace_id=inventory.workspace_id,
            new_workspace_id=target.workspace_id,
            collections=len(inventory.collections),
            sources=tuple(progress.sources),
            skipped=tuple(progress.skipped),
            reviews_replayed=replayed,
        )
    finally:
        target.close()
```

- [ ] **Step 4: Compose the target in `api.py`**

Add to the imports of `src/tychora/api.py`:

```python
from functools import partial

from tychora.adapters.sqlite.workspace_inventory import (
    InventoryCollection,
    seed_collections,
)
from tychora.application.workspace_rebuild import (
    RebuildTarget,
    WorkspaceRebuildReport,
    rebuild_workspace,
)
from tychora.contracts.corpus import ReviewTarget
```

and after `LocalWorkspace`:

```python
class _LocalRebuildTarget:
    """A freshly created local workspace receiving rebuilt sources."""

    def __init__(self, workspace: LocalWorkspace) -> None:
        self._workspace = workspace
        self.workspace_id = workspace.owner.workspace_id

    def _on_owner[T](self, call: Callable[[], T]) -> T:
        owner = self._workspace.owner
        return self._workspace.bridge.call(owner.run_blocking(call, ExecutionContext()))

    def kinds(self) -> frozenset[str]:
        return frozenset(self._workspace.owner.store.registry.kinds)

    def seed_collections(self, collections: tuple[InventoryCollection, ...]) -> int:
        owner = self._workspace.owner
        return self._on_owner(
            lambda: seed_collections(owner.db, owner.workspace_id, collections)
        )

    def add(self, source: SourceInput) -> str:
        workspace = self._workspace
        return workspace.services.source_jobs.add(source, workspace.scope, None).id

    def wait(self, job_id: str) -> str:
        workspace = self._workspace
        status = workspace.bridge.call(
            workspace.services.jobs.wait(job_id, ExecutionContext(), timeout=3600.0)
        )
        return status.state

    def record_revision_review(self, revision_id: str, state: str, actor: str) -> None:
        store = self._workspace.owner.store
        self._on_owner(
            lambda: store.record_review(ReviewTarget(revision_id=revision_id), state, actor)
        )

    def close(self) -> None:
        self._workspace.close()


def open_rebuild_target(
    path: Path,
    scope: RequestScope,
    *,
    providers: ProviderBindings | None = None,
    token_counter: TokenCounter | None = None,
) -> RebuildTarget:
    """Create the destination workspace of a rebuild with an explicit grant."""
    return _LocalRebuildTarget(
        LocalWorkspace(
            path,
            scope=scope,
            config=None,
            providers=providers,
            token_counter=token_counter,
        )
    )


def rebuild_workspace_from(
    old: Path,
    destination: Path,
    *,
    kind_map: Mapping[str, str] | None = None,
    providers: ProviderBindings | None = None,
    token_counter: TokenCounter | None = None,
) -> WorkspaceRebuildReport:
    """Recreate ``destination`` in the current format from ``old``'s originals."""
    return rebuild_workspace(
        Path(old),
        Path(destination),
        ExecutionContext(),
        open_target=partial(
            open_rebuild_target, providers=providers, token_counter=token_counter
        ),
        kind_map=kind_map,
    )
```

`TokenCounter` is imported only under `TYPE_CHECKING` in `api.py`; because the module starts with `from __future__ import annotations`, the annotations above need no runtime import.

`LocalWorkspace.__init__` writes the default configuration only when `workspace.toml` is missing; `rebuild_workspace` copies the old one first, so the new workspace resolves the same profile.

- [ ] **Step 5: Mark the module SQL-free**

In `tests/architecture/test_sql_free_modules.py`:

```python
SQL_FREE_MODULES: tuple[str, ...] = (
    "ports/repositories.py",
    "application/workspace_rebuild.py",
)
```

- [ ] **Step 6: Run the tests**

Run: `uv run --locked pytest tests/application/test_workspace_rebuild.py tests/architecture -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/application/workspace_rebuild.py src/tychora/api.py tests/application/test_workspace_rebuild.py tests/architecture/test_sql_free_modules.py
git commit -m "feat: rebuild a workspace in the current format from an older one"
```

---

### Task 10: `manage rebuild native` and `manage rebuild workspace --from`

**Files:**
- Modify: `src/tychora/interfaces/commands/manage.py:9` (import `load_json`), `:72-75` (replace the `rebuild` command)
- Modify: `docs/cli.md` (rebuild section)
- Test: `tests/cli/test_rebuild_commands.py`

**Interfaces:**
- Consumes: `rebuild_workspace_from` (Task 9), `RebuildMaintenance` (`contracts/operations.py:352`).
- Produces: CLI `tychora [--workspace NEW] manage rebuild native CHANNEL...` (same request as the former `manage rebuild CHANNEL...`) and `tychora --workspace NEW manage rebuild workspace --from OLD [--kind-map FILE] [--json]`.

`manage.py` keeps the CLI's lazy-import convention (`interfaces/cli.py` docstring: runtime imports occur only inside commands), so the new command adds one function-body import. Record that +1 on `interfaces/commands/manage.py` for gate G6 with the justification "CLI help must not import the runtime" in the pull request, as the code-health ratchet rule 4 requires for any hand-edited baseline value.

- [ ] **Step 1: Write the failing CLI tests**

```python
# tests/cli/test_rebuild_commands.py
"""Rebuild sub-commands: native channels and whole workspaces."""

import json

from typer.testing import CliRunner

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text, make_format_one
from tychora import Tychora
from tychora.interfaces.cli import app


def test_rebuild_native_keeps_the_maintenance_request(tmp_path):
    result = CliRunner().invoke(
        app, ["--workspace", str(tmp_path / "ws"), "manage", "rebuild", "native", "text"]
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["recovery"]["native_pending_batches"] == 0


def test_rebuild_workspace_from_old(tmp_path):
    old = tmp_path / "old"
    with Tychora.open(old, token_counter=Counter()) as kb:
        add_text(kb, "alpha", "Alpha.")
    make_format_one(old)
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "new"),
            "manage",
            "rebuild",
            "workspace",
            "--from",
            str(old),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert [item["source_key"] for item in report["sources"]] == ["alpha"]


def test_rebuild_workspace_refuses_occupied_destination(tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "x").write_text("x")
    (tmp_path / "old").mkdir()
    result = CliRunner().invoke(
        app,
        ["--workspace", str(tmp_path / "new"), "manage", "rebuild", "workspace", "--from", str(tmp_path / "old")],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["reason"] == "rebuild.destination_not_empty"
```

The third test passes because `_checked_paths` runs before the inventory; an empty `old` directory is never opened.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/cli/test_rebuild_commands.py -v`
Expected: FAIL (`No such command 'native'`).

- [ ] **Step 3: Replace the rebuild command with a sub-group**

In `manage.py`, change the `_common` import to `from ._common import client, emit, fail, load_json, progress, selected_format, state` and replace the `rebuild` command with:

```python
rebuild_group = typer.Typer(
    no_args_is_help=True, help="Rebuild projections or a whole workspace."
)
group.add_typer(rebuild_group, name="rebuild")


@rebuild_group.command("native")
def rebuild_native(ctx: typer.Context, channel: list[str] = typer.Argument(...)) -> None:
    """Re-project channels from retained rows, without provider calls."""
    _run(ctx, {"operation": "rebuild", "channels": channel})


@rebuild_group.command("workspace")
def rebuild_workspace(
    ctx: typer.Context,
    from_path: Path = typer.Option(..., "--from", help="Older workspace, opened read-only."),
    kind_map: Path | None = typer.Option(
        None, help="JSON object mapping old kind IDs to current kind IDs."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recreate --workspace (new, empty) from an older workspace's originals."""
    from tychora.api import rebuild_workspace_from

    options = state(ctx)
    try:
        output_format = selected_format(options, json_output)
        mapping = load_json(kind_map) if kind_map is not None else None
        report = rebuild_workspace_from(from_path, options.workspace, kind_map=mapping)
        emit(report, format=output_format)
    except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
        fail(error)
```

- [ ] **Step 4: Document the commands**

In `docs/cli.md`, replace the line describing `tychora manage rebuild CHANNEL...` with:

```markdown
- `tychora manage rebuild native CHANNEL...` re-projects the named channels
  (`lexical`, `text`, `image`, `graph`) from retained rows without provider calls.
- `tychora --workspace NEW manage rebuild workspace --from OLD [--kind-map FILE]`
  recreates an older workspace in the current storage format. `NEW` must be absent
  or empty; `OLD` is opened read-only and must not be open in another process.
  Every retained original is re-added as a source job; the JSON report lists the
  rebuilt sources and every skipped item with its reason (`withdrawn`,
  `repository_source`, `unsupported_kind`, `original_removed`, `original_missing`,
  `job_failed`, `finer_review`). `--kind-map` maps retired kind IDs to current ones.
```

(`grep -n "rebuild" docs/cli.md` locates the line; if the file has no rebuild entry, add these two items to the `manage` command list.)

- [ ] **Step 5: Run the CLI and maintenance suites**

Run: `uv run --locked pytest tests/cli tests/operations/test_migrations.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/interfaces/commands/manage.py docs/cli.md tests/cli/test_rebuild_commands.py
git commit -m "feat: add manage rebuild native and manage rebuild workspace commands"
```

---
### Task 11: Stable gold labels and their resolution

**Files:**
- Modify: `src/tychora/evaluation/dataset.py:1-60` (anchors, `GoldLabel`, `GoldBinding`, resolver protocol, `resolve_gold_binding`, `resolve_gold_label`, `bind_case`, `bind_cases`; `GoldCase` gains `gold_labels` and `forbidden_labels`)
- Modify: `src/tychora/contracts/operations.py:556-570` (`LabelFailure`; `EvaluationReport.label_failures`)
- Create: `src/tychora/adapters/sqlite/gold_labels.py`
- Test: `tests/evaluation/test_gold_labels.py`

**Interfaces:**
- Consumes: `normalize_source_key` (`contracts/identity.py:41`), `CodepointRange` (`contracts/base.py:126`).
- Produces (index names kept; the resolver protocol takes primitives because adapters may not import `tychora.evaluation`):

```python
class NativeRefAnchor(Contract): native_ref: NonEmptyStr
class CharRangeAnchor(CodepointRange): ...            # char_start, char_end on the original's codepoints
class PageAnchor(Contract): page: PositiveInt
class GoldLabel(Contract): collection: NonEmptyStr; source_key: NonEmptyStr; edition_key: str | None = None; anchor: NativeRefAnchor | CharRangeAnchor | PageAnchor
class GoldBinding(Contract): source_id: Identifier; revision_id: Identifier; unit_ids: tuple[Identifier, ...]
class GoldLabelResolver(Protocol):
    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]: ...
    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]: ...
    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]: ...
    def units_by_char_range(self, revision_id: str, char_start: int, char_end: int) -> tuple[str, ...]: ...
    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]: ...
class GoldLabelError(ValueError): reason: Literal["label.missing", "label.ambiguous"]; detail: str
def resolve_gold_binding(label: GoldLabel, resolver: GoldLabelResolver) -> GoldBinding
def resolve_gold_label(label: GoldLabel, resolver: GoldLabelResolver) -> tuple[str, ...]
def bind_case(case: GoldCase, resolver: GoldLabelResolver) -> GoldCase
def bind_cases(cases: tuple[GoldCase, ...], resolver: GoldLabelResolver) -> tuple[tuple[GoldCase, ...], tuple[LabelFailure, ...]]
# contracts/operations.py
class LabelFailure(Contract): case_id: Identifier; reason: Literal["label.missing", "label.ambiguous"]; detail: str
EvaluationReport.label_failures: tuple[LabelFailure, ...] = ()
# adapters/sqlite/gold_labels.py
class SqliteGoldLabelResolver:  def __init__(self, db: sqlite3.Connection, snapshot_id: str) -> None
```

Labels are the stable format: a collection (slug or ID), a source key, an optional edition key and an anchor, resolved to canonical IDs per workspace at evaluation time. Canonical-ID fields stay accepted, because a bound case is a case whose labels have been resolved into them; the executable synthetic fixture keeps them. New datasets, including every benchmark query set, use labels.

- [ ] **Step 1: Write the failing tests for resolution**

```python
# tests/evaluation/test_gold_labels.py
"""Gold labels resolve to canonical IDs or fail the case with a reason."""

import pytest

from tychora.evaluation.dataset import (
    CharRangeAnchor,
    GoldCase,
    GoldLabel,
    GoldLabelError,
    NativeRefAnchor,
    PageAnchor,
    bind_cases,
    resolve_gold_binding,
    resolve_gold_label,
)


class FakeResolver:
    def __init__(self, sources=("s1",), revisions=("r1",), units=("u1",)):
        self.sources, self.revisions, self.units = sources, revisions, units
        self.calls: list[tuple] = []

    def source_ids(self, collection, source_key):
        return self.sources

    def revision_ids(self, source_id, edition_key):
        return self.revisions

    def units_by_native_ref(self, revision_id, native_ref):
        self.calls.append(("native", native_ref))
        return self.units

    def units_by_char_range(self, revision_id, char_start, char_end):
        self.calls.append(("range", char_start, char_end))
        return self.units

    def units_by_page(self, revision_id, page):
        self.calls.append(("page", page))
        return self.units


def _label(anchor) -> GoldLabel:
    return GoldLabel(collection="default", source_key="guide", anchor=anchor)


def test_labels_parse_each_anchor_kind_from_json():
    for anchor, expected in (
        ({"native_ref": "block:3"}, NativeRefAnchor),
        ({"char_start": 4, "char_end": 9}, CharRangeAnchor),
        ({"page": 2}, PageAnchor),
    ):
        label = GoldLabel.from_mapping(
            {"collection": "default", "source_key": "guide", "anchor": anchor}
        )
        assert isinstance(label.anchor, expected)


def test_resolution_dispatches_on_the_anchor():
    resolver = FakeResolver()
    assert resolve_gold_label(_label(NativeRefAnchor(native_ref="block:3")), resolver) == ("u1",)
    resolve_gold_label(_label(CharRangeAnchor(char_start=4, char_end=9)), resolver)
    resolve_gold_label(_label(PageAnchor(page=2)), resolver)
    assert resolver.calls == [("native", "block:3"), ("range", 4, 9), ("page", 2)]
    binding = resolve_gold_binding(_label(PageAnchor(page=2)), resolver)
    assert (binding.source_id, binding.revision_id) == ("s1", "r1")


@pytest.mark.parametrize(
    ("resolver", "reason"),
    [
        (FakeResolver(sources=()), "label.missing"),
        (FakeResolver(sources=("s1", "s2")), "label.ambiguous"),
        (FakeResolver(revisions=()), "label.missing"),
        (FakeResolver(revisions=("r1", "r2")), "label.ambiguous"),
        (FakeResolver(units=()), "label.missing"),
    ],
)
def test_unresolvable_labels_carry_a_reason(resolver, reason):
    with pytest.raises(GoldLabelError) as error:
        resolve_gold_label(_label(PageAnchor(page=1)), resolver)
    assert error.value.reason == reason


def _case(case_id: str, label: GoldLabel) -> GoldCase:
    return GoldCase.from_mapping(
        {
            "case_id": case_id,
            "family_key": case_id,
            "split": "test",
            "query": {"question": "guide", "modalities": ["lexical"]},
            "gold_labels": [label.model_dump(mode="json")],
            "forbidden_labels": [],
        }
    )


def test_bind_cases_fills_ids_and_reports_failures():
    good = _case("good", _label(PageAnchor(page=1)))
    bound, failures = bind_cases((good,), FakeResolver())
    assert bound[0].expected_unit_ids == frozenset({"u1"})
    assert bound[0].expected_source_ids == frozenset({"s1"})
    assert bound[0].expected_revision_ids == frozenset({"r1"})
    bound, failures = bind_cases((good,), FakeResolver(sources=()))
    assert bound == ()
    assert [(item.case_id, item.reason) for item in failures] == [("good", "label.missing")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/evaluation/test_gold_labels.py -v`
Expected: FAIL with `ImportError: cannot import name 'CharRangeAnchor'`.

- [ ] **Step 3: Add `LabelFailure` to the operation contracts**

In `src/tychora/contracts/operations.py`, before `class EvaluationReport`:

```python
class LabelFailure(Contract):
    """A gold case skipped because one of its labels did not resolve."""

    case_id: Identifier
    reason: Literal["label.missing", "label.ambiguous"]
    detail: str
```

and add `label_failures: tuple[LabelFailure, ...] = ()` as the last field of `EvaluationReport`.

- [ ] **Step 4: Add labels and resolution to `evaluation/dataset.py`**

Change the imports to:

```python
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
```

add before `class GoldCase`:

```python
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
    source_id: Identifier
    revision_id: Identifier
    unit_ids: tuple[Identifier, ...]


class GoldLabelResolver(Protocol):
    """Canonical lookups a label resolution needs, scoped to one snapshot."""

    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]: ...
    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]: ...
    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]: ...
    def units_by_char_range(
        self, revision_id: str, char_start: int, char_end: int
    ) -> tuple[str, ...]: ...
    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]: ...


class GoldLabelError(ValueError):
    """A label that resolves to nothing, or to more than one source or revision."""

    def __init__(self, reason: Literal["label.missing", "label.ambiguous"], detail: str):
        super().__init__(detail)
        self.reason: Literal["label.missing", "label.ambiguous"] = reason
        self.detail = detail


def _single(values: tuple[str, ...], what: str) -> str:
    if not values:
        raise GoldLabelError("label.missing", f"no {what} matches the label")
    if len(values) > 1:
        raise GoldLabelError("label.ambiguous", f"{len(values)} {what}s match the label")
    return values[0]


def _units(label: GoldLabel, revision_id: str, resolver: GoldLabelResolver) -> tuple[str, ...]:
    anchor = label.anchor
    if isinstance(anchor, NativeRefAnchor):
        return resolver.units_by_native_ref(revision_id, anchor.native_ref)
    if isinstance(anchor, CharRangeAnchor):
        return resolver.units_by_char_range(revision_id, anchor.char_start, anchor.char_end)
    return resolver.units_by_page(revision_id, anchor.page)


def resolve_gold_binding(label: GoldLabel, resolver: GoldLabelResolver) -> GoldBinding:
    """Resolve a label to its source, revision and units in the pinned snapshot."""
    source_id = _single(resolver.source_ids(label.collection, label.source_key), "source")
    revision_id = _single(resolver.revision_ids(source_id, label.edition_key), "revision")
    unit_ids = _units(label, revision_id, resolver)
    if not unit_ids:
        raise GoldLabelError("label.missing", "the anchor matches no unit")
    return GoldBinding(source_id=source_id, revision_id=revision_id, unit_ids=unit_ids)


def resolve_gold_label(label: GoldLabel, resolver: GoldLabelResolver) -> tuple[str, ...]:
    """Resolve a label to the IDs of the units it designates."""
    return resolve_gold_binding(label, resolver).unit_ids
```

add two fields to `GoldCase` after `expected_spans`:

```python
    gold_labels: tuple[GoldLabel, ...] = ()
    forbidden_labels: tuple[GoldLabel, ...] = ()
```

and add after `class EvaluationDataset`:

```python
def bind_case(case: GoldCase, resolver: GoldLabelResolver) -> GoldCase:
    """Return the case with its labels resolved into canonical ID judgements."""
    if not case.gold_labels and not case.forbidden_labels:
        return case
    expected = [resolve_gold_binding(label, resolver) for label in case.gold_labels]
    forbidden = [resolve_gold_binding(label, resolver) for label in case.forbidden_labels]
    data = case.model_dump(mode="json")

    def merged(name: str, values: list[str]) -> list[str]:
        return sorted(set(data[name]) | set(values))

    data.update(
        expected_source_ids=merged("expected_source_ids", [b.source_id for b in expected]),
        expected_revision_ids=merged("expected_revision_ids", [b.revision_id for b in expected]),
        expected_unit_ids=merged(
            "expected_unit_ids", [unit for b in expected for unit in b.unit_ids]
        ),
        forbidden_source_ids=merged("forbidden_source_ids", [b.source_id for b in forbidden]),
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
                LabelFailure(case_id=case.case_id, reason=error.reason, detail=error.detail)
            )
    return tuple(bound), tuple(failures)
```

- [ ] **Step 5: Run the resolution tests**

Run: `uv run --locked pytest tests/evaluation/test_gold_labels.py tests/evaluation/test_dataset.py -v`
Expected: PASS.

- [ ] **Step 6: Write the SQLite resolver and its test**

```python
# src/tychora/adapters/sqlite/gold_labels.py
"""Resolve gold labels against one pinned snapshot's canonical rows."""

import sqlite3

from tychora.contracts.identity import normalize_source_key


class SqliteGoldLabelResolver:
    """Canonical lookups for label resolution; call on the owner thread."""

    def __init__(self, db: sqlite3.Connection, snapshot_id: str) -> None:
        self._db = db
        self._snapshot_id = snapshot_id

    def _ids(self, statement: str, parameters: tuple) -> tuple[str, ...]:
        return tuple(row[0] for row in self._db.execute(statement, parameters))

    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]:
        try:
            key = normalize_source_key(source_key)
        except ValueError:
            return ()
        return self._ids(
            "SELECT s.source_id FROM sources s JOIN collections c USING(collection_id) "
            "WHERE (c.slug=? OR c.collection_id=?) AND s.source_key=? "
            "AND s.withdrawn_at IS NULL ORDER BY s.source_id",
            (collection, collection, key),
        )

    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]:
        if edition_key is None:
            return self._ids(
                "SELECT revision_id FROM snapshot_members "
                "WHERE snapshot_id=? AND source_id=? AND is_default=1",
                (self._snapshot_id, source_id),
            )
        return self._ids(
            "SELECT m.revision_id FROM snapshot_members m JOIN revisions r USING(revision_id) "
            "WHERE m.snapshot_id=? AND m.source_id=? AND r.edition_key=? ORDER BY m.revision_id",
            (self._snapshot_id, source_id, edition_key),
        )

    def _extraction(self, revision_id: str) -> str | None:
        row = self._db.execute(
            "SELECT extraction_id FROM snapshot_members WHERE snapshot_id=? AND revision_id=?",
            (self._snapshot_id, revision_id),
        ).fetchone()
        return None if row is None else row[0]

    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]:
        return self._ids(
            "SELECT unit_id FROM evidence_units WHERE extraction_id=? AND native_ref=? "
            "ORDER BY ordinal",
            (self._extraction(revision_id), native_ref),
        )

    def units_by_char_range(
        self, revision_id: str, char_start: int, char_end: int
    ) -> tuple[str, ...]:
        return self._ids(
            "SELECT u.unit_id FROM evidence_units u WHERE u.extraction_id=? AND EXISTS("
            "SELECT 1 FROM locators l WHERE l.unit_id=u.unit_id "
            "AND l.locator_type IN ('text','code') "
            "AND json_extract(l.details_json,'$.char_start')<? "
            "AND json_extract(l.details_json,'$.char_end')>?) ORDER BY u.ordinal",
            (self._extraction(revision_id), char_end, char_start),
        )

    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]:
        return self._ids(
            "SELECT u.unit_id FROM evidence_units u WHERE u.extraction_id=? AND EXISTS("
            "SELECT 1 FROM locators l WHERE l.unit_id=u.unit_id AND l.physical_page=?) "
            "ORDER BY u.ordinal",
            (self._extraction(revision_id), page),
        )
```

Append to `tests/evaluation/test_gold_labels.py`:

```python
from tychora import Tychora
from tychora.adapters.sqlite.gold_labels import SqliteGoldLabelResolver
from tychora.ports.execution import ExecutionContext

TEXT = "# Guide\n\n## Checks\n\nAlpha beta zx00001q gamma.\n"


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def test_sqlite_resolver_finds_the_marker_unit(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=Counter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "guide",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": TEXT,
                "metadata": {"title": "Guide"},
            }
        )
        snapshot = kb.jobs.wait(job.id).result_snapshot_id
        owner = kb._workspace.owner
        start = TEXT.index("zx00001q")
        label = _label(CharRangeAnchor(char_start=start, char_end=start + 8))

        def resolve():
            resolver = SqliteGoldLabelResolver(owner.db, snapshot)
            units = resolve_gold_label(label, resolver)
            text = owner.db.execute(
                "SELECT text FROM evidence_units WHERE unit_id=?", (units[0],)
            ).fetchone()[0]
            missing = resolver.source_ids("default", "absent")
            return units, text, missing

        units, text, missing = kb._workspace.bridge.call(
            owner.run_blocking(resolve, ExecutionContext())
        )
    assert len(units) == 1
    assert "zx00001q" in text
    assert missing == ()
```

- [ ] **Step 7: Run the tests**

Run: `uv run --locked pytest tests/evaluation/test_gold_labels.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/tychora/evaluation/dataset.py src/tychora/contracts/operations.py src/tychora/adapters/sqlite/gold_labels.py tests/evaluation/test_gold_labels.py
git commit -m "feat: add stable gold labels resolved per workspace"
```

---

### Task 12: Evaluations bind labels and report unresolved cases

**Files:**
- Modify: `src/tychora/evaluation/runner.py:297-360` (`EvaluationService.run`: `accept()` binds cases; report carries failures)
- Modify: `docs/evaluation.md` ("Gold format and registration")
- Test: `tests/evaluation/test_gold_label_runs.py`

**Interfaces:**
- Consumes: `bind_cases`, `LabelFailure` (Task 11), `SqliteGoldLabelResolver` (Task 11).
- Produces: `EvaluationReport.label_failures` populated by every run; a run whose split has no resolvable case fails with `INVALID_REQUEST` reason `label.missing` or `label.ambiguous`.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/evaluation/test_gold_label_runs.py
"""A label-based gold set runs end to end and reports unresolved cases."""

from hashlib import sha256

from tychora import Tychora
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import MaintenanceRequest

TEXT = "Alpha beta zx00001q gamma."


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def _case(case_id: str, source_key: str) -> dict:
    start = TEXT.index("zx00001q")
    return {
        "case_id": case_id,
        "family_key": case_id,
        "split": "test",
        "query": {"question": "zx00001q", "modalities": ["lexical"]},
        "gold_labels": [
            {
                "collection": "default",
                "source_key": source_key,
                "anchor": {"char_start": start, "char_end": start + 8},
            }
        ],
    }


def test_label_based_dataset_scores_and_reports_failures(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=Counter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "guide",
                "collection_id": "default",
                "kind": "document@1",
                "format": "text",
                "text": TEXT,
                "metadata": {"title": "Guide"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        gold = tmp_path / "gold"
        gold.mkdir()
        payload = b"".join(
            canonical_bytes(case) + b"\n"
            for case in (_case("good", "guide"), _case("bad", "absent"))
        )
        (gold / "cases.jsonl").write_bytes(payload)
        (gold / "manifest.json").write_bytes(
            canonical_bytes(
                {
                    "format": "tychora-gold@1",
                    "dataset_id": "labels-v1",
                    "name": "Label-based gold",
                    "version_sha": sha256(payload).hexdigest(),
                    "case_count": 2,
                }
            )
        )
        kb.evaluations.register_dataset(gold)
        completed = kb.management.run(
            MaintenanceRequest.from_mapping(
                {"operation": "evaluate", "dataset_id": "labels-v1", "split": "test"}
            )
        )
        assert completed.job.state == "succeeded"
        stored = kb.evaluations.get(completed.job.id)
    assert set(stored["cases"]) == {"good"}
    assert [
        (item["case_id"], item["reason"]) for item in stored["report"]["label_failures"]
    ] == [("bad", "label.missing")]
    scored = {
        item["name"]: item["value"]
        for item in stored["cases"]["good"]["runs"][0]["metrics"]["metrics"]
    }
    assert scored["unit_recall"] == 1.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/evaluation/test_gold_label_runs.py -v`
Expected: FAIL (`KeyError: 'label_failures'` or the `bad` case scored with an empty denominator).

- [ ] **Step 3: Bind cases in the runner**

In `src/tychora/evaluation/runner.py` add imports:

```python
from tychora.adapters.sqlite.gold_labels import SqliteGoldLabelResolver
from tychora.evaluation.dataset import GoldCase, bind_cases, load_dataset, load_dataset_bytes
```

In `accept()`, directly after the `snapshot is None` check (the `NOT_FOUND` "Evaluation snapshot is unavailable" raise), insert:

```python
                cases, label_failures = bind_cases(
                    cases, SqliteGoldLabelResolver(self.owner.db, request.snapshot_id)
                )
                if not cases:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "No gold case of this split resolves in the workspace.",
                        reason=label_failures[0].reason,
                    )
```

Change the end of `accept()` to `return dataset, cases, label_failures, self._epochs(), snapshot[0], handle`, its caller to

```python
            dataset, cases, label_failures, epochs, config_sha, handle = (
                await self.owner.run_blocking(accept, ctx)
            )
```

and pass `label_failures=label_failures` to the `EvaluationReport(...)` constructor.

`validate_workload(len(cases), …)` and the `variant_query` pre-check run before binding and therefore count unresolved cases too; that only makes admission stricter, never looser.

- [ ] **Step 4: Document labels**

In `docs/evaluation.md`, append to "Gold format and registration":

```markdown
Cases may state judgements as stable labels instead of canonical IDs:
`gold_labels` and `forbidden_labels` are lists of
`{collection, source_key, edition_key?, anchor}`, where `collection` is a slug
or ID and `anchor` is `{native_ref}`, `{char_start, char_end}` (codepoints of
the original) or `{page}`. Labels resolve against the evaluated snapshot when a
run starts: expected labels add source, revision and unit IDs; forbidden labels
add source and revision IDs. A case whose label matches nothing
(`label.missing`) or more than one source or revision (`label.ambiguous`) is
not scored and is listed in the report's `label_failures`.
```

- [ ] **Step 5: Run the evaluation suite**

Run: `uv run --locked pytest tests/evaluation -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/evaluation/runner.py docs/evaluation.md tests/evaluation/test_gold_label_runs.py
git commit -m "feat: resolve gold labels at evaluation time and report unresolved cases"
```

---
### Task 13: Benchmark budgets, stage sources and result documents

**Files:**
- Create: `src/tychora/evaluation/bench/__init__.py`, `budgets.py`, `stages.py`, `results.py`
- Modify: `tests/architecture/test_sql_free_modules.py` (append `"evaluation/bench/budgets.py"`, `"evaluation/bench/stages.py"`)
- Test: `tests/evaluation/bench/test_measurement.py`

**Interfaces:**
- Consumes: `JobEvent` (`contracts/operations.py:321`).
- Produces:

```python
# budgets.py (spec §4.12, verbatim values)
QUERY_STAGE_BUDGETS_MS: Mapping[str, int]   # validate_scope_catalog 5, eligibility 40, candidates 70, fusion_admission 5,
                                            # survivors_rerank_facts 30, segments_assembly 50, record 5, local_total 205,
                                            # query_embedding 300, rerank 450, total 955
QUERY_BUDGET_TRACE_STAGES: Mapping[str, tuple[str, ...]]   # budget key -> trace stage names (phase 5 consumes)
LOCAL_QUERY_EMBEDDING_BUDGET_MS = 60
INGEST_STAGE_BUDGETS_S: Mapping[str, float] # acquire_register 0.5, parse 15.0, normalize_commit 1.5, windowing 1.0,
                                            # embedding 6.0, batch_writes 1.0, publication 0.5, total 25.5, projection 9.5
PER_PAGE_BUDGETS_S: Mapping[str, float]     # scanned_page 2.5, book_native_page 0.25
REFERENCE_TOLERANCE = 0.10; SHARED_RUNNER_REGRESSION = 0.20
# stages.py
class StageSample(Contract): stage: NonEmptyStr; duration_ms: float; status: NonEmptyStr
class StageSource(Protocol): query_stages(run_id) -> tuple[StageSample, ...]; ingest_stages(job_id) -> tuple[StageSample, ...]
class WallClockStageSource: __init__(events: Callable[[str], tuple[JobEvent, ...]])
def ingest_bucket(stage: str) -> str
# results.py
Suite = Literal["ingest", "query", "scale", "families"]; Profile = Literal["offline", "online"]
class StageStats, MachineFingerprint, CorpusIdentity, ScalePoint, Assertion, BenchResult (with document() -> dict)
def nearest_rank(samples: Sequence[float], fraction: float) -> float
def verdict(observed: float, budget: float | None, *, reference: bool) -> Verdict
def stage_stats(samples: Sequence[float], *, unit: Unit, budget: float | None, reference: bool, quantile: float) -> StageStats
def regressions(current: BenchResult, previous: BenchResult, tolerance: float = SHARED_RUNNER_REGRESSION) -> tuple[str, ...]
def machine_fingerprint() -> MachineFingerprint
def is_reference(machine: MachineFingerprint, spec: Mapping[str, object], runner_class: str | None) -> bool
def write_result(result: BenchResult, out_dir: Path) -> Path      # out_dir/<commit>/<suite>-<profile>.json
def load_result(path: Path) -> BenchResult
```

Until the query trace exists (engine core phase 5), the wall-clock source derives ingest stages from consecutive job-event timestamps (`acquire`, `capture` → `acquire_register`; `parse`; `project` and `projection/*` → `projection`, whose budget is the sum of normalize, windowing, embedding and batch writes; `publish` → `publication`) and queries report a single `total`. `BenchResult.retained_sha` holds `ResolvedConfig.config_sha` until phase 2 adds `retained_sha`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/evaluation/bench/test_measurement.py
"""Budgets, stage derivation, verdicts and result documents."""

from datetime import UTC, datetime, timedelta

import pytest

from tychora.contracts.operations import JobEvent
from tychora.evaluation.bench.budgets import (
    INGEST_STAGE_BUDGETS_S,
    QUERY_STAGE_BUDGETS_MS,
)
from tychora.evaluation.bench.results import (
    BenchResult,
    CorpusIdentity,
    is_reference,
    load_result,
    machine_fingerprint,
    nearest_rank,
    regressions,
    stage_stats,
    verdict,
    write_result,
)
from tychora.evaluation.bench.stages import WallClockStageSource, ingest_bucket

LOCAL = (
    "validate_scope_catalog",
    "eligibility",
    "candidates",
    "fusion_admission",
    "survivors_rerank_facts",
    "segments_assembly",
    "record",
)
INGEST = (
    "acquire_register",
    "parse",
    "normalize_commit",
    "windowing",
    "embedding",
    "batch_writes",
    "publication",
)


def test_budgets_match_the_spec_tables():
    assert sum(QUERY_STAGE_BUDGETS_MS[name] for name in LOCAL) == 205
    assert QUERY_STAGE_BUDGETS_MS["local_total"] == 205
    assert QUERY_STAGE_BUDGETS_MS["total"] == 205 + 300 + 450
    assert sum(INGEST_STAGE_BUDGETS_S[name] for name in INGEST) == pytest.approx(25.5)
    assert INGEST_STAGE_BUDGETS_S["projection"] == pytest.approx(1.5 + 1.0 + 6.0 + 1.0)


def test_nearest_rank_and_verdicts():
    samples = [float(value) for value in range(1, 101)]
    assert nearest_rank(samples, 0.95) == 95.0
    assert nearest_rank(samples, 0.5) == 50.0
    assert verdict(54.0, 50.0, reference=True) == "pass"
    assert verdict(56.0, 50.0, reference=True) == "fail"
    assert verdict(56.0, 50.0, reference=False) == "informational"
    assert verdict(56.0, None, reference=True) == "no_budget"
    stats = stage_stats(samples, unit="ms", budget=90.0, reference=True, quantile=0.95)
    assert (stats.n, stats.p95, stats.verdict) == (100, 95.0, "pass")


def _event(sequence: int, stage: str, at: datetime, state="running") -> JobEvent:
    return JobEvent(job_id="job", sequence=sequence, timestamp=at, stage=stage, state=state)


def test_wall_clock_source_derives_ingest_stages():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stages = ("accepted", "start", "acquire", "capture", "parse", "project", "publish", "complete")
    events = tuple(
        _event(index + 1, stage, start + timedelta(seconds=index), "succeeded" if stage == "complete" else "running")
        for index, stage in enumerate(stages)
    )
    samples = WallClockStageSource(lambda job_id: events).ingest_stages("job")
    durations = {sample.stage: sample.duration_ms for sample in samples}
    assert durations["parse"] == 1000.0
    assert durations["total"] == 7000.0
    assert [ingest_bucket(name) for name in ("acquire", "capture", "projection/plan/x", "publish")] == [
        "acquire_register",
        "acquire_register",
        "projection",
        "publication",
    ]
    assert WallClockStageSource(lambda job_id: events).query_stages("run") == ()


def _result(p95: float) -> BenchResult:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return BenchResult(
        commit="abc",
        suite="query",
        profile="offline",
        corpus=CorpusIdentity(name="bench-small", digest="a" * 64, documents=5, queries=6),
        machine=machine_fingerprint(),
        reference_machine=False,
        retained_sha="b" * 64,
        started_at=now,
        finished_at=now,
        stages={
            "total": stage_stats([p95], unit="ms", budget=205.0, reference=False, quantile=0.95)
        },
        usage={},
    )


def test_result_round_trip_and_regressions(tmp_path):
    path = write_result(_result(100.0), tmp_path)
    assert path == tmp_path / "abc" / "query-offline.json"
    document = path.read_text()
    assert '"schema":"tychora.bench-result"' in document
    assert load_result(path) == _result(100.0)
    assert regressions(_result(119.0), _result(100.0)) == ()
    assert regressions(_result(121.0), _result(100.0)) == ("total: p95 121.0 ms > 120.0 ms",)


def test_reference_machine_requires_the_declared_runner_class():
    machine = machine_fingerprint()
    spec = {"runner_class": "github-ubuntu-22.04", "os_prefix": machine.os[:5], "min_cores": 1}
    assert is_reference(machine, spec, "github-ubuntu-22.04")
    assert not is_reference(machine, spec, None)
    assert not is_reference(machine, spec, "laptop")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/evaluation/bench/test_measurement.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.evaluation.bench'`.

- [ ] **Step 3: Write `budgets.py` and `stages.py`**

```python
# src/tychora/evaluation/bench/__init__.py
"""Benchmark corpus, measurement and reporting for engine performance budgets."""
```

```python
# src/tychora/evaluation/bench/budgets.py
"""Performance budgets of the engine core design (section 4.12), in one place."""

from collections.abc import Mapping
from types import MappingProxyType

QUERY_STAGE_BUDGETS_MS: Mapping[str, int] = MappingProxyType(
    {
        "validate_scope_catalog": 5,
        "eligibility": 40,
        "candidates": 70,
        "fusion_admission": 5,
        "survivors_rerank_facts": 30,
        "segments_assembly": 50,
        "record": 5,
        "local_total": 205,
        "query_embedding": 300,
        "rerank": 450,
        "total": 955,
    }
)
QUERY_LOCAL_CEILING_MS = 300
QUERY_TOTAL_CEILING_MS = 1000
LOCAL_QUERY_EMBEDDING_BUDGET_MS = 60

QUERY_BUDGET_TRACE_STAGES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "eligibility": ("eligibility",),
        "candidates": ("exact", "lexical", "text_ann", "image_ann", "graph"),
        "fusion_admission": ("fusion", "admission"),
        "survivors_rerank_facts": ("rerank_facts",),
        "segments_assembly": ("assembly",),
        "record": ("record",),
        "query_embedding": ("text_embed", "image_embed"),
        "rerank": ("rerank",),
    }
)

INGEST_STAGE_BUDGETS_S: Mapping[str, float] = MappingProxyType(
    {
        "acquire_register": 0.5,
        "parse": 15.0,
        "normalize_commit": 1.5,
        "windowing": 1.0,
        "embedding": 6.0,
        "batch_writes": 1.0,
        "publication": 0.5,
        "total": 25.5,
        "projection": 9.5,
    }
)
INGEST_TOTAL_CEILING_S = 30.0
PER_PAGE_BUDGETS_S: Mapping[str, float] = MappingProxyType(
    {"scanned_page": 2.5, "book_native_page": 0.25}
)

REFERENCE_TOLERANCE = 0.10
SHARED_RUNNER_REGRESSION = 0.20
```

```python
# src/tychora/evaluation/bench/stages.py
"""Where stage durations come from: job events now, query traces later."""

from collections.abc import Callable
from typing import Annotated, Protocol

from pydantic import Field

from tychora.contracts.base import Contract, NonEmptyStr
from tychora.contracts.operations import JobEvent

_INGEST_BUCKETS = {
    "acquire": "acquire_register",
    "capture": "acquire_register",
    "parse": "parse",
    "project": "projection",
    "publish": "publication",
    "total": "total",
}


class StageSample(Contract):
    stage: NonEmptyStr
    duration_ms: Annotated[float, Field(ge=0)]
    status: NonEmptyStr


class StageSource(Protocol):
    """Per-stage durations of one query run or one ingestion job."""

    def query_stages(self, run_id: str) -> tuple[StageSample, ...]: ...
    def ingest_stages(self, job_id: str) -> tuple[StageSample, ...]: ...


def ingest_bucket(stage: str) -> str:
    """Map a job-event stage to its ingestion budget row."""
    if stage.startswith("projection/"):
        return "projection"
    return _INGEST_BUCKETS.get(stage, "queue")


class WallClockStageSource:
    """Stage durations from consecutive job-event timestamps; no query stages."""

    def __init__(self, events: Callable[[str], tuple[JobEvent, ...]]) -> None:
        self._events = events

    def query_stages(self, run_id: str) -> tuple[StageSample, ...]:
        return ()

    def ingest_stages(self, job_id: str) -> tuple[StageSample, ...]:
        events = self._events(job_id)
        samples = [
            StageSample(
                stage=current.stage,
                duration_ms=(following.timestamp - current.timestamp).total_seconds() * 1000,
                status=current.state,
            )
            for current, following in zip(events, events[1:], strict=False)
        ]
        if len(events) >= 2:
            samples.append(
                StageSample(
                    stage="total",
                    duration_ms=(events[-1].timestamp - events[0].timestamp).total_seconds()
                    * 1000,
                    status=events[-1].state,
                )
            )
        return tuple(samples)
```

- [ ] **Step 4: Write `results.py`**

```python
# src/tychora/evaluation/bench/results.py
"""Benchmark result documents: percentiles, verdicts, machine and corpus identity."""

import json
import math
import os
import platform
import sqlite3
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime

from tychora.contracts.base import (
    Contract,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from tychora.contracts.identity import canonical_bytes

from .budgets import REFERENCE_TOLERANCE, SHARED_RUNNER_REGRESSION

Suite = Literal["ingest", "query", "scale", "families"]
Profile = Literal["offline", "online"]
Unit = Literal["ms", "s"]
Verdict = Literal["pass", "fail", "informational", "no_budget"]


class StageStats(Contract):
    n: NonNegativeInt
    p50: float
    p90: float
    p95: float
    p99: float
    unit: Unit
    budget: float | None
    verdict: Verdict


class MachineFingerprint(Contract):
    cpu: NonEmptyStr
    cores: PositiveInt
    ram_bytes: NonNegativeInt
    os: NonEmptyStr
    python: NonEmptyStr
    sqlite: NonEmptyStr
    chroma: str | None


class CorpusIdentity(Contract):
    name: NonEmptyStr
    digest: Sha256
    documents: NonNegativeInt
    queries: NonNegativeInt


class ScalePoint(Contract):
    documents: PositiveInt
    add_p50_ms: float
    bytes_per_add: float
    search_p50_ms: float
    search_p95_ms: float
    largest_artifact_bytes: NonNegativeInt
    footprint_bytes: NonNegativeInt


class Assertion(Contract):
    name: NonEmptyStr
    passed: bool
    detail: str = ""


class BenchResult(Contract):
    commit: NonEmptyStr
    suite: Suite
    profile: Profile
    corpus: CorpusIdentity
    machine: MachineFingerprint
    reference_machine: bool
    retained_sha: Sha256
    started_at: AwareDatetime
    finished_at: AwareDatetime
    stages: dict[str, StageStats]
    scale: tuple[ScalePoint, ...] = ()
    usage: dict[str, int]
    assertions: tuple[Assertion, ...] = ()
    failures: tuple[str, ...] = ()
    aborted: bool = False

    def document(self) -> dict:
        """The JSON document written to ``bench-results``."""
        return {"schema": "tychora.bench-result", "version": 1, **self.model_dump(mode="json")}

    @property
    def failed(self) -> bool:
        return any(stats.verdict == "fail" for stats in self.stages.values())


def nearest_rank(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the ceil(q*n)-th smallest sample."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def verdict(observed: float, budget: float | None, *, reference: bool) -> Verdict:
    """Budgets fail only on the reference machine, with a 10% tolerance."""
    if budget is None:
        return "no_budget"
    if not reference:
        return "informational"
    return "fail" if observed > budget * (1 + REFERENCE_TOLERANCE) else "pass"


def stage_stats(
    samples: Sequence[float],
    *,
    unit: Unit,
    budget: float | None,
    reference: bool,
    quantile: float,
) -> StageStats:
    """Percentiles of one stage and its verdict at the asserted quantile."""
    values = [float(value) for value in samples] or [0.0]
    asserted = nearest_rank(values, quantile)
    return StageStats(
        n=len(samples),
        p50=nearest_rank(values, 0.50),
        p90=nearest_rank(values, 0.90),
        p95=nearest_rank(values, 0.95),
        p99=nearest_rank(values, 0.99),
        unit=unit,
        budget=None if budget is None else float(budget),
        verdict=verdict(asserted, budget, reference=reference) if samples else "no_budget",
    )


def regressions(
    current: BenchResult,
    previous: BenchResult,
    tolerance: float = SHARED_RUNNER_REGRESSION,
) -> tuple[str, ...]:
    """Stages whose p95 grew by more than ``tolerance`` against an earlier run."""
    found = []
    for name, stats in sorted(current.stages.items()):
        earlier = previous.stages.get(name)
        if earlier is None or not earlier.n:
            continue
        ceiling = round(earlier.p95 * (1 + tolerance), 3)
        if stats.p95 > ceiling:
            found.append(f"{name}: p95 {stats.p95} {stats.unit} > {ceiling} {stats.unit}")
    return tuple(found)


def machine_fingerprint() -> MachineFingerprint:
    """Describe the measuring machine and the storage libraries."""
    try:
        chroma = metadata.version("chromadb")
    except metadata.PackageNotFoundError:
        chroma = None
    return MachineFingerprint(
        cpu=platform.processor() or platform.machine() or "unknown",
        cores=os.cpu_count() or 1,
        ram_bytes=os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        os=platform.platform(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        chroma=chroma,
    )


def is_reference(
    machine: MachineFingerprint, spec: Mapping[str, object], runner_class: str | None
) -> bool:
    """True on the declared reference runner class (budgets are asserted there)."""
    return (
        runner_class is not None
        and runner_class == spec.get("runner_class")
        and machine.os.startswith(str(spec.get("os_prefix", "")))
        and machine.cores >= int(str(spec.get("min_cores", 1)))
    )


def write_result(result: BenchResult, out_dir: Path) -> Path:
    """Write ``<out_dir>/<commit>/<suite>-<profile>.json`` and return its path."""
    path = out_dir / result.commit / f"{result.suite}-{result.profile}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(result.document()) + b"\n")
    return path


def load_result(path: Path) -> BenchResult:
    """Read a result document written by ``write_result``."""
    document = json.loads(path.read_bytes())
    if document.pop("schema", None) != "tychora.bench-result" or document.pop("version", None) != 1:
        raise ValueError(f"{path} is not a tychora.bench-result version 1 document")
    return BenchResult.model_validate_json(json.dumps(document))
```

`results.py` imports `sqlite3` only to report the library version, so it is not added to `SQL_FREE_MODULES`.

- [ ] **Step 5: Run the tests**

Run: `uv run --locked pytest tests/evaluation/bench/test_measurement.py tests/architecture -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/evaluation/bench tests/evaluation/bench/test_measurement.py tests/architecture/test_sql_free_modules.py
git commit -m "feat: add benchmark budgets, stage sources and result documents"
```

---

### Task 14: Benchmark corpus, seeded synthetic documents and verified downloads

**Files:**
- Create: `src/tychora/evaluation/bench/synth.py`, `src/tychora/evaluation/bench/corpus.py`, `src/tychora/evaluation/bench/curation.py` (rasterizer only; Task 16 extends it)
- Create: `benchmarks/bench-small.toml`
- Modify: `tests/architecture/test_sql_free_modules.py` (append `"evaluation/bench/synth.py"`, `"evaluation/bench/corpus.py"`)
- Test: `tests/evaluation/bench/test_corpus.py`

**Interfaces:**
- Consumes: `GoldLabel`, `CharRangeAnchor` (Task 11).
- Produces:

```python
# synth.py
VERIFICATION_SENTENCE = "The verification outcome was recorded for this control."
PROBE_QUESTION = "verification outcome"
class SynthSpec(Contract): seed: NonNegativeInt; count: NonNegativeInt; markdown_share: float = 0.6; verification_share: float = 0.3; queries: NonNegativeInt = 20
class SynthDocument(Contract): source_key; title; format: Literal["markdown", "text"]; text: str; marker: str; marker_start: NonNegativeInt; has_verification: bool
def document_at(spec: SynthSpec, index: int) -> SynthDocument      # deterministic per (seed, index)
def generate(spec: SynthSpec) -> tuple[SynthDocument, ...]
# corpus.py
Category = Literal["native_pdf", "scanned_pdf", "book", "text", "code", "image", "synthetic"]
class BenchDocument(Contract): source_key; title; kind; category: Category; format: str | None; media_type; url; sha256; licence; transform: Literal["rasterize@150dpi"] | None = None
class BenchQuery(Contract): query_id; question; labels: tuple[GoldLabel, ...]; probe: bool = False
class BenchCorpus(Contract): name; synthetic: SynthSpec; documents: tuple[BenchDocument, ...]; queries: tuple[BenchQuery, ...]; def digest(self) -> str
class BenchCorpusError(ValueError)
def synth_queries(documents: Sequence[SynthDocument], count: int, collection: str = "default") -> tuple[BenchQuery, ...]
def load_corpus(name: str, root: Path) -> BenchCorpus
def all_queries(corpus: BenchCorpus) -> tuple[BenchQuery, ...]
def fetch_corpus(corpus: BenchCorpus, cache: Path, opener: Callable[[str], Any] | None = None, max_bytes: int = 512 * 1024**2) -> int
def document_bytes(document: BenchDocument, cache: Path) -> bytes
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/evaluation/bench/test_corpus.py
"""Deterministic synthetic documents, corpus files and verified downloads."""

import io
from hashlib import sha256
from pathlib import Path

import pytest

from tychora.evaluation.bench.corpus import (
    BenchCorpus,
    BenchCorpusError,
    BenchDocument,
    all_queries,
    fetch_corpus,
    load_corpus,
    synth_queries,
)
from tychora.evaluation.bench.synth import (
    PROBE_QUESTION,
    SynthSpec,
    document_at,
    generate,
)

BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"


def test_synthetic_documents_are_deterministic_and_mark_their_anchor():
    spec = SynthSpec(seed=7, count=5)
    first, again = generate(spec), generate(spec)
    assert first == again
    assert document_at(spec, 3) == first[3]
    for document in first:
        start = document.marker_start
        assert document.text[start : start + len(document.marker)] == document.marker
    assert len({document.marker for document in first}) == 5


def test_synthetic_queries_label_markers_and_the_probe():
    documents = generate(SynthSpec(seed=7, count=40, verification_share=0.5))
    queries = synth_queries(documents, 10)
    markers = [query for query in queries if not query.probe]
    assert len(markers) == 10
    probe = [query for query in queries if query.probe]
    assert probe and probe[0].question == PROBE_QUESTION
    assert all(label.anchor.char_end > label.anchor.char_start for label in probe[0].labels)


def test_bench_small_loads_with_a_stable_digest():
    corpus = load_corpus("bench-small", BENCHMARKS)
    assert corpus.synthetic.count == 100
    assert corpus.documents == ()
    assert corpus.digest() == load_corpus("bench-small", BENCHMARKS).digest()
    assert len(all_queries(corpus)) == 21


def _corpus(data: bytes) -> BenchCorpus:
    return BenchCorpus(
        name="tiny",
        synthetic=SynthSpec(seed=1, count=0),
        documents=(
            BenchDocument(
                source_key="public/one",
                title="One",
                kind="document@1",
                category="text",
                format="text",
                media_type="text/plain",
                url="https://example.org/one.txt",
                sha256=sha256(data).hexdigest(),
                licence="CC0-1.0",
            ),
        ),
        queries=(),
    )


def test_fetch_stores_verified_bytes_once(tmp_path):
    calls = []

    def opener(url):
        calls.append(url)
        return io.BytesIO(b"pinned bytes")

    corpus = _corpus(b"pinned bytes")
    assert fetch_corpus(corpus, tmp_path, opener) == 1
    assert fetch_corpus(corpus, tmp_path, opener) == 0
    assert calls == ["https://example.org/one.txt"]


def test_fetch_refuses_changed_bytes(tmp_path):
    corpus = _corpus(b"pinned bytes")
    with pytest.raises(BenchCorpusError, match="public/one"):
        fetch_corpus(corpus, tmp_path, lambda url: io.BytesIO(b"changed upstream"))
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/evaluation/bench/test_corpus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.evaluation.bench.corpus'`.

- [ ] **Step 3: Write `synth.py`**

```python
# src/tychora/evaluation/bench/synth.py
"""Seeded synthetic benchmark documents with one unique marker each."""

import random
from typing import Literal

from tychora.contracts.base import Contract, NonEmptyStr, NonNegativeInt

VERIFICATION_SENTENCE = "The verification outcome was recorded for this control."
PROBE_QUESTION = "verification outcome"
WORDS = (
    "account", "action", "annex", "approval", "archive", "assessment", "audit",
    "balance", "boundary", "budget", "calendar", "capacity", "channel", "charter",
    "claim", "clause", "committee", "compliance", "contract", "control", "council",
    "criteria", "decision", "delivery", "deposit", "design", "duty", "edition",
    "evidence", "exception", "filing", "finding", "framework", "guidance", "harbor",
    "incident", "inventory", "issuer", "journal", "ledger", "license", "maintenance",
    "measure", "method", "notice", "obligation", "operator", "outline", "payment",
    "permit", "policy", "procedure", "process", "record", "register", "report",
    "requirement", "review", "schedule", "section", "standard", "statement",
    "supplier", "threshold",
)


class SynthSpec(Contract):
    seed: NonNegativeInt
    count: NonNegativeInt
    markdown_share: float = 0.6
    verification_share: float = 0.3
    queries: NonNegativeInt = 20


class SynthDocument(Contract):
    source_key: NonEmptyStr
    title: NonEmptyStr
    format: Literal["markdown", "text"]
    text: str
    marker: NonEmptyStr
    marker_start: NonNegativeInt
    has_verification: bool


def _paragraph(rng: random.Random) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(30, 90))]
    return " ".join(words).capitalize() + "."


def _render(title: str, sections: list[tuple[str, list[str]]], markdown: bool) -> str:
    prefix, heading = ("# ", "## ") if markdown else ("", "")
    blocks = [prefix + title] + [
        heading + name + "\n\n" + "\n\n".join(paragraphs) for name, paragraphs in sections
    ]
    return "\n\n".join(blocks) + "\n"


def document_at(spec: SynthSpec, index: int) -> SynthDocument:
    """The index-th synthetic document; the same for every run with this seed."""
    rng = random.Random(f"{spec.seed}:{index}")
    markdown = rng.random() < spec.markdown_share
    verification = rng.random() < spec.verification_share
    title = f"Synthetic {rng.choice(WORDS)} {index}"
    sections = [
        (f"Section {number + 1} {rng.choice(WORDS)}", [_paragraph(rng) for _ in range(rng.randint(2, 4))])
        for number in range(rng.randint(2, 5))
    ]
    marker = f"zq{index:06d}x"
    target = sections[rng.randrange(len(sections))][1]
    target[0] = f"{target[0]} The {marker} marker identifies this passage."
    if verification:
        sections[-1][1].append(VERIFICATION_SENTENCE)
    text = _render(title, sections, markdown)
    return SynthDocument(
        source_key=f"synthetic/{index:06d}",
        title=title,
        format="markdown" if markdown else "text",
        text=text,
        marker=marker,
        marker_start=text.index(marker),
        has_verification=verification,
    )


def generate(spec: SynthSpec) -> tuple[SynthDocument, ...]:
    """The first ``spec.count`` synthetic documents."""
    return tuple(document_at(spec, index) for index in range(spec.count))
```

- [ ] **Step 4: Write `corpus.py`**

```python
# src/tychora/evaluation/bench/corpus.py
"""Benchmark corpus definitions, their digest, queries and verified downloads."""

import hashlib
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from tychora.contracts.base import Contract, NonEmptyStr, Sha256
from tychora.contracts.identity import canonical_bytes, digest
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel

from .curation import rasterize_pdf
from .synth import PROBE_QUESTION, SynthDocument, SynthSpec, generate

Category = Literal["native_pdf", "scanned_pdf", "book", "text", "code", "image", "synthetic"]
PROBE_LABEL_LIMIT = 50


class BenchDocument(Contract):
    source_key: NonEmptyStr
    title: NonEmptyStr
    kind: NonEmptyStr
    category: Category
    format: str | None = None
    media_type: NonEmptyStr
    url: NonEmptyStr
    sha256: Sha256
    licence: NonEmptyStr
    transform: Literal["rasterize@150dpi"] | None = None


class BenchQuery(Contract):
    query_id: NonEmptyStr
    question: NonEmptyStr
    labels: tuple[GoldLabel, ...]
    probe: bool = False


class BenchCorpus(Contract):
    name: NonEmptyStr
    synthetic: SynthSpec
    documents: tuple[BenchDocument, ...]
    queries: tuple[BenchQuery, ...]

    def digest(self) -> str:
        """Identity of everything that defines the corpus and its queries."""
        return digest("bench-corpus-v1", self.model_dump(mode="json"))


class BenchCorpusError(ValueError):
    """A corpus file or a download that cannot be used as pinned."""


def _range_label(document: SynthDocument, start: int, length: int, collection: str) -> GoldLabel:
    return GoldLabel(
        collection=collection,
        source_key=document.source_key,
        anchor=CharRangeAnchor(char_start=start, char_end=start + length),
    )


def synth_queries(
    documents: Sequence[SynthDocument], count: int, collection: str = "default"
) -> tuple[BenchQuery, ...]:
    """Marker queries spread over the documents, plus the strict-mode probe."""
    step = max(1, len(documents) // max(count, 1))
    queries = [
        BenchQuery(
            query_id=f"marker/{document.source_key}",
            question=document.marker,
            labels=(_range_label(document, document.marker_start, len(document.marker), collection),),
        )
        for document in list(documents)[::step][:count]
    ]
    probed = [document for document in documents if document.has_verification]
    if probed:
        queries.append(
            BenchQuery(
                query_id="probe/verification-outcome",
                question=PROBE_QUESTION,
                labels=tuple(
                    _range_label(
                        document, document.text.index(PROBE_QUESTION), len(PROBE_QUESTION), collection
                    )
                    for document in probed[:PROBE_LABEL_LIMIT]
                ),
                probe=True,
            )
        )
    return tuple(queries)


def load_corpus(name: str, root: Path) -> BenchCorpus:
    """Read ``<root>/<name>.toml`` and its query file."""
    path = root / f"{name}.toml"
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BenchCorpusError(f"{path}: {error}") from None
    if data.get("schema") != "tychora.bench-corpus" or data.get("version") != 1:
        raise BenchCorpusError(f"{path} is not a tychora.bench-corpus version 1 file")
    lines = (root / data["queries"]).read_text().splitlines() if "queries" in data else []
    return BenchCorpus.model_validate_json(
        canonical_bytes(
            {
                "name": data["name"],
                "synthetic": data["synthetic"],
                "documents": data.get("documents", []),
                "queries": [BenchQuery.model_validate_json(line).model_dump(mode="json") for line in lines if line],
            }
        )
    )


def all_queries(corpus: BenchCorpus) -> tuple[BenchQuery, ...]:
    """Curated queries followed by the synthetic marker queries and the probe."""
    return corpus.queries + synth_queries(generate(corpus.synthetic), corpus.synthetic.queries)


def _urlopen(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "tychora-bench/1"})
    return urllib.request.urlopen(request, timeout=120)  # noqa: S310 - pinned https URLs


def fetch_corpus(
    corpus: BenchCorpus,
    cache: Path,
    opener: Callable[[str], Any] | None = None,
    max_bytes: int = 512 * 1024**2,
) -> int:
    """Download missing public documents into ``cache``; return how many were fetched."""
    cache.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for document in corpus.documents:
        target = cache / document.sha256
        if target.is_file():
            continue
        with (opener or _urlopen)(document.url) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise BenchCorpusError(f"{document.source_key}: download exceeds {max_bytes} bytes")
        if hashlib.sha256(data).hexdigest() != document.sha256:
            raise BenchCorpusError(
                f"{document.source_key}: bytes from {document.url} differ from the pinned SHA-256"
            )
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        partial.replace(target)
        fetched += 1
    return fetched


def document_bytes(document: BenchDocument, cache: Path) -> bytes:
    """The verified bytes of a fetched document, transformed when it is derived."""
    data = (cache / document.sha256).read_bytes()
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise BenchCorpusError(f"{document.source_key}: cached bytes are damaged; fetch again")
    if document.transform is None:
        return data
    derived = cache / f"{document.sha256}.{document.transform.replace('@', '-')}.pdf"
    if not derived.is_file():
        derived.write_bytes(rasterize_pdf(data))
    return derived.read_bytes()
```

- [ ] **Step 4b: Write the rasterizer used for derived scans**

```python
# src/tychora/evaluation/bench/curation.py
"""Helpers that turn public sources into benchmark documents and queries."""

import io

try:
    import pypdfium2

    RASTERIZE_AVAILABLE = True
except ImportError:  # the documents extra is not installed
    RASTERIZE_AVAILABLE = False


class CurationUnavailable(RuntimeError):
    """A curation step needs the documents extra."""


def rasterize_pdf(data: bytes, dpi: int = 150) -> bytes:
    """Render every page to an image and return an image-only PDF (a synthetic scan)."""
    if not RASTERIZE_AVAILABLE:
        raise CurationUnavailable("Rasterizing needs `uv sync --extra documents`.")
    document = pypdfium2.PdfDocument(data)
    try:
        pages = [
            document[index].render(scale=dpi / 72).to_pil().convert("RGB")
            for index in range(len(document))
        ]
    finally:
        document.close()
    output = io.BytesIO()
    pages[0].save(output, "PDF", save_all=True, append_images=pages[1:], resolution=float(dpi))
    return output.getvalue()
```

Task 16 adds the known-item and TOML helpers to this module.

- [ ] **Step 5: Write `benchmarks/bench-small.toml`**

```toml
# Engine-core benchmark corpus: 100 seeded synthetic documents, 20 marker queries
# and the "verification outcome" strict-mode probe. No download is needed.
schema = "tychora.bench-corpus"
version = 1
name = "bench-small"

[synthetic]
seed = 20261016
count = 100
markdown_share = 0.6
verification_share = 0.3
queries = 20
```

- [ ] **Step 6: Run the tests**

Run: `uv run --locked pytest tests/evaluation/bench/test_corpus.py tests/architecture -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/evaluation/bench benchmarks/bench-small.toml tests/evaluation/bench/test_corpus.py tests/architecture/test_sql_free_modules.py
git commit -m "feat: add the benchmark corpus format, synthetic generator and verified fetch"
```

---
### Task 15: Benchmark suites and the `python -m tychora.interfaces.bench` entry point

**Files:**
- Create: `src/tychora/evaluation/bench/harness.py`
- Create: `src/tychora/interfaces/bench.py`
- Create: `benchmarks/reference-machine.toml`, `benchmarks/online-workspace.toml`
- Modify: `tests/architecture/test_sql_free_modules.py` (append `"evaluation/bench/harness.py"`, `"interfaces/bench.py"`)
- Test: `tests/evaluation/bench/test_harness.py`, `tests/cli/test_bench_command.py`

**Interfaces:**
- Consumes: Tasks 13 and 14; `Tychora` public client; `resolve_config` (`configuration/resolve.py`); `SourceInput`.
- Produces:

```python
# harness.py
class BenchSource(Contract): source_key; title; kind; category: Category; format: str | None = None; media_type: str | None = None; text: str | None = None; data: bytes | None = None
class SearchOutcome(Contract): run_id: str | None; unit_ids: tuple[str, ...]; error_code: str | None; duration_ms: float
class Footprint(Contract): total_bytes: NonNegativeInt; largest_artifact_bytes: NonNegativeInt
class BenchClient(Protocol): add(source) -> str; wait(job_id) -> str; events(job_id) -> tuple[JobEvent, ...]; search(question) -> SearchOutcome; footprint() -> Footprint
class BenchUnavailable(Exception)
@dataclass(frozen=True) class RunContext: commit; profile; machine; reference: bool; retained_sha; checkpoints = (100, 1000, 10000); max_wall_seconds: float | None = None; limit: int | None = None; clock = time.monotonic
def iter_sources(corpus: BenchCorpus, cache: Path, limit: int | None = None) -> Iterator[BenchSource]
def run_ingest(client, sources, stage_source) -> Samples
def run_queries(client, queries, stage_source) -> Samples
def run_scale(client, spec: SynthSpec, context: RunContext) -> Samples
def run_suite(client: BenchClient, corpus: BenchCorpus, suite: Suite, context: RunContext, cache: Path, stage_source: StageSource) -> BenchResult
# interfaces/bench.py
class TychoraBenchClient: __init__(kb: Tychora, root: Path, modalities: tuple[str, ...])
def main(argv: Sequence[str] | None = None) -> int     # 0 ok, 1 budget failure or regression, 2 unusable corpus or suite
```

Suites: `ingest` adds every document and records per-job stage durations (seconds, asserted at p90); `query` ingests the corpus unmeasured, warms each query once, then records each query's wall time (milliseconds, asserted at p95 against the local share offline and the total online) and the strict-mode probe as an assertion; `scale` adds synthetic documents up to each checkpoint and, over the last 20 adds before it, records add latency, workspace bytes grown per add, the largest artifact and search latency over 20 marker queries. Workspace growth per add is the harness's stand-in for "bytes written by publication per add" (E3); the SQLite WAL is checkpointed by then only if SQLite chose to, so compare checkpoints of the same run, never absolute numbers. `families` exits with code 2 until vector family options exist.

- [ ] **Step 1: Write the failing harness tests**

```python
# tests/evaluation/bench/test_harness.py
"""Suites measured against a fake client."""

from datetime import UTC, datetime, timedelta

import pytest

from tychora.contracts.operations import JobEvent
from tychora.evaluation.bench.corpus import BenchCorpus
from tychora.evaluation.bench.harness import (
    BenchUnavailable,
    Footprint,
    RunContext,
    SearchOutcome,
    run_suite,
)
from tychora.evaluation.bench.results import machine_fingerprint
from tychora.evaluation.bench.stages import WallClockStageSource
from tychora.evaluation.bench.synth import SynthSpec

STAGES = ("accepted", "start", "acquire", "capture", "parse", "project", "publish", "complete")


class FakeClient:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, source) -> str:
        self.added.append(source.source_key)
        return f"job-{len(self.added)}"

    def wait(self, job_id: str) -> str:
        return "succeeded"

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        return tuple(
            JobEvent(
                job_id=job_id,
                sequence=index + 1,
                timestamp=start + timedelta(seconds=index),
                stage=stage,
                state="succeeded" if stage == "complete" else "running",
            )
            for index, stage in enumerate(STAGES)
        )

    def search(self, question: str) -> SearchOutcome:
        return SearchOutcome(run_id=None, unit_ids=("unit",), error_code=None, duration_ms=12.0)

    def footprint(self) -> Footprint:
        return Footprint(total_bytes=1000 * len(self.added), largest_artifact_bytes=500)


def _corpus(**synthetic) -> BenchCorpus:
    return BenchCorpus(
        name="fake",
        synthetic=SynthSpec(seed=3, count=10, queries=4, **synthetic),
        documents=(),
        queries=(),
    )


def _context(**overrides) -> RunContext:
    values = dict(
        commit="abc",
        profile="offline",
        machine=machine_fingerprint(),
        reference=False,
        retained_sha="c" * 64,
    )
    values.update(overrides)
    return RunContext(**values)


def test_ingest_suite_buckets_job_stages(tmp_path):
    client = FakeClient()
    result = run_suite(client, _corpus(), "ingest", _context(limit=5), tmp_path, WallClockStageSource(client.events))
    assert len(client.added) == 5
    assert result.stages["parse"].n == 5
    assert result.stages["acquire_register"].p50 == 2.0
    assert result.stages["total"].budget == 25.5
    assert result.stages["total"].verdict == "informational"


def test_query_suite_records_totals_and_the_probe(tmp_path):
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(verification_share=1.0),
        "query",
        _context(),
        tmp_path,
        WallClockStageSource(client.events),
    )
    assert result.stages["total"].budget == 205.0
    assert result.stages["total"].n == result.corpus.queries
    assert [assertion.passed for assertion in result.assertions] == [True]


def test_scale_suite_measures_each_checkpoint(tmp_path):
    client = FakeClient()
    result = run_suite(
        client, _corpus(), "scale", _context(checkpoints=(5, 10)), tmp_path, WallClockStageSource(client.events)
    )
    assert [(point.documents, point.bytes_per_add) for point in result.scale] == [(5, 1000.0), (10, 1000.0)]
    assert not result.aborted


def test_scale_suite_stops_at_the_wall_limit(tmp_path):
    ticks = iter(range(1000))
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(),
        "scale",
        _context(checkpoints=(10,), max_wall_seconds=3.0, clock=lambda: float(next(ticks))),
        tmp_path,
        WallClockStageSource(client.events),
    )
    assert result.aborted
    assert len(client.added) < 10


def test_families_suite_is_unavailable(tmp_path):
    client = FakeClient()
    with pytest.raises(BenchUnavailable):
        run_suite(client, _corpus(), "families", _context(), tmp_path, WallClockStageSource(client.events))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --locked pytest tests/evaluation/bench/test_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.evaluation.bench.harness'`.

- [ ] **Step 3: Write `harness.py`**

```python
# src/tychora/evaluation/bench/harness.py
"""Benchmark suites over a workspace client: ingestion, queries and scale."""

import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Protocol

from tychora.contracts.base import Contract, NonEmptyStr, NonNegativeInt
from tychora.contracts.operations import JobEvent

from .budgets import INGEST_STAGE_BUDGETS_S, QUERY_STAGE_BUDGETS_MS
from .corpus import BenchCorpus, BenchQuery, Category, all_queries, document_bytes
from .results import (
    Assertion,
    BenchResult,
    CorpusIdentity,
    MachineFingerprint,
    Profile,
    ScalePoint,
    StageStats,
    Suite,
    nearest_rank,
    stage_stats,
)
from .stages import StageSource, ingest_bucket
from .synth import SynthDocument, SynthSpec, document_at, generate

SCALE_WINDOW = 20


class BenchSource(Contract):
    source_key: NonEmptyStr
    title: NonEmptyStr
    kind: NonEmptyStr
    category: Category
    format: str | None = None
    media_type: str | None = None
    text: str | None = None
    data: bytes | None = None


class SearchOutcome(Contract):
    run_id: str | None
    unit_ids: tuple[str, ...]
    error_code: str | None
    duration_ms: float


class Footprint(Contract):
    total_bytes: NonNegativeInt
    largest_artifact_bytes: NonNegativeInt


class BenchClient(Protocol):
    """The workspace operations a benchmark needs."""

    def add(self, source: BenchSource) -> str: ...
    def wait(self, job_id: str) -> str: ...
    def events(self, job_id: str) -> tuple[JobEvent, ...]: ...
    def search(self, question: str) -> SearchOutcome: ...
    def footprint(self) -> Footprint: ...


class BenchUnavailable(Exception):
    """A suite this version cannot run."""


@dataclass
class Samples:
    stages: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    assertions: list[Assertion] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    scale: list[ScalePoint] = field(default_factory=list)
    ingested: set[str] = field(default_factory=set)
    aborted: bool = False


@dataclass(frozen=True)
class RunContext:
    commit: str
    profile: Profile
    machine: MachineFingerprint
    reference: bool
    retained_sha: str
    checkpoints: tuple[int, ...] = (100, 1000, 10000)
    max_wall_seconds: float | None = None
    limit: int | None = None
    clock: Callable[[], float] = time.monotonic


def _synthetic(document: SynthDocument) -> BenchSource:
    return BenchSource(
        source_key=document.source_key,
        title=document.title,
        kind="document@1",
        category="synthetic",
        format=document.format,
        text=document.text,
    )


def iter_sources(corpus: BenchCorpus, cache: Path, limit: int | None = None) -> Iterator[BenchSource]:
    """Synthetic documents first, then public ones read lazily from the cache."""

    def produce() -> Iterator[BenchSource]:
        for document in generate(corpus.synthetic):
            yield _synthetic(document)
        for document in corpus.documents:
            yield BenchSource(
                source_key=document.source_key,
                title=document.title,
                kind=document.kind,
                category=document.category,
                format=document.format,
                media_type=document.media_type,
                data=document_bytes(document, cache),
            )

    return islice(produce(), limit)


def run_ingest(client: BenchClient, sources: Iterable[BenchSource], stage_source: StageSource) -> Samples:
    """Add each source and record its stage durations in seconds."""
    samples = Samples()
    for source in sources:
        job_id = client.add(source)
        state = client.wait(job_id)
        if state != "succeeded":
            samples.failures.append(f"{source.source_key}: job {state}")
            continue
        samples.ingested.add(source.source_key)
        per_job: dict[str, float] = defaultdict(float)
        for sample in stage_source.ingest_stages(job_id):
            per_job[ingest_bucket(sample.stage)] += sample.duration_ms / 1000
        for bucket, seconds in per_job.items():
            samples.stages[bucket].append(seconds)
    return samples


def run_queries(client: BenchClient, queries: Iterable[BenchQuery], stage_source: StageSource) -> Samples:
    """Warm each query once, then record per-stage and total durations in milliseconds."""
    selected = tuple(queries)
    for query in selected:
        client.search(query.question)
    samples = Samples()
    for query in selected:
        outcome = client.search(query.question)
        stages = stage_source.query_stages(outcome.run_id) if outcome.run_id else ()
        for sample in stages:
            samples.stages[sample.stage].append(sample.duration_ms)
        if not any(sample.stage == "total" for sample in stages):
            samples.stages["total"].append(outcome.duration_ms)
        if outcome.error_code:
            samples.failures.append(f"{query.query_id}: {outcome.error_code}")
        if query.probe:
            samples.assertions.append(
                Assertion(
                    name=query.query_id,
                    passed=outcome.error_code is None and bool(outcome.unit_ids),
                    detail=outcome.error_code or "",
                )
            )
    return samples


def _grow(
    client: BenchClient,
    spec: SynthSpec,
    span: tuple[int, int],
    samples: Samples,
    context: RunContext,
    started: float,
) -> list[float]:
    latencies = []
    for index in range(*span):
        if context.max_wall_seconds is not None and context.clock() - started > context.max_wall_seconds:
            samples.aborted = True
            break
        began = time.perf_counter()
        state = client.wait(client.add(_synthetic(document_at(spec, index))))
        latencies.append((time.perf_counter() - began) * 1000)
        if state != "succeeded":
            samples.failures.append(f"synthetic/{index:06d}: job {state}")
    return latencies


def _checkpoint(client: BenchClient, documents: int, latencies: list[float], before: Footprint, questions: list[str]) -> ScalePoint:
    after = client.footprint()
    searches = [client.search(question).duration_ms for question in questions] or [0.0]
    return ScalePoint(
        documents=documents,
        add_p50_ms=nearest_rank(latencies, 0.5),
        bytes_per_add=(after.total_bytes - before.total_bytes) / len(latencies),
        search_p50_ms=nearest_rank(searches, 0.5),
        search_p95_ms=nearest_rank(searches, 0.95),
        largest_artifact_bytes=after.largest_artifact_bytes,
        footprint_bytes=after.total_bytes,
    )


def run_scale(client: BenchClient, spec: SynthSpec, context: RunContext) -> Samples:
    """Grow a synthetic workspace through the checkpoints, measuring each window."""
    samples, added, started = Samples(), 0, context.clock()
    questions = [document_at(spec, index).marker for index in range(SCALE_WINDOW)]
    for checkpoint in sorted(context.checkpoints):
        window = max(added, checkpoint - SCALE_WINDOW)
        _grow(client, spec, (added, window), samples, context, started)
        before = client.footprint()
        latencies = _grow(client, spec, (window, checkpoint), samples, context, started)
        if samples.aborted:
            return samples
        added = checkpoint
        if latencies:
            samples.scale.append(
                _checkpoint(client, checkpoint, latencies, before, questions[: min(checkpoint, SCALE_WINDOW)])
            )
    return samples


def _budget(suite: Suite, profile: Profile, stage: str) -> float | None:
    if suite == "ingest":
        return INGEST_STAGE_BUDGETS_S.get(stage)
    if suite == "query" and stage == "total":
        return float(QUERY_STAGE_BUDGETS_MS["local_total" if profile == "offline" else "total"])
    if suite == "query" and stage in QUERY_STAGE_BUDGETS_MS:
        return float(QUERY_STAGE_BUDGETS_MS[stage])
    return None


def _summarize(samples: Samples, suite: Suite, context: RunContext) -> dict[str, StageStats]:
    unit = "s" if suite == "ingest" else "ms"
    quantile = 0.90 if suite == "ingest" else 0.95
    return {
        stage: stage_stats(
            values,
            unit=unit,
            budget=_budget(suite, context.profile, stage),
            reference=context.reference,
            quantile=quantile,
        )
        for stage, values in sorted(samples.stages.items())
    }


def _measure(client: BenchClient, corpus: BenchCorpus, suite: Suite, context: RunContext, cache: Path, stage_source: StageSource) -> Samples:
    if suite == "ingest":
        return run_ingest(client, iter_sources(corpus, cache, context.limit), stage_source)
    if suite == "query":
        loaded = run_ingest(client, iter_sources(corpus, cache, context.limit), stage_source)
        queries = [
            query
            for query in all_queries(corpus)
            if any(label.source_key in loaded.ingested for label in query.labels)
        ]
        return run_queries(client, queries, stage_source)
    return run_scale(client, corpus.synthetic, context)


def run_suite(
    client: BenchClient,
    corpus: BenchCorpus,
    suite: Suite,
    context: RunContext,
    cache: Path,
    stage_source: StageSource,
) -> BenchResult:
    """Run one suite and return its result document."""
    if suite == "families":
        raise BenchUnavailable(
            "The families suite needs vector family options, which this version does not have yet."
        )
    started_at = datetime.now(UTC)
    samples = _measure(client, corpus, suite, context, cache, stage_source)
    return BenchResult(
        commit=context.commit,
        suite=suite,
        profile=context.profile,
        corpus=CorpusIdentity(
            name=corpus.name,
            digest=corpus.digest(),
            documents=corpus.synthetic.count + len(corpus.documents),
            queries=len(samples.stages["total"]) if suite == "query" else len(all_queries(corpus)),
        ),
        machine=context.machine,
        reference_machine=context.reference,
        retained_sha=context.retained_sha,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        stages=_summarize(samples, suite, context),
        scale=tuple(samples.scale),
        usage={},
        assertions=tuple(samples.assertions),
        failures=tuple(samples.failures),
        aborted=samples.aborted,
    )
```

- [ ] **Step 4: Run the harness tests**

Run: `uv run --locked pytest tests/evaluation/bench/test_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing entry-point test**

```python
# tests/cli/test_bench_command.py
"""The benchmark entry point runs a small offline suite end to end."""

import json

from tychora.interfaces.bench import main


def test_offline_query_suite_writes_a_result(tmp_path):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "query",
            "--profile",
            "offline",
            "--limit",
            "5",
            "--out",
            str(tmp_path / "results"),
            "--cache",
            str(tmp_path / "cache"),
        ]
    )
    assert code == 0
    [path] = list((tmp_path / "results").rglob("query-offline.json"))
    document = json.loads(path.read_text())
    assert document["schema"] == "tychora.bench-result"
    assert document["stages"]["total"]["n"] >= 1
    assert document["corpus"]["name"] == "bench-small"
    assert {"cpu", "cores", "ram_bytes", "os", "python", "sqlite", "chroma"} <= set(
        document["machine"]
    )


def test_families_suite_exits_with_usage_code(tmp_path, capsys):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "families",
            "--profile",
            "offline",
            "--cache",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert "families suite" in capsys.readouterr().err
```

- [ ] **Step 6: Write `interfaces/bench.py` and the two benchmark settings files**

```python
# src/tychora/interfaces/bench.py
"""Run the engine benchmark: ``python -m tychora.interfaces.bench fetch|run``."""

import argparse
import os
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobEvent
from tychora.contracts.requests import SourceInput
from tychora.evaluation.bench.corpus import BenchCorpusError, fetch_corpus, load_corpus
from tychora.evaluation.bench.harness import (
    BenchSource,
    BenchUnavailable,
    Footprint,
    RunContext,
    SearchOutcome,
    run_suite,
)
from tychora.evaluation.bench.results import (
    is_reference,
    load_result,
    machine_fingerprint,
    regressions,
    write_result,
)
from tychora.evaluation.bench.stages import WallClockStageSource

DEFAULT_BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"
OFFLINE_CONFIG = (
    'schema_version = 1\n[profiles.bench]\nbuiltin = "general-documents@1"\n'
    '[defaults]\nprofile = "bench"\n'
)


class BenchCounter:
    """Deterministic output counter for offline runs (about four characters a token)."""

    model = "bench-quarter-codepoints"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)


class TychoraBenchClient:
    """A benchmark client over one open local workspace."""

    def __init__(self, kb: Tychora, root: Path, modalities: tuple[str, ...]) -> None:
        self._kb = kb
        self._root = root
        self._modalities = modalities

    def add(self, source: BenchSource) -> str:
        fields: dict[str, object] = {
            "source_key": source.source_key,
            "collection_id": self._kb.default_collection_id,
            "kind": source.kind,
            "metadata": {"title": source.title},
        }
        if source.format:
            fields["format"] = source.format
        if source.media_type:
            fields["media_type"] = source.media_type
        if source.text is not None:
            fields.update(type="text", text=source.text)
        else:
            fields.update(type="bytes", data=source.data)
        return self._kb.sources.add(SourceInput.model_validate(fields)).id

    def wait(self, job_id: str) -> str:
        return self._kb.jobs.wait(job_id, timeout=3600.0).state

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        return tuple(self._kb.jobs.events_after(job_id))

    def search(self, question: str) -> SearchOutcome:
        began = time.perf_counter()
        try:
            result = self._kb.search({"question": question, "modalities": list(self._modalities)})
        except TychoraError as error:
            return SearchOutcome(
                run_id=None,
                unit_ids=(),
                error_code=error.code,
                duration_ms=(time.perf_counter() - began) * 1000,
            )
        return SearchOutcome(
            run_id=result.run_id,
            unit_ids=tuple(unit for segment in result.segments for unit in segment.matched_unit_ids),
            error_code=None,
            duration_ms=(time.perf_counter() - began) * 1000,
        )

    def footprint(self) -> Footprint:
        total = largest = 0
        for path in self._root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                total += size
                if path.parent.name == "artifacts":
                    largest = max(largest, size)
        return Footprint(total_bytes=total, largest_artifact_bytes=largest)


def _commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() or "unknown"


def _checkpoints(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(",") if part)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tychora.interfaces.bench")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Download and verify a corpus's public documents.")
    run = commands.add_parser("run", help="Run one benchmark suite.")
    for sub in (fetch, run):
        sub.add_argument("--corpus", required=True)
        sub.add_argument("--cache", type=Path, default=Path(".cache/bench"))
        sub.add_argument("--benchmarks", type=Path, default=DEFAULT_BENCHMARKS)
    run.add_argument("--suite", required=True, choices=("ingest", "query", "scale", "families"))
    run.add_argument("--profile", required=True, choices=("offline", "online"))
    run.add_argument("--out", type=Path, default=Path("bench-results"))
    run.add_argument("--workspace", type=Path)
    run.add_argument("--limit", type=int)
    run.add_argument("--checkpoints", type=_checkpoints, default=(100, 1000, 10000))
    run.add_argument("--max-wall-seconds", type=float)
    run.add_argument("--compare", type=Path, help="Earlier result of the same runner class.")
    return parser


def _open(workspace: Path, profile: str, benchmarks: Path) -> tuple[Tychora, str]:
    workspace.mkdir(parents=True, exist_ok=True)
    settings = workspace / "workspace.toml"
    if not settings.exists():
        settings.write_text(
            OFFLINE_CONFIG if profile == "offline" else (benchmarks / "online-workspace.toml").read_text()
        )
    config = resolve_config(settings, "bench", {})
    kb = Tychora.open(
        workspace, config=config, token_counter=BenchCounter() if profile == "offline" else None
    )
    if profile == "online":
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": str(kb.default_collection_id),
                "provider": "voyage",
                "action": "grant",
            }
        )
    return kb, config.config_sha


def _problems(result, compare: Path | None) -> list[str]:
    found = list(regressions(result, load_result(compare))) if compare else []
    found += [f"{name}: over budget" for name, stats in result.stages.items() if stats.verdict == "fail"]
    return found


def _run(args: argparse.Namespace) -> int:
    if args.suite == "families":
        raise BenchUnavailable(
            "The families suite needs vector family options, which this version does not have yet."
        )
    corpus = load_corpus(args.corpus, args.benchmarks)
    workspace = args.workspace or args.cache / "workspaces" / f"{args.suite}-{args.profile}-{time.time_ns()}"
    if workspace.exists() and any(workspace.iterdir()):
        raise BenchUnavailable(f"{workspace} must be a new, empty directory")
    machine = machine_fingerprint()
    reference = tomllib.loads((args.benchmarks / "reference-machine.toml").read_text())
    kb, config_sha = _open(workspace, args.profile, args.benchmarks)
    try:
        client = TychoraBenchClient(
            kb, workspace, ("lexical",) if args.profile == "offline" else ("lexical", "text")
        )
        context = RunContext(
            commit=_commit(),
            profile=args.profile,
            machine=machine,
            reference=is_reference(machine, reference, os.environ.get("TYCHORA_BENCH_RUNNER_CLASS")),
            retained_sha=config_sha,
            checkpoints=args.checkpoints,
            max_wall_seconds=args.max_wall_seconds,
            limit=args.limit,
        )
        result = run_suite(client, corpus, args.suite, context, args.cache, WallClockStageSource(client.events))
    finally:
        kb.close()
    print(write_result(result, args.out))
    problems = _problems(result, args.compare)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run ``fetch`` or ``run``; return the process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            print(fetch_corpus(load_corpus(args.corpus, args.benchmarks), args.cache))
            return 0
        return _run(args)
    except (BenchCorpusError, BenchUnavailable) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

```toml
# benchmarks/reference-machine.toml
# Budget verdicts fail only on this runner class; elsewhere they are informational.
runner_class = "github-ubuntu-22.04"
os_prefix = "Linux"
min_cores = 4
```

```toml
# benchmarks/online-workspace.toml
# Online benchmark profile: Voyage text embeddings with a Chroma index.
schema_version = 1

[profiles.bench]
builtin = "general-documents@1"

[defaults]
profile = "bench"

[configuration]
offline = false

[configuration.providers.text]
provider = "voyage"
model = "voyage-context-4"
dimensions = 1024
credential_ref = "env:VOYAGE_API_KEY"

[configuration.index.text]
backend = "chroma"
```

- [ ] **Step 7: Run the tests**

Run: `uv run --locked pytest tests/evaluation/bench tests/cli/test_bench_command.py tests/architecture -v`
Expected: PASS.

- [ ] **Step 8: Run the offline gate commands once by hand**

Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline --out /tmp/tychora-bench --cache /tmp/tychora-bench-cache`
Expected: prints `/tmp/tychora-bench/<commit>/ingest-offline.json`, exit code 0. The document's `stages` has `acquire_register`, `parse`, `projection`, `publication`, `total`, each with `n` 100.

- [ ] **Step 9: Commit**

```bash
git add src/tychora/evaluation/bench/harness.py src/tychora/interfaces/bench.py benchmarks/reference-machine.toml benchmarks/online-workspace.toml tests/evaluation/bench/test_harness.py tests/cli/test_bench_command.py tests/architecture/test_sql_free_modules.py
git commit -m "feat: add benchmark suites and the bench entry point"
```

---
### Task 16: `bench-v1` curation, scheduled runs and documentation

**Files:**
- Modify: `src/tychora/evaluation/bench/curation.py` (add `known_item`, `page_known_item`, `corpus_toml`)
- Create: `benchmarks/curate_bench_v1.py`
- Create (generated by the script, then committed): `benchmarks/bench-v1.toml`, `benchmarks/queries/bench-v1.jsonl`
- Create: `.github/workflows/bench.yml`
- Modify: `.gitignore` (bench results)
- Modify: `docs/performance.md` (benchmark section)
- Test: `tests/evaluation/bench/test_curation.py`

**Interfaces:**
- Consumes: `BenchDocument`, `BenchQuery`, `load_corpus` (Task 14); `GoldLabel`, `CharRangeAnchor`, `PageAnchor` (Task 11); `SynthSpec` (Task 14).
- Produces: `known_item(text: str, rng: random.Random, words: int = 7) -> tuple[str, int, int] | None` (question, sentence start, sentence end in codepoints); `page_known_item(pages: Sequence[str], rng: random.Random, words: int = 7) -> tuple[str, int] | None` (question, 1-based page); `corpus_toml(name: str, synthetic: Contract, documents: Sequence[Contract], queries_path: str, header: str) -> str`.

`bench-v1` mix (spec §4.10), with its public sources and licences:

| Category | Count | Source | Licence |
| --- | ---: | --- | --- |
| Native PDF ≤ 100 pages | 400 | UK Public General Acts, `https://www.legislation.gov.uk/ukpga/{year}/{number}/data.pdf` | Open Government Licence v3.0 |
| Native PDF ≤ 100 pages | 200 | arXiv computer-science papers whose OAI-PMH `license` is CC BY 4.0, `https://arxiv.org/pdf/{id}v1` | CC BY 4.0 |
| Scanned PDF | 50 | The first 50 acts above with ≤ 20 pages, rasterized at 150 dpi on fetch (`transform = "rasterize@150dpi"`) | Open Government Licence v3.0 |
| Book > 300 pages | 10 | Project Gutenberg plain text (IDs 1342, 2701, 1400, 98, 2600, 135, 145, 766, 1399, 4300) | Public domain in the USA (Project Gutenberg License) |
| Markdown or text | 200 | Python Enhancement Proposals at a pinned commit of `python/peps`, only those stating public domain or CC0 | Public domain or CC0-1.0 |
| Code | 100 | Top-level modules of `Lib/` in CPython at tag `v3.12.7` | PSF-2.0 |
| Image | 40 | Wikimedia Commons, category `CC-zero`, PNG or JPEG ≤ 5 MB | CC0-1.0 |

When a source yields fewer documents (network errors, licence filter), the seeded synthetic generator fills the corpus to 1,000 documents; queries are 150 curated known-item queries, the synthetic marker queries and the probe, 200 in total. Code files are ingested as `document@1` text until the language packs of chunking v2 exist. The "2026-10-15 real-PDF probe" queries named in spec §4.10 were never written down (`docs/superpowers/2026-10-15-large-document-limits.md` gives timings, not queries); only "verification outcome" is included, and the gap is reported.

- [ ] **Step 1: Write the failing curation tests**

```python
# tests/evaluation/bench/test_curation.py
"""Known-item queries and corpus files produced by curation."""

import random

from tychora.evaluation.bench.corpus import BenchDocument, BenchQuery, load_corpus
from tychora.evaluation.bench.curation import corpus_toml, known_item, page_known_item
from tychora.evaluation.bench.synth import SynthSpec
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel

TEXT = (
    "Short.\n"
    "The harbour authority publishes a schedule of maintenance dredging every spring season.\n"
    "Another sentence that is long enough to be chosen as a known item for the query set."
)


def test_known_item_quotes_one_sentence():
    question, start, end = known_item(TEXT, random.Random(1))
    assert question in TEXT[start:end]
    assert len(question.split()) == 7
    assert known_item("Too short.", random.Random(1)) is None


def test_page_known_item_returns_a_one_based_page():
    question, page = page_known_item(["", TEXT], random.Random(2))
    assert page == 2
    assert question in TEXT


def test_corpus_toml_round_trips_through_load_corpus(tmp_path):
    document = BenchDocument(
        source_key="public/pep-0008",
        title="Style Guide for Python Code",
        kind="document@1",
        category="text",
        format="text",
        media_type="text/plain",
        url="https://raw.githubusercontent.com/python/peps/abc/peps/pep-0008.rst",
        sha256="d" * 64,
        licence="Public domain or CC0-1.0",
    )
    query = BenchQuery(
        query_id="known/public/pep-0008",
        question="code is read much more often",
        labels=(
            GoldLabel(
                collection="default",
                source_key="public/pep-0008",
                anchor=CharRangeAnchor(char_start=10, char_end=60),
            ),
        ),
    )
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "x.jsonl").write_text(query.model_dump_json() + "\n")
    (tmp_path / "x.toml").write_text(
        corpus_toml("x", SynthSpec(seed=1, count=3), [document], "queries/x.jsonl", "Test corpus.")
    )
    corpus = load_corpus("x", tmp_path)
    assert corpus.documents == (document,)
    assert corpus.queries == (query,)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --locked pytest tests/evaluation/bench/test_curation.py -v`
Expected: FAIL with `ImportError: cannot import name 'corpus_toml'`.

- [ ] **Step 3: Add the curation helpers**

Append to `src/tychora/evaluation/bench/curation.py` (and add `import json`, `import random`, `import re`, `from collections.abc import Sequence` and `from tychora.contracts.base import Contract` to its imports):

```python
_SENTENCE = re.compile(r"[^.!?\n]{40,400}[.!?]")


def known_item(text: str, rng: random.Random, words: int = 7) -> tuple[str, int, int] | None:
    """A query of ``words`` consecutive words and the codepoint range of its sentence."""
    sentences = [match for match in _SENTENCE.finditer(text) if len(match.group().split()) >= words + 2]
    if not sentences:
        return None
    match = rng.choice(sentences)
    tokens = match.group().split()
    first = rng.randrange(0, len(tokens) - words + 1)
    return " ".join(tokens[first : first + words]), match.start(), match.end()


def page_known_item(
    pages: Sequence[str], rng: random.Random, words: int = 7
) -> tuple[str, int] | None:
    """A known-item query drawn from one page, with the page's 1-based number."""
    candidates = [index for index, text in enumerate(pages) if _SENTENCE.search(text)]
    rng.shuffle(candidates)
    for index in candidates:
        item = known_item(pages[index], rng, words)
        if item is not None:
            return item[0], index + 1
    return None


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value, ensure_ascii=True)


def corpus_toml(
    name: str,
    synthetic: Contract,
    documents: Sequence[Contract],
    queries_path: str,
    header: str,
) -> str:
    """Render a ``tychora.bench-corpus`` version 1 file."""
    lines = [f"# {line}" for line in header.splitlines()]
    lines += [
        'schema = "tychora.bench-corpus"',
        "version = 1",
        f"name = {_toml_value(name)}",
        f"queries = {_toml_value(queries_path)}",
        "",
        "[synthetic]",
    ]
    lines += [f"{key} = {_toml_value(value)}" for key, value in synthetic.model_dump().items()]
    for document in documents:
        lines += ["", "[[documents]]"]
        lines += [
            f"{key} = {_toml_value(value)}"
            for key, value in document.model_dump().items()
            if value is not None
        ]
    return "\n".join(lines) + "\n"
```

JSON string escapes (`\"`, `\\`, `\uXXXX`) are valid TOML basic-string escapes, so `json.dumps(..., ensure_ascii=True)` is a correct TOML string encoder.

- [ ] **Step 4: Run the curation tests**

Run: `uv run --locked pytest tests/evaluation/bench/test_curation.py -v`
Expected: PASS.

- [ ] **Step 5: Write the curation script**

```python
# benchmarks/curate_bench_v1.py
"""Curate benchmarks/bench-v1.toml and benchmarks/queries/bench-v1.jsonl.

Run once with network access, review the summary, commit both outputs. Every
document is pinned by SHA-256; downloads go to --cache and are never committed.

    uv run --locked python benchmarks/curate_bench_v1.py --cache .cache/bench
"""

import argparse
import hashlib
import io
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from tychora.evaluation.bench.corpus import BenchDocument, BenchQuery
from tychora.evaluation.bench.curation import corpus_toml, known_item, page_known_item
from tychora.evaluation.bench.synth import SynthSpec
from tychora.evaluation.dataset import CharRangeAnchor, GoldLabel, PageAnchor

ROOT = Path(__file__).resolve().parent
SEED = 20261016
TARGET_DOCUMENTS = 1000
TARGET_QUERIES = 200
CURATED_QUERIES = 150
OGL = "Open Government Licence v3.0"
GUTENBERG = {
    1342: "Pride and Prejudice",
    2701: "Moby Dick",
    1400: "Great Expectations",
    98: "A Tale of Two Cities",
    2600: "War and Peace",
    135: "Les Miserables",
    145: "Middlemarch",
    766: "David Copperfield",
    1399: "Anna Karenina",
    4300: "Ulysses",
}
CPYTHON_TAG = "v3.12.7"
ARXIV_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}
Entry = tuple[BenchDocument, bytes]


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tychora-bench-curation/1"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed public hosts
        data = response.read()
    time.sleep(1)
    return data


def entry(key, title, category, fmt, media, url, licence, data, transform=None) -> Entry:
    document = BenchDocument(
        source_key=key,
        title=title,
        kind="document@1",
        category=category,
        format=fmt,
        media_type=media,
        url=url,
        sha256=hashlib.sha256(data).hexdigest(),
        licence=licence,
        transform=transform,
    )
    return document, data


def page_count(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def legislation(limit: int = 400) -> list[Entry]:
    found: list[Entry] = []
    for year in range(2023, 2000, -1):
        for number in range(1, 61):
            if len(found) >= limit:
                return found
            url = f"https://www.legislation.gov.uk/ukpga/{year}/{number}/data.pdf"
            try:
                data = get(url)
                if page_count(data) > 100:
                    continue
            except (urllib.error.URLError, ValueError):
                continue
            title = f"UK Public General Act {year} c. {number}"
            found.append(entry(f"public/ukpga/{year}-{number}", title, "native_pdf", None, "application/pdf", url, OGL, data))
    return found


def _arxiv_page(url: str, limit: int, found: list[Entry]) -> str | None:
    tree = ElementTree.fromstring(get(url))
    for record in tree.iterfind(".//a:arXiv", ARXIV_NS):
        if len(found) >= limit:
            return None
        if record.findtext("a:license", "", ARXIV_NS) != "http://creativecommons.org/licenses/by/4.0/":
            continue
        identifier = record.findtext("a:id", "", ARXIV_NS)
        title = " ".join(record.findtext("a:title", "", ARXIV_NS).split())
        pdf = f"https://arxiv.org/pdf/{identifier}v1"
        try:
            data = get(pdf)
            if page_count(data) > 100:
                continue
        except (urllib.error.URLError, ValueError):
            continue
        found.append(entry(f"public/arxiv/{identifier}", title, "native_pdf", None, "application/pdf", pdf, "CC BY 4.0", data))
        time.sleep(2)
    token = tree.findtext(".//oai:resumptionToken", None, ARXIV_NS)
    return f"https://export.arxiv.org/oai2?verb=ListRecords&resumptionToken={urllib.parse.quote(token)}" if token else None


def arxiv(limit: int = 200) -> list[Entry]:
    found: list[Entry] = []
    url: str | None = "https://export.arxiv.org/oai2?verb=ListRecords&metadataPrefix=arXiv&set=cs&from=2024-01-01"
    while url and len(found) < limit:
        url = _arxiv_page(url, limit, found)
    return found


def scans(acts: list[Entry], limit: int = 50) -> list[Entry]:
    short = [(document, data) for document, data in acts if page_count(data) <= 20][:limit]
    return [
        entry(document.source_key + "-scan", document.title + " (scan)", "scanned_pdf", None, "application/pdf", document.url, OGL, data, "rasterize@150dpi")
        for document, data in short
    ]


def books() -> list[Entry]:
    licence = "Public domain in the USA (Project Gutenberg License)"
    return [
        entry(f"public/gutenberg/{number}", title, "book", "text", "text/plain", url, licence, get(url))
        for number, title in GUTENBERG.items()
        for url in [f"https://www.gutenberg.org/cache/epub/{number}/pg{number}.txt"]
    ]


def peps(limit: int = 200) -> list[Entry]:
    commit = json.loads(get("https://api.github.com/repos/python/peps/commits/main"))["sha"]
    listing = json.loads(get(f"https://api.github.com/repos/python/peps/contents/peps?ref={commit}"))
    names = sorted(item["name"] for item in listing if re.fullmatch(r"pep-\d{4}\.rst", item["name"]))
    found: list[Entry] = []
    for name in names:
        if len(found) >= limit:
            break
        url = f"https://raw.githubusercontent.com/python/peps/{commit}/peps/{name}"
        data = get(url)
        if b"public domain" not in data.lower() and b"CC0" not in data:
            continue
        found.append(entry(f"public/peps/{name[:-4]}", name[:-4].upper(), "text", "text", "text/plain", url, "Public domain or CC0-1.0", data))
    return found


def cpython(limit: int = 100) -> list[Entry]:
    listing = json.loads(get(f"https://api.github.com/repos/python/cpython/contents/Lib?ref={CPYTHON_TAG}"))
    names = sorted(item["name"] for item in listing if item["name"].endswith(".py"))[:limit]
    return [
        entry(f"public/cpython/{name}", f"CPython Lib/{name}", "code", "text", "text/plain", url, "PSF-2.0", get(url))
        for name in names
        for url in [f"https://raw.githubusercontent.com/python/cpython/{CPYTHON_TAG}/Lib/{name}"]
    ]


def images(limit: int = 40) -> list[Entry]:
    api = (
        "https://commons.wikimedia.org/w/api.php?action=query&generator=categorymembers"
        "&gcmtitle=Category:CC-zero&gcmtype=file&gcmlimit=200&prop=imageinfo"
        "&iiprop=url|mime|size&format=json"
    )
    pages = sorted(json.loads(get(api))["query"]["pages"].values(), key=lambda page: page["title"])
    found: list[Entry] = []
    for page in pages:
        info = page.get("imageinfo", [{}])[0]
        if len(found) >= limit or info.get("mime") not in ("image/png", "image/jpeg"):
            continue
        if info.get("size", 0) > 5_000_000:
            continue
        found.append(entry(f"public/commons/{page['pageid']}", page["title"], "image", None, info["mime"], info["url"], "CC0-1.0", get(info["url"])))
    return found


def curated_queries(entries: list[Entry], rng: random.Random) -> list[BenchQuery]:
    candidates = [item for item in entries if item[0].category in ("native_pdf", "book", "text", "code")]
    rng.shuffle(candidates)
    queries: list[BenchQuery] = []
    for document, data in candidates:
        if len(queries) >= CURATED_QUERIES:
            break
        if document.category == "native_pdf":
            pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
            item = page_known_item(pages, rng)
            anchor = None if item is None else PageAnchor(page=item[1])
        else:
            found = known_item(data.decode("utf-8", errors="replace"), rng)
            item = None if found is None else (found[0], 0)
            anchor = None if found is None else CharRangeAnchor(char_start=found[1], char_end=found[2])
        if item is None or anchor is None:
            continue
        label = GoldLabel(collection="default", source_key=document.source_key, anchor=anchor)
        queries.append(BenchQuery(query_id=f"known/{document.source_key}", question=item[0], labels=(label,)))
    return queries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    cache = parser.parse_args().cache
    cache.mkdir(parents=True, exist_ok=True)
    acts = legislation()
    entries = acts + arxiv() + scans(acts) + books() + peps() + cpython() + images()
    for document, data in entries:
        (cache / document.sha256).write_bytes(data)
    queries = curated_queries(entries, random.Random(SEED))
    documents = [document for document, _ in entries]
    synthetic = SynthSpec(
        seed=SEED,
        count=max(0, TARGET_DOCUMENTS - len(documents)),
        queries=max(0, TARGET_QUERIES - 1 - len(queries)),
    )
    (ROOT / "queries").mkdir(exist_ok=True)
    (ROOT / "queries" / "bench-v1.jsonl").write_text("".join(query.model_dump_json() + "\n" for query in queries))
    header = "Engine-core benchmark corpus bench-v1, generated by benchmarks/curate_bench_v1.py.\nPublic documents are pinned by SHA-256 and fetched into a cache; never commit them."
    (ROOT / "bench-v1.toml").write_text(corpus_toml("bench-v1", synthetic, documents, "queries/bench-v1.jsonl", header))
    print(json.dumps({"documents": dict(Counter(document.category for document in documents)), "synthetic": synthetic.count, "curated_queries": len(queries)}, sort_keys=True))


if __name__ == "__main__":
    main()
```

The script lives outside `src/` (it is not shipped) and is not subject to the package's import layering; it imports only public modules.

- [ ] **Step 6: Run the curation and check the outputs**

Run: `uv run --locked python benchmarks/curate_bench_v1.py --cache .cache/bench`
Expected: prints a JSON summary such as `{"curated_queries": 150, "documents": {"book": 10, "code": 100, "image": 40, "native_pdf": 600, "scanned_pdf": 50, "text": 200}, "synthetic": 0}`. Lower public counts are acceptable; synthetic documents fill the rest.

Run: `uv run --locked python -c "from pathlib import Path; from tychora.evaluation.bench.corpus import load_corpus, all_queries; c = load_corpus('bench-v1', Path('benchmarks')); print(len(c.documents) + c.synthetic.count, len(all_queries(c)))"`
Expected: `1000 200`.

Run: `uv run --locked python -m tychora.interfaces.bench fetch --corpus bench-v1 --cache .cache/bench`
Expected: `0` (everything is already cached and verified).

- [ ] **Step 7: Ignore local results and add the scheduled workflow**

Append to `.gitignore`:

```gitignore
# Benchmark results (published to the bench-results branch); baselines are committed
bench-results/*
!bench-results/baseline/
```

```yaml
# .github/workflows/bench.yml
name: Benchmarks

on:
  schedule:
    - cron: "17 3 * * 1"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  offline:
    runs-on: ubuntu-22.04
    timeout-minutes: 300
    env:
      TYCHORA_BENCH_RUNNER_CLASS: github-ubuntu-22.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked stack
        run: uv sync --locked --all-extras --group dev
      - name: Ingest suite
        run: uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline --cache "$RUNNER_TEMP/bench"
      - name: Query suite
        run: uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite query --profile offline --cache "$RUNNER_TEMP/bench"
      - name: Scale suite
        run: uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite scale --profile offline --checkpoints 100,1000,10000 --max-wall-seconds 14400 --cache "$RUNNER_TEMP/bench"
      - name: Publish results
        if: always()
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "tychora-bench"
          git config user.email "tychora-bench@users.noreply.github.com"
          remote="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
          if git fetch "$remote" bench-results:bench-results; then
            git worktree add "$RUNNER_TEMP/results" bench-results
          else
            git worktree add --orphan -b bench-results "$RUNNER_TEMP/results"
          fi
          cp -R bench-results/. "$RUNNER_TEMP/results/"
          cd "$RUNNER_TEMP/results"
          git add .
          git commit -m "bench: offline results for ${GITHUB_SHA}" || exit 0
          git push "$remote" bench-results

  online:
    runs-on: ubuntu-22.04
    timeout-minutes: 600
    needs: offline
    env:
      TYCHORA_BENCH_RUNNER_CLASS: github-ubuntu-22.04
      VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked stack
        run: uv sync --locked --all-extras --group dev
      - name: Provision pinned public test assets
        run: python packaging/provision_ci_assets.py --cache "$RUNNER_TEMP/tychora-public-models" --workspace "$RUNNER_TEMP/tychora-asset-setup" --github-env "$GITHUB_ENV"
      - name: Fetch bench-v1
        run: uv run --locked python -m tychora.interfaces.bench fetch --corpus bench-v1 --cache "$RUNNER_TEMP/bench"
      - name: Online query suite
        run: uv run --locked python -m tychora.interfaces.bench run --corpus bench-v1 --suite query --profile online --cache "$RUNNER_TEMP/bench"
      - name: Online ingest suite
        run: uv run --locked python -m tychora.interfaces.bench run --corpus bench-v1 --suite ingest --profile online --cache "$RUNNER_TEMP/bench"
      - name: Publish results
        if: always()
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "tychora-bench"
          git config user.email "tychora-bench@users.noreply.github.com"
          remote="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
          git fetch "$remote" bench-results:bench-results
          git worktree add "$RUNNER_TEMP/results" bench-results
          cp -R bench-results/. "$RUNNER_TEMP/results/"
          cd "$RUNNER_TEMP/results"
          git add .
          git commit -m "bench: online results for ${GITHUB_SHA}" || exit 0
          git push "$remote" bench-results
```

The online job uses the provisioning step of `check.yml`, which sets `TYCHORA_MODEL_CACHE` for the output tokenizer. It needs the repository secret `VOYAGE_API_KEY`; without it the job fails at the first embedding with `PROVIDER_FAILURE` reason `provider.auth`, which is the intended signal.

- [ ] **Step 8: Document the benchmark**

Append to `docs/performance.md`:

```markdown
## Benchmark corpus and harness

`benchmarks/bench-small.toml` (100 seeded synthetic documents) needs no download.
`benchmarks/bench-v1.toml` pins about 1,000 public documents by URL, SHA-256 and
licence (UK legislation, CC BY arXiv papers, Project Gutenberg books, Python
PEPs, CPython modules, CC0 images), plus synthetic fill, and 200 queries with
stable gold labels in `benchmarks/queries/bench-v1.jsonl`. Regenerate it with
`benchmarks/curate_bench_v1.py`; never commit the downloaded documents.

    python -m tychora.interfaces.bench fetch --corpus bench-v1 --cache .cache/bench
    python -m tychora.interfaces.bench run --corpus bench-small --suite ingest|query|scale --profile offline

Each run writes `bench-results/<commit>/<suite>-<profile>.json` with the commit,
the machine fingerprint, the corpus digest, per-stage n, p50, p90, p95 and p99,
and a verdict against the budgets of the engine core design. Verdicts fail only
on the runner class named in `benchmarks/reference-machine.toml` (set
`TYCHORA_BENCH_RUNNER_CLASS`); elsewhere they are informational, and
`--compare <earlier result>` fails on a p95 regression above 20%. The weekly
`Benchmarks` workflow publishes results to the `bench-results` branch.
```

- [ ] **Step 9: Commit**

```bash
git add src/tychora/evaluation/bench/curation.py benchmarks/curate_bench_v1.py benchmarks/bench-v1.toml benchmarks/queries/bench-v1.jsonl .github/workflows/bench.yml .gitignore docs/performance.md tests/evaluation/bench/test_curation.py
git commit -m "feat: curate bench-v1 from public sources and schedule benchmark runs"
```

---

### Task 17: Retrieval baseline at `b91d808` (E6)

**Files:**
- Create: `benchmarks/evaluation_baseline.py`
- Create (by running the script at `b91d808`): `bench-results/baseline/evaluation-b91d808.json`
- Test: `tests/evaluation/test_retrieval_baseline.py`

**Interfaces:**
- Consumes: only the public API that exists at `b91d808` (`Tychora.open`, `kb.sources.add`, `kb.jobs.wait`, `kb.search`, `kb.evaluations.register_dataset`, `kb.evaluations.run`, `kb.evaluations.get`) and `tests/fixtures/evaluation/corpus.json`.
- Produces: `measure(workspace: Path, commit: str) -> dict` in the script; the committed baseline document `{"schema": "tychora.evaluation-baseline", "version": 1, "commit", "dataset", "variants": {"lexical": {metric: value}}, "unavailable": {"hybrid": reason, "reranked": reason}}`.

The executable synthetic corpus is the existing evaluation set that runs offline. Hybrid and reranked stages need a text family and a reranker, which need Voyage credentials; they are recorded as unavailable offline, and the online command is given below. The code gold set baseline (G22) is owned and recorded by code health.

- [ ] **Step 1: Write the baseline script**

```python
# benchmarks/evaluation_baseline.py
"""Record lexical retrieval metrics of the synthetic executable corpus.

Uses only public API available since b91d808, so one script measures the
baseline commit and every later commit:

    uv run --locked python benchmarks/evaluation_baseline.py --commit "$(git rev-parse HEAD)" --out OUT.json
"""

import argparse
import json
import tempfile
from hashlib import sha256
from pathlib import Path

from tychora import Tychora
from tychora.contracts.identity import canonical_bytes
from tychora.ports.execution import ExecutionContext

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "evaluation" / "corpus.json"
DATASET = "synthetic-executable-v1"
VARIANTS = {"lexical": {"channels": ["lexical"], "rerank": False}}
UNAVAILABLE = {
    "hybrid": "needs a text family: run with an online Voyage profile",
    "reranked": "needs a text family and a reranker: run with an online Voyage profile",
}


class Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def _ingest(kb, sources) -> None:
    for source in sources:
        metadata = {"title": source["title"], "publisher": "Synthetic fixture", "edition": "1"}
        if source.get("kind") == "standard@1":
            metadata["issuer"] = "Synthetic fixture"
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": source["source_key"],
                "collection_id": "default",
                "kind": source.get("kind", "manual@1"),
                "format": source.get("format", "text"),
                "text": "synthetic filler " * source.get("prefix_repeat", 0) + source["text"],
                "metadata": metadata,
            }
        )
        if kb.jobs.wait(handle.id, timeout=60).state != "succeeded":
            raise SystemExit(f"Ingestion failed for {source['source_key']}")


def _labels(kb, sources) -> dict:
    owner = kb._workspace.owner

    def read():
        return {
            source["source_key"]: owner.db.execute(
                "SELECT s.source_id,r.revision_id,u.unit_id FROM evidence_units u "
                "JOIN extractions e USING(extraction_id) JOIN revisions r USING(revision_id) "
                "JOIN sources s USING(source_id) WHERE s.source_key=? AND instr(u.text,?)>0",
                (source["source_key"], source["anchor"]),
            ).fetchall()
            for source in sources
        }

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


def _cases(sources, labels) -> list[dict]:
    cases = []
    for source in sources:
        if source.get("case", True) is False:
            continue
        source_id, revision_id, unit_id = labels[source["source_key"]][0]
        query = dict(source.get("query", {"question": source["anchor"], "modalities": ["lexical"]}))
        if source.get("restrict_to_revision"):
            query["revision_ids"] = [revision_id]
        cases.append(
            {
                "case_id": source["source_key"],
                "family_key": source["source_key"],
                "split": "test",
                "benchmark": "integrity",
                "label_provenance": "declared synthetic source anchor and canonical SQL identity",
                "query": query,
                "expected_source_ids": [source_id],
                "expected_revision_ids": [revision_id],
                "expected_unit_ids": [unit_id],
                "forbidden_source_ids": [
                    labels[other["source_key"]][0][0] for other in sources if other is not source
                ],
            }
        )
    return cases


def _write_gold(directory: Path, cases: list[dict]) -> None:
    directory.mkdir()
    payload = b"".join(canonical_bytes(case) + b"\n" for case in cases)
    (directory / "cases.jsonl").write_bytes(payload)
    (directory / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": DATASET,
                "name": "Declared synthetic lexical integrity",
                "version_sha": sha256(payload).hexdigest(),
                "case_count": len(cases),
            }
        )
    )


def measure(workspace: Path, commit: str) -> dict:
    """Ingest the synthetic corpus, evaluate each available variant, return metrics."""
    sources = json.loads(CORPUS.read_text())["sources"]
    with Tychora.open(workspace / "ws", token_counter=Counter()) as kb:
        _ingest(kb, sources)
        _write_gold(workspace / "gold", _cases(sources, _labels(kb, sources)))
        kb.evaluations.register_dataset(workspace / "gold")
        snapshot = kb.search({"question": "saffron", "modalities": ["lexical"]}).snapshot_id
        profile = kb._workspace.config.profile
        variants = {}
        for name, overrides in VARIANTS.items():
            handle = kb.evaluations.run(
                {
                    "dataset_id": DATASET,
                    "split": "test",
                    "snapshot_id": snapshot,
                    "offline": False,
                    "variants": [{"name": name, "profile": profile, "overrides": overrides}],
                }
            )
            report = kb.evaluations.get(handle.id)["report"]
            variants[name] = {item["name"]: item["value"] for item in report["aggregate"]["metrics"]}
    return {
        "schema": "tychora.evaluation-baseline",
        "version": 1,
        "commit": commit,
        "dataset": DATASET,
        "variants": variants,
        "unavailable": UNAVAILABLE,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        document = measure(Path(directory), args.commit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Measure the baseline at `b91d808`**

```bash
git worktree add ../tychora-b91d808 b91d808
cd ../tychora-b91d808
uv sync --locked --all-extras --group dev
uv run --locked python "$OLDPWD/benchmarks/evaluation_baseline.py" --commit "$(git rev-parse HEAD)" --out "$OLDPWD/bench-results/baseline/evaluation-b91d808.json"
cd "$OLDPWD"
git worktree remove ../tychora-b91d808
```

Expected: `bench-results/baseline/evaluation-b91d808.json` exists; `variants.lexical.unit_recall` is `1.0` and `forbidden_leakage` is `0.0` (the executable corpus's integrity expectations, `tests/evaluation/test_executable_corpus.py:58-69`).

With Voyage credentials, the online stages are measured the same way from an online workspace profile; add them to `VARIANTS` as `"hybrid": {"channels": ["lexical", "text"], "rerank": False}` and `"reranked": {"channels": ["lexical", "text"], "rerank": True}` in a one-off run and record the document as `evaluation-b91d808-online.json`. This is not required for the phase gate.

- [ ] **Step 3: Write the ratchet test against the baseline**

```python
# tests/evaluation/test_retrieval_baseline.py
"""Lexical retrieval metrics stay at or above the b91d808 baseline (E6)."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "bench-results" / "baseline" / "evaluation-b91d808.json"
LOWER_IS_BETTER = {"forbidden_leakage"}


def _script():
    spec = importlib.util.spec_from_file_location(
        "evaluation_baseline", ROOT / "benchmarks" / "evaluation_baseline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lexical_metrics_do_not_fall_below_the_baseline(tmp_path):
    baseline = json.loads(BASELINE.read_text())["variants"]["lexical"]
    current = _script().measure(tmp_path, "HEAD")["variants"]["lexical"]
    for name, expected in baseline.items():
        if expected is None:
            continue
        observed = current[name]
        assert observed is not None, name
        if name in LOWER_IS_BETTER:
            assert observed <= expected + 1e-9, name
        else:
            assert observed >= expected - 1e-9, name
```

- [ ] **Step 4: Run it**

Run: `uv run --locked pytest tests/evaluation/test_retrieval_baseline.py -v`
Expected: PASS (the current code measures the same values as `b91d808`).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/evaluation_baseline.py bench-results/baseline/evaluation-b91d808.json tests/evaluation/test_retrieval_baseline.py
git commit -m "test: record the b91d808 lexical retrieval baseline and guard it"
```

---

### Task 18: Phase gate and code-health ratchet

**Files:**
- Modify: `quality/baseline.json` (through `tools.health update` only)

**Interfaces:**
- Consumes: everything above.
- Produces: the phase-1 benchmark results and an updated baseline.

- [ ] **Step 1: Full offline suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 2: Lint, format, types**

Run: `uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked ty check src tests`
Expected: no findings.

- [ ] **Step 3: Phase-1 benchmark gate (index, "Benchmark gates per phase")**

Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline --cache .cache/bench`
Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite query --profile offline --cache .cache/bench`
Expected: both exit 0 and print the path of a result whose document has `schema`, `version`, `commit`, `suite`, `profile`, `corpus.digest`, `machine`, `retained_sha`, `stages` (with n, p50, p90, p95, p99, budget, verdict), `usage`, `assertions`. Attach both JSON files to the pull request; `bench-results/baseline/evaluation-b91d808.json` is committed (Task 17).

- [ ] **Step 4: Ratchet**

Run: `uv run --locked python -m tools.health check`
Expected: green, or "stale baseline" naming improved metrics. Expected improvements: G8 (raw SQL outside adapters) lower by the epoch and watermark sites moved in Task 6; G9 (Python duplication) not higher; G24 (reasonless integrity errors) lower by the sites classified in `migrations.py`, `connection.py`, `cas.py` and the new adapters; G12 (vulture) lower by one if `new_family_nonce` or `vector_record_id` remain whitelisted (they gain callers in phase 2, not here). The one expected local worsening is G6 on `interfaces/commands/manage.py` (+1, Task 10); justify it in the pull request description as the CLI lazy-import convention.

Run: `uv run --locked python -m tools.health update`
Then: `git diff quality/baseline.json` shows only improvements (plus the G6 entry hand-edited with its justification).

- [ ] **Step 5: Commit**

```bash
git add quality/baseline.json
git commit -m "chore: lock engine-core foundations improvements into the health baseline"
```

---

## Self-Review

**Spec coverage (phase-1 scope of the index):**

| Requirement | Task |
| --- | --- |
| §4.13 error reasons: registry, `ErrorEnvelope` v2, `TychoraError(reason=)`, HTTP status override, transports | 1, 2 |
| C5 busy owner (`owner.live`) | 3 |
| §4.13 repository ports, `UnitOfWork`, keyset `Cursor`, store-agnostic interface rules | 4, 5 |
| §4.13 duplication: one `ArtifactRef` row constructor, one watermark read, one epoch read | 5, 6 |
| §6 port-neutrality contract test | 4 |
| §5 format 2, `workspace.format_old` for workspaces and archives | 7 |
| §4.11 `rebuild workspace --from`, programme decision 11 kind map | 8, 9, 10 |
| §4.14 stable gold labels, failing ambiguous or missing labels with a reason | 11, 12 |
| §4.10 benchmark corpus (`bench-v1`, `bench-small`), harness, suites, output, reference runner, scheduled publication | 13, 14, 15, 16 |
| §4.12 budgets encoded and asserted only on the reference runner, 20% regression on shared runners | 13, 15 |
| E6 baseline at `b91d808` | 17 |
| §7 ratchet obligations of this phase | 6, 18 |

Not in this phase by design (index): `families` suite (phase 7), trace-backed stage durations (phase 5), usage totals (phase 5).

**Placeholder scan:** no "TBD", no "similar to", no step without its code or command. The one generated artifact without inline content is `benchmarks/bench-v1.toml` with its query file, produced by the committed script in Task 16 and checked by exact commands.

**Type consistency:** `TychoraError(code, message, *, retryable=None, reason=None, diagnostic_id=None)` is used identically in Tasks 1–12; `SqliteUnitOfWork.read()/write()` yield `SqliteTransaction` with `.workspace` and `.artifacts` (Tasks 5, 6, 7); `GoldLabel.anchor` types match between Tasks 11, 14 and 16; `BenchResult`, `StageStats`, `RunContext`, `BenchSource`, `SearchOutcome`, `Footprint` keep one definition each (Tasks 13, 15); `rebuild_workspace(old, destination, ctx, *, open_target, kind_map)` matches its callers in `api.py`.

**Review Focus coverage:** each of the five lines has its pinning test in the owning task: Task 2 (`test_pdf_analysis_child_keeps_reason`, `test_http_client_rebuilds_reason`), Task 7 (`test_format_one_workspace_is_refused_untouched`, `test_format_one_archive_import_is_refused`), Task 9 (`test_rebuild_refuses_non_empty_or_same_destination`, `test_rebuild_reports_skipped_items_and_leaves_old_unchanged`), Task 4 (`test_cursor_rejects_tampered_tokens`), Task 14 (`test_fetch_refuses_changed_bytes`).

**Deviations from the index to carry back:**
1. Entry point is `python -m tychora.interfaces.bench`, not `python -m tychora.evaluation.bench` (layering: evaluation may not import the public client).
2. `ArtifactRef.from_row` on the contract is the single constructor; `artifact_ref(row)` in `adapters/sqlite/repositories` is an alias, because `indexing` and `evaluation` may not import adapters.
3. `GoldLabelResolver` takes primitives (`units_by_native_ref`, `units_by_char_range`, `units_by_page`) because adapters may not import `tychora.evaluation`; `resolve_gold_binding` and `GoldBinding` are added beside `resolve_gold_label`.
4. `rebuild_workspace` gains keyword-only `open_target` and `kind_map`.
5. New reasons beyond spec §4.13: `label.missing`, `label.ambiguous`, `rebuild.destination_not_empty` (INVALID_REQUEST).
