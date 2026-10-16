# Code Health Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the 28 automated code-health gates, a measured baseline and the ratchet checker (`python -m tools.health`), fix what blocks them, and leave every later sub-project of the Tychora v2 programme a "equal or better" rule it cannot bypass.

**Architecture:** A small standard-library package `tools/health/` (outside `src/`, never shipped) runs each tool, reduces its output to `Metric` values, and compares them with `quality/baseline.json` under three rules: no global regression, no per-key regression, no stale baseline. Tool configuration lives in `pyproject.toml`, `.jscpd.json`, `dashboard/knip.json`, `dashboard/vite.config.ts` and `quality/`. Pull requests run fast gates with parallel tests and `sysmon` line coverage; a nightly workflow runs branch coverage, mutation testing, flaky detection, the code gold set and the architecture report.

**Tech Stack:** Python 3.12, uv 0.9.28, pytest 9.1.1 (+ pytest-xdist 3.8.0, pytest-cov 7.1.0, coverage 7.16.2, pytest-randomly 5.0.0), ruff 0.16.7, ty 0.0.80, import-linter 2.15, vulture 2.16, complexipy 8.0.1, radon 5.1.0, mutmut 3.8.0, pip-audit 2.10.1; Node 22 with jscpd 5.4.1, knip 6.41.0, madge 8.0.0, @vitest/coverage-v8 4.1.11, Prettier 3.8.5; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-15-code-health-design.md` (sub-project 0). Programme: `docs/superpowers/specs/2026-10-15-tychora-v2-program-design.md` (resolved questions 5 and 6 apply here). Read both before starting a task.

## Global Constraints

- Python `>=3.12,<3.13`; every Python command runs through `uv run --locked` (CI) or the locked environment.
- Tool pins, exactly: ruff 0.16.7, ty 0.0.80, pytest 9.1.1, pytest-asyncio 1.4.0, pytest-cov 7.1.0, coverage 7.16.2, pytest-randomly 5.0.0, pytest-xdist 3.8.0, import-linter 2.15, vulture 2.16, complexipy 8.0.1, radon 5.1.0 (group `dev`); pip-audit 2.10.1 (group `audit`); mutmut 3.8.0 (group `mutation`); jscpd 5.4.1, knip 6.41.0, madge 8.0.0, @vitest/coverage-v8 4.1.11, prettier 3.8.5 (dashboard devDependencies).
- Node is pinned to 22: `dashboard/.nvmrc` contains `22`; `dashboard/package.json` has `"engines": {"node": ">=22"}`. Node stays build-only; jscpd is installed once in `dashboard/` and invoked from the root.
- New-code thresholds: 50 statements (PLR0915), 5 arguments (PLR0913), 6 returns (PLR0911), cognitive complexity 15, 80 physical lines per function.
- Ratchet tolerances: coverage and mutation scores 0.1 percentage point; test runtime 15%.
- Advisory waivers expire at most 30 days after approval and are reviewed monthly (programme resolved question 5).
- Coverage runs as a report or nightly; fast gates run on every change (programme resolved question 6). Pull requests: `pytest -n auto` with `COVERAGE_CORE=sysmon` line coverage, target under 5 minutes. Nightly: branch coverage, mutation testing, three random seeds.
- `TYCHORA_TEST_TIMEOUT_FACTOR` scales the fixed waits in tests; default 1, CI coverage runs use 3.
- A test slower than 30 s locally carries `@pytest.mark.slow`.
- No pre-commit framework: `python -m tools.health check --fast` is the local entry point.
- `tools/` is excluded from the wheel and the sdist (both use explicit include lists; do not add `tools` to them).
- No runtime behaviour change except the `LocalSchemas` schema-check cache (Task 2). No stored format or public contract changes; adding `__init__.py` files does not change import paths.
- The public repository never contains private corpora; the code gold set's corpus is Tychora's own source at a pinned commit, fetched by the provisioning script, never by tests (the network guard in `tests/conftest.py` stays).
- Comments and docstrings state what the code does and why, in present tense; they never cite plan tasks, plan file names or review identifiers. This plan's own task numbers must not appear in code, tests, dashboard files or file names.
- Code snippets in this plan are not always in ruff's layout: run `uv run --locked ruff format` on the files a task creates or edits before checking. Every task ends with `uv run --locked ruff check .`, `uv run --locked ruff format --check .` and `uv run --locked ty check src tests tools` (drop `tools` before Task 12 creates it) passing, plus the task's own tests.

## Review Focus

1. **Qualification run without assets** — a `@pytest.mark.qualification` (or `release`) test whose declared asset variable is unset must fail with the variable name, never skip; otherwise the qualification workflow turns green with nothing qualified. Pinned in Task 4 (`test_qualification_fails_instead_of_skipping`).
2. **A schema the metaschema rejects, constructed twice** — the `LocalSchemas` cache must never remember a failure, so the second construction raises `INVALID_REQUEST` again; the cache must also stay bounded. Pinned in Task 2 (`test_schema_rejected_by_the_metaschema_is_rejected_every_time`, `test_schema_check_cache_is_bounded`).
3. **A report file missing because an earlier CI step failed** — the checker must fail naming `reports/<file>`, never read the absence as zero coverage or zero failures. Pinned in Task 14 (`test_missing_report_names_the_file`, `test_missing_core_module_is_an_error_not_a_zero`).
4. **`update` after renaming a long function versus adding a new one** — a rename (same total) is recorded; a new over-limit function is refused; a hand-edited looser baseline only passes when it equals the current value. Pinned in Task 12 (`test_update_accepts_a_rename_but_not_a_new_long_function`, `test_hand_edited_worse_value_passes_only_when_it_matches_the_current_value`).
5. **Advisory register drift** — an unlisted advisory, an expired waiver, and a waiver whose advisory is no longer reported (for example after the Chroma upgrade) must each fail G19, so the register only shrinks by review. Pinned in Task 14 (`test_advisory_register_lists_unlisted_expired_and_obsolete_entries`).

## Clarifications of the spec made by this plan

The spec is approved; these points resolve contradictions or gaps found while planning. Each is applied in the task named.

- **pytest-xdist**: spec §2 lists parallel execution as a non-goal, but §4.7 "Suite speed" (added later, with measurements) adopts `pytest-xdist`. The plan follows §4.7 (Task 6).
- **Slow tests**: §4.7 says slow tests stay "in the default selection" and also "run nightly only". The plan keeps `slow` inside the default marker expression (local runs and the S2 clean-machine job include them) and excludes it only from the pull-request `tests` job (`and not slow`); nightly runs everything (Tasks 6, 16, 20).
- **Layering exceptions**: §4.4 counts 61 direct violating edges. Two more exist (`clients.sync`/`clients.async_` import `interfaces.http.client`, siblings declared independent with `|`), and import-linter's chain checks need some edges listed in more than one contract. Measured on a copy: contract `layers` 34 entries, `adapters-implement-ports` 29, `storage-through-ports` 24, `pure-contracts` 3 — 90 entries for 63 distinct edges, all four contracts kept. G7's ratchet counts the 90 entries (Task 10).
- **Raw SQL count**: the AST count of `.execute`/`.executemany`/`.executescript` outside adapters is 240 (spec: 237 by text search). The baseline is whatever the CI measurement records (Task 16).
- **Public API docstrings**: §7 says 81 missing; ruff's D1 rules see only 30 there because most public methods sit in underscore-named resource classes (`_Sources.add` is `kb.sources.add`). G14's hard part therefore uses an AST check that counts those methods: 65 public names without a docstring today (Tasks 9, 13).
- **Plan references**: G15 counts every matching line in tracked files under `src/`, `tests/`, `dashboard/` (excluding `dashboard/package-lock.json`) plus file names everywhere except `docs/superpowers/`; today 125 hits (spec: 74 lines + 36 names, measured with a narrower scope). Target 0 either way (Task 7).
- **jscpd allow-lists**: one fingerprint baseline per scope (`quality/jscpd/python.json`, `typescript.json`, `tests.json`) instead of one `quality/jscpd-baseline.json`, because each jscpd run has one baseline file (Task 14).
- **mutmut scope**: mutmut 3.8.0 supports `only_mutate`; the plan lists the eight core modules there instead of generating a `do_not_mutate` list of every other module (Task 17). Verified on a copy: with the new identity tests, `contracts/identity.py` goes from 36 survivors to the 3 equivalent mutants documented in `quality/equivalent-mutants.toml`.
- **Command blocks in docs**: §4.8 asks that the command blocks of `HANDOFF.md` and `docs/contributing.md` "run". Executing `uv sync --all-extras` or the asset download inside the test suite would need network and minutes; the plan's test checks each command statically (every referenced path exists, every `uv run --locked <tool>` names an installed console script, every `-m` expression uses registered markers) and the documentation audit runs them by hand (Task 21).
- **Code gold set corpus** (R2): the programme defers publishing the earlier private project, so the plan uses the spec's fallback: Tychora's own `src/tychora` at commit `700df656fb391aa5eaa41a75ce27eed432be442c` (already on `origin/main`), 33 hand-written queries with the earlier project's schema, converted by `tools/health/convert_gold.py`. The corpus is exported from Git, whose tree id (`34fa1cac9248f208c5dfaa34347674a4f6d6d26b`) pins the bytes (Task 19).
- **Ruff defaults**: ruff 0.16.7's default rule set is broad (hundreds of rules, not only E4/E7/E9/F); the repository passes it today, and new code in `tools/` must too.

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `.github/workflows/qualification.yml`, `docs.yml`, `dashboard.yml` | action pinning, credentials, Node version file | 1 |
| `.gitignore` | ignore `.worktrees/`, `.superpowers/`, `mutants/`, `dashboard/coverage/` | 1 |
| `dashboard/.nvmrc`, `dashboard/package.json` | Node pin, engines, scripts, dev tools | 1, 8 |
| `src/tychora/configuration/schemas.py` | bounded metaschema-check cache | 2 |
| `tests/configuration/test_registry.py` | cache tests | 2 |
| `src/tychora/{adapters,adapters/docling,adapters/sqlite,adapters/vision,adapters/voyage,application,configuration,formatting,ingestion,interfaces,ports}/__init__.py` | explicit packages | 3 |
| `tests/test_packages.py` | every Python directory is a package | 3 |
| `tests/helpers/assets.py`, `tests/conftest.py`, `tests/test_asset_markers.py` | `assets` marker, `asset_path` | 4 |
| `tests/fixtures/docling.py` | shared Docling fixtures | 5 |
| `tests/helpers/timing.py` | `scaled()` wait budgets | 6 |
| `pyproject.toml` | dependency pins, pytest, coverage, importlinter, vulture, mutmut | 6, 10, 11, 17 |
| `quality/coverage-line.toml` | pull-request line-coverage configuration | 6 |
| `dashboard/knip.json`, `dashboard/src/api/types.ts`, `dashboard/vite.config.ts` | knip, cycle fix, coverage thresholds | 8 |
| `quality/vulture_whitelist.py` | reviewed dead-code allow-list | 11 |
| `tools/__init__.py`, `tools/health/*.py`, `tools/health/tests/*.py`, `tools/health/isolation.txt` | checker | 12–15, 18 |
| `.jscpd.json`, `quality/jscpd/*.json` | duplication configuration and fingerprints | 14, 16 |
| `quality/advisories.toml` | advisory waiver register | 14 |
| `quality/baseline.json` | ratchet baseline (measured on CI) | 16 |
| `.github/workflows/check.yml` | PR tests matrix, clean machine, isolation, health | 16 |
| `quality/equivalent-mutants.toml`, `tests/contracts/test_identity.py` | mutation equivalents, identity tests | 17 |
| `tools/health/architecture.py`, `docs/quality/` | architecture report and review template | 18 |
| `tools/health/convert_gold.py`, `tests/fixtures/evaluation/code-gold/`, `tests/helpers/code_gold.py`, `tests/evaluation/test_code_gold.py`, `packaging/provision_ci_assets.py` | code gold set | 19 |
| `.github/workflows/health-nightly.yml` | nightly gates | 20 |
| `docs/contributing.md`, `docs/documentation.md`, `docs/architecture.md`, `HANDOFF.md`, `tests/docs/test_examples.py` | comment rule, audit, dependency direction | 9, 10, 21 |

---

### Task 1: Repository and CI hygiene

Small, independent infrastructure fixes from spec §4.10.

**Files:**
- Modify: `.github/workflows/qualification.yml:24-25`
- Modify: `.github/workflows/docs.yml:22`
- Modify: `.github/workflows/dashboard.yml:19-22`
- Modify: `.gitignore` (last line)
- Create: `dashboard/.nvmrc`
- Modify: `dashboard/package.json`

**Interfaces:**
- Produces: npm script `format:check` (`prettier --check src e2e`), used by the checker's G2 (Task 14) and CI.

- [ ] **Step 1: Pin the qualification actions to the SHAs the other workflows use**

In `.github/workflows/qualification.yml` replace

```yaml
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
```

with

```yaml
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
```

- [ ] **Step 2: Stop the docs workflow from persisting the token**

In `.github/workflows/docs.yml` replace

```yaml
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
```

with

```yaml
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
```

- [ ] **Step 3: Pin Node 22 for the dashboard**

Create `dashboard/.nvmrc`:

```
22
```

In `dashboard/package.json` add after `"type": "module",`:

```json
  "engines": {
    "node": ">=22"
  },
```

and add to `"scripts"` (after `"typecheck"`):

```json
    "format:check": "prettier --check src e2e",
```

In `.github/workflows/dashboard.yml` replace `node-version: "22"` with `node-version-file: dashboard/.nvmrc`.

- [ ] **Step 4: Ignore local worktrees, superpowers state and generated reports**

In `.gitignore` replace the last line `.superpowers/verification/` with:

```
.superpowers/
.worktrees/
mutants/
dashboard/coverage/
```

(`reports/` is already ignored.)

- [ ] **Step 5: Verify**

Run:

```bash
git check-ignore -v .worktrees/x .superpowers/x mutants/x dashboard/coverage/x
npm --prefix dashboard ci --ignore-scripts
npm --prefix dashboard run format:check
grep -n "uses:" .github/workflows/qualification.yml
```

Expected: four `.gitignore` matches; Prettier prints "All matched files use Prettier code style!"; qualification uses only SHA-pinned actions.

- [ ] **Step 6: Commit**

```bash
git add .gitignore .github/workflows/qualification.yml .github/workflows/docs.yml .github/workflows/dashboard.yml dashboard/.nvmrc dashboard/package.json
git commit -m "ci: pin qualification actions, pin Node 22 and ignore local state"
```

---

### Task 2: Cache Draft 2020-12 metaschema checks in `LocalSchemas`

Product change (spec §4.7 "Fixture and product hot spots"): every `LocalSchemas` construction re-runs `Draft202012Validator.check_schema` over about 22 schemas; it was 55% of a workspace-open-plus-four-adds run. Cache successful checks by the canonical digest of the schema content, bounded, thread-safe, never caching failures.

**Files:**
- Modify: `src/tychora/configuration/schemas.py:1-12` (imports), `:47` (after `_FORMATS`), `:63` (new helper before `_pointer`), `:153` (call site)
- Test: `tests/configuration/test_registry.py` (append)

**Interfaces:**
- Produces: private module state `tychora.configuration.schemas._checked_schemas: OrderedDict[str, None]`, `_SCHEMA_CHECK_CACHE_SIZE: int = 4096`, `_check_schema_once(document) -> None`. Behaviour of `LocalSchemas(...)` is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/configuration/test_registry.py`:

```python
def _counting_check_schema(monkeypatch):
    from tychora.configuration import schemas as module

    module._checked_schemas.clear()
    calls: list[str] = []
    original = module.Draft202012Validator.check_schema

    def counting(schema, *args, **kwargs):
        calls.append(schema["$id"])
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(module.Draft202012Validator, "check_schema", counting)
    return module, calls


def test_schema_metaschema_check_runs_once_per_schema_content(monkeypatch):
    _, calls = _counting_check_schema(monkeypatch)
    schema = {"$id": "cached@1", "type": "integer", "minimum": 1}
    first = LocalSchemas({"cached@1": schema}, {"cached@1": {}})
    second = LocalSchemas({"cached@1": dict(schema)}, {"cached@1": {}})
    assert calls == ["cached@1"]
    assert second.validate("cached@1", 2) == first.validate("cached@1", 2) == 2
    changed = {"$id": "cached@1", "type": "integer", "minimum": 2}
    LocalSchemas({"cached@1": changed}, {"cached@1": {}})
    assert calls == ["cached@1", "cached@1"]


def test_schema_rejected_by_the_metaschema_is_rejected_every_time(monkeypatch):
    module, calls = _counting_check_schema(monkeypatch)
    invalid = {"$id": "invalid@1", "type": "not-a-json-type"}
    for _ in range(2):
        with pytest.raises(TychoraError) as err:
            LocalSchemas({"invalid@1": invalid}, {"invalid@1": {}})
        assert err.value.code == "INVALID_REQUEST"
    assert calls == ["invalid@1", "invalid@1"]
    assert not module._checked_schemas


def test_schema_check_cache_is_bounded(monkeypatch):
    module, calls = _counting_check_schema(monkeypatch)
    monkeypatch.setattr(module, "_SCHEMA_CHECK_CACHE_SIZE", 2)
    for minimum in (1, 2, 3):
        schema = {"$id": "bounded@1", "type": "integer", "minimum": minimum}
        LocalSchemas({"bounded@1": schema}, {"bounded@1": {}})
    assert len(module._checked_schemas) == 2
    LocalSchemas(
        {"bounded@1": {"$id": "bounded@1", "type": "integer", "minimum": 1}},
        {"bounded@1": {}},
    )
    assert len(calls) == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/configuration/test_registry.py -q -k "schema_metaschema or rejected_by or bounded"`
Expected: 3 FAILED with `AttributeError: module 'tychora.configuration.schemas' has no attribute '_checked_schemas'`.

Before changing the code, also record the reference time used in Step 5:

```bash
uv run --locked pytest tests/evaluation tests/application tests/transports -q -p no:randomly | tail -1
```

- [ ] **Step 3: Implement the cache**

In `src/tychora/configuration/schemas.py`, change the imports

```python
import json
import re
from collections.abc import Mapping
```

to

```python
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
```

After the line `_FORMATS = FormatChecker(formats=["date"])` add:

```python
# Metaschema checks dominate workspace open; a schema with identical canonical
# content is checked once per process. Failures are never remembered.
_SCHEMA_CHECK_CACHE_SIZE = 4096
_checked_schemas: OrderedDict[str, None] = OrderedDict()
_checked_schemas_lock = threading.Lock()
```

Immediately before `def _pointer(document: Any, pointer: str) -> Any:` add:

```python
def _check_schema_once(document: Any) -> None:
    """Check a schema against the Draft 2020-12 metaschema once per exact content."""
    key = digest("checked-schema-v1", document)
    with _checked_schemas_lock:
        if key in _checked_schemas:
            _checked_schemas.move_to_end(key)
            return
    Draft202012Validator.check_schema(document)
    with _checked_schemas_lock:
        _checked_schemas[key] = None
        while len(_checked_schemas) > _SCHEMA_CHECK_CACHE_SIZE:
            _checked_schemas.popitem(last=False)
```

In `LocalSchemas.__init__` replace

```python
                walk(document, key, "", ())
                Draft202012Validator.check_schema(document)
```

with

```python
                walk(document, key, "", ())
                _check_schema_once(document)
```

(The check still runs on the detached, not-yet-rewritten document, exactly where it ran before; `digest` is already imported from `tychora.contracts.identity`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/configuration -q`
Expected: all pass (112 in `test_registry.py` on the reference copy).

- [ ] **Step 5: Measure the speed-up on the evaluation, application and HTTP tests**

Run the timing command again (the reference was taken in Step 2):

```bash
uv run --locked pytest tests/evaluation tests/application tests/transports -q -p no:randomly | tail -1
```

Expected: the second run is markedly faster (the spec measured −29%). Record both times in the commit message body.

- [ ] **Step 6: Commit**

```bash
git add src/tychora/configuration/schemas.py tests/configuration/test_registry.py
git commit -m "perf: check each local schema against the metaschema once per process"
```

---

### Task 3: Make the eleven namespace packages explicit

import-linter (grimp) cannot analyse implicit namespace packages. Spec §4.4 prerequisite.

**Files:**
- Create: `src/tychora/adapters/__init__.py`, `src/tychora/adapters/docling/__init__.py`, `src/tychora/adapters/sqlite/__init__.py`, `src/tychora/adapters/vision/__init__.py`, `src/tychora/adapters/voyage/__init__.py`, `src/tychora/application/__init__.py`, `src/tychora/configuration/__init__.py`, `src/tychora/formatting/__init__.py`, `src/tychora/ingestion/__init__.py`, `src/tychora/interfaces/__init__.py`, `src/tychora/ports/__init__.py`
- Test: `tests/test_packages.py`

**Interfaces:**
- Produces: importable packages `tychora.adapters`, `tychora.application`, ... (same import paths as today).

- [ ] **Step 1: Write the failing test**

Create `tests/test_packages.py`:

```python
"""Every directory of Python modules under src/tychora is an explicit package."""

from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "tychora"


def test_every_python_directory_has_an_init_module():
    missing = sorted(
        directory.relative_to(SOURCE).as_posix()
        for directory in [SOURCE, *SOURCE.rglob("*")]
        if directory.is_dir()
        and directory.name != "__pycache__"
        and any(directory.glob("*.py"))
        and not (directory / "__init__.py").exists()
    )
    assert missing == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --locked pytest tests/test_packages.py -q`
Expected: FAIL listing `adapters`, `adapters/docling`, `adapters/sqlite`, `adapters/vision`, `adapters/voyage`, `application`, `configuration`, `formatting`, `ingestion`, `interfaces`, `ports`.

- [ ] **Step 3: Add the package modules**

Each file holds only its docstring:

| File | Content |
| --- | --- |
| `src/tychora/adapters/__init__.py` | `"""Concrete implementations of the ports: storage, parsers, providers and indexes."""` |
| `src/tychora/adapters/docling/__init__.py` | `"""Docling document parsing, OCR evidence and PDF batching."""` |
| `src/tychora/adapters/sqlite/__init__.py` | `"""SQLite canonical store: corpus, publication, eligibility, jobs and history."""` |
| `src/tychora/adapters/vision/__init__.py` | `"""Local OCR and Anthropic picture-description providers."""` |
| `src/tychora/adapters/voyage/__init__.py` | `"""Voyage embedding and reranking requests, responses and validation."""` |
| `src/tychora/application/__init__.py` | `"""Application services and the workspace owner, the composition root."""` |
| `src/tychora/configuration/__init__.py` | `"""Profiles, kind registries, local schemas and resolved workspace configuration."""` |
| `src/tychora/formatting/__init__.py` | `"""Response rendering and output token budgets."""` |
| `src/tychora/ingestion/__init__.py` | `"""Source acquisition, normalization and document-part extraction."""` |
| `src/tychora/interfaces/__init__.py` | `"""Transports: command line, HTTP and MCP."""` |
| `src/tychora/ports/__init__.py` | `"""Protocols that application services depend on and adapters implement."""` |

- [ ] **Step 4: Run the test, the import test and a wheel build**

```bash
uv run --locked pytest tests/test_packages.py tests/test_import.py -q
uv build --wheel -o "$TMPDIR/tychora-wheel-check" && unzip -l "$TMPDIR"/tychora-wheel-check/*.whl | grep -E "tychora/(adapters|ports|application)/__init__.py"
```

Expected: tests pass (`tests/test_import.py::test_console_command_displays_help_without_creating_state` still needs `tychora` on `PATH`, which `uv run` provides); the wheel lists the three `__init__.py` files.

- [ ] **Step 5: Commit**

```bash
git add src/tychora tests/test_packages.py
git commit -m "refactor: make the eleven namespace packages explicit"
```

---

### Task 4: The `assets` marker and `asset_path`

Spec §4.7 "Asset markers". A test declares which `TYCHORA_TEST_*` variables it needs; a hook skips it with the variable names when any is unset, except qualification and release tests, which fail.

**Files:**
- Create: `tests/helpers/assets.py`
- Modify: `tests/conftest.py`
- Modify: `pyproject.toml` (`[tool.pytest.ini_options] markers`)
- Test: `tests/test_asset_markers.py`

**Interfaces:**
- Produces: marker `@pytest.mark.assets(*names)`; `tests.helpers.assets.asset_path(name: str) -> Path` (raises `RuntimeError` unless the running test declared `name`); `tests.helpers.assets.ASSET_VARIABLES: frozenset[str]` (includes `TYCHORA_TEST_CODE_CORPUS` used in Task 19); `check_item(item)`, `end_test()` called by `tests/conftest.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_asset_markers.py`:

```python
"""The assets marker skips ordinary tests and fails qualification tests without assets."""

import pytest

from tests.helpers import assets

pytest_plugins = ["pytester"]

CONFTEST = """
import pytest
from tests.helpers import assets

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    assets.check_item(item)

def pytest_runtest_teardown(item):
    assets.end_test()
"""

TESTS = """
import pytest
from tests.helpers.assets import asset_path

@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_reads_cache():
    assert asset_path("TYCHORA_TEST_MODEL_CACHE").name == "cache"

@pytest.mark.qualification
@pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")
def test_qualifies_docling():
    pass

def test_undeclared_read():
    asset_path("TYCHORA_TEST_MODEL_CACHE")
"""


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(CONFTEST)
    pytester.makeini(
        "[pytest]\nmarkers =\n    assets(*names): provisioned test assets\n"
        "    qualification: real local adapters\n"
    )
    pytester.makepyfile(test_assets=TESTS)
    return pytester


def test_missing_asset_skips_with_the_variable_name(suite, monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_MODEL_CACHE", raising=False)
    monkeypatch.delenv("TYCHORA_TEST_DOCLING_MODELS", raising=False)
    result = suite.runpytest("-rs", "-p", "no:randomly")
    result.assert_outcomes(skipped=1, failed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*requires TYCHORA_TEST_MODEL_CACHE (provision with packaging/provision_ci_assets.py)*"
        ]
    )


def test_qualification_fails_instead_of_skipping(suite, monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_DOCLING_MODELS", raising=False)
    result = suite.runpytest("-p", "no:randomly", "-k", "qualifies")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*requires TYCHORA_TEST_DOCLING_MODELS*"])


def test_declared_asset_is_readable_and_undeclared_read_is_refused(
    suite, monkeypatch, tmp_path
):
    monkeypatch.setenv("TYCHORA_TEST_MODEL_CACHE", str(tmp_path / "cache"))
    result = suite.runpytest("-p", "no:randomly", "-k", "reads_cache or undeclared")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        ["*needs @pytest.mark.assets(*TYCHORA_TEST_MODEL_CACHE*) on the test*"]
    )


def test_unknown_asset_variable_is_a_usage_error(pytester: pytest.Pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makeini("[pytest]\nmarkers =\n    assets(*names): provisioned test assets\n")
    pytester.makepyfile(
        "import pytest\n@pytest.mark.assets('TYCHORA_TEST_TYPO')\ndef test_x():\n    pass\n"
    )
    result = pytester.runpytest("-p", "no:randomly")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*unknown asset variables*TYCHORA_TEST_TYPO*"])


def test_registered_variables_cover_every_provisioned_asset():
    assert "TYCHORA_TEST_CODE_CORPUS" in assets.ASSET_VARIABLES
```

(`-p no:randomly` only blocks a plugin, so it is harmless before pytest-randomly is installed in Task 6.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --locked pytest tests/test_asset_markers.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'tests.helpers.assets'`.

- [ ] **Step 3: Implement the helper**

Create `tests/helpers/assets.py`:

```python
"""Provisioned test assets: declared with @pytest.mark.assets, read with asset_path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROVISION_HINT = "provision with packaging/provision_ci_assets.py"
ASSET_VARIABLES = frozenset(
    {
        "TYCHORA_TEST_MODEL_CACHE",
        "TYCHORA_TEST_DOCLING_MODELS",
        "TYCHORA_TEST_TOKENIZER_ASSETS",
        "TYCHORA_TEST_OCR_MODELS",
        "TYCHORA_TEST_CODE",
        "TYCHORA_TEST_CODE_CORPUS",
        "TYCHORA_TEST_PDF_RESPONSES",
    }
)
_declared: frozenset[str] = frozenset()


def declared_assets(item: pytest.Item) -> frozenset[str]:
    """Every variable named by the assets markers on a test, its class and module."""
    names: set[str] = set()
    for marker in item.iter_markers("assets"):
        names.update(marker.args)
    unknown = names - ASSET_VARIABLES
    if unknown:
        raise pytest.UsageError(
            f"unknown asset variables {sorted(unknown)} on {item.nodeid}"
        )
    return frozenset(names)


def missing_assets(item: pytest.Item) -> list[str]:
    """Declared variables that are unset or empty in this environment."""
    return sorted(name for name in declared_assets(item) if not os.environ.get(name))


def check_item(item: pytest.Item) -> None:
    """Before fixtures run: remember the declared assets, then skip or fail if any is missing.

    Ordinary tests skip with the variable names. Qualification and release tests fail,
    because a missing verified bundle is an unavailable qualification, not a pass.
    """
    global _declared
    _declared = declared_assets(item)
    missing = missing_assets(item)
    if not missing:
        return
    message = f"requires {', '.join(missing)} ({PROVISION_HINT})"
    if item.get_closest_marker("qualification") or item.get_closest_marker("release"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def end_test() -> None:
    """Called by tests/conftest.py after teardown."""
    global _declared
    _declared = frozenset()


def asset_path(name: str) -> Path:
    """Path of a provisioned asset that the running test declared with @pytest.mark.assets."""
    if name not in _declared:
        raise RuntimeError(
            f"asset_path({name!r}) needs @pytest.mark.assets({name!r}) on the test"
        )
    return Path(os.environ[name])
```

- [ ] **Step 4: Register the hook and the marker**

In `tests/conftest.py` replace the header

```python
import socket

import pytest
```

with

```python
import socket

import pytest

from tests.helpers import assets


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip (or, for qualification and release, fail) when declared assets are missing."""
    assets.check_item(item)


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Forget the finished test's declared assets."""
    assets.end_test()
```

In `pyproject.toml` replace the `markers = [...]` line of `[tool.pytest.ini_options]` with:

```toml
markers = [
  "qualification: real local adapters",
  "live: explicitly authorized remote synthetic inputs",
  "release: installed artifact",
  "assets(*names): TYCHORA_TEST_* variables the test needs; skipped when unset",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/test_asset_markers.py tests/contracts -q`
Expected: 5 passed in `test_asset_markers.py`; `tests/contracts` unchanged.

- [ ] **Step 6: Commit**

```bash
git add tests/helpers/assets.py tests/conftest.py tests/test_asset_markers.py pyproject.toml
git commit -m "test: declare provisioned assets with a marker that skips with their names"
```

---

### Task 5: Default suite green on a clean machine (S2)

Spec §4.7 "Shared Docling fixtures" and "Environment assumptions"; success criterion S2. Before: 46 failed, 57 errors without assets.

**Files:**
- Create: `tests/fixtures/docling.py` (moved fixtures)
- Modify: `tests/qualification/test_docling.py`, `tests/qualification/test_docling_records.py`, `tests/ingestion/test_docling_mapping.py`, `tests/ingestion/test_document_records.py`
- Modify: `tests/conftest.py` (`pytest_plugins`)
- Modify (asset sites): `tests/api/test_configured_runtime.py`, `tests/api/test_public_async.py`, `tests/api/test_public_vision.py`, `tests/formatting/test_response_budget.py`, `tests/ingestion/test_public_vision_selection.py`, `tests/ingestion/test_tables.py`, `tests/retrieval/test_query.py`, `tests/transports/test_mcp_stdio.py`, `tests/docs/test_examples.py`, `tests/qualification/test_rich_document_public.py`, `tests/qualification/test_visual_live.py`, `tests/qualification/test_voyage_live.py`
- Modify: `tests/test_import.py:21-30`, `tests/transports/test_dashboard_distribution.py:10`

**Interfaces:**
- Consumes: `asset_path`, marker `assets` (Task 4).
- Produces: fixtures `document_kind`, `execution`, `rich_capture`, `docling_parser`, `document_record_case` available to every test through `pytest_plugins = ["tests.fixtures.docling"]`.

- [ ] **Step 1: Record the failing clean-machine baseline**

Run (the `uv run` keeps the console script on `PATH`; `env -i` removes every `TYCHORA_TEST_*` variable):

```bash
env -i PATH="$PATH" HOME="$HOME" uv run --locked pytest tests/ingestion/test_document_records.py -q | tail -1
```

Expected: the command reports errors (`Set TYCHORA_TEST_DOCLING_MODELS ...` raised from the qualification fixture).

- [ ] **Step 2: Move the shared Docling fixtures**

Save this one-off script outside the repository (for example `$TMPDIR/move_docling_fixtures.py`) and run it from the repository root with `uv run --locked python $TMPDIR/move_docling_fixtures.py`. It moves the fixtures verbatim, replaces their `pytest.fail` reads with `asset_path`, and marks the modules that need the bundle.

```python
"""One-off: move the shared Docling fixtures out of the qualification modules."""

from pathlib import Path

QUAL = Path("tests/qualification/test_docling.py")
RECORDS = Path("tests/qualification/test_docling_records.py")
TARGET = Path("tests/fixtures/docling.py")

HEADER = '''"""Docling workspace fixtures shared by unit and qualification tests.

Registered for every test through `pytest_plugins` in tests/conftest.py. Fixtures
that parse with the real Docling bundle read it through asset_path, so tests using
them must carry @pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS").
"""

import json
import shutil
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.fixtures.documents.create_record_fixtures import (
    create_record_document,
    record_content,
)
from tests.helpers.assets import asset_path
from tychora.adapters.docling.parser import DoclingParser
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.extraction import DocumentRegistration
from tychora.contracts.requests import RequestScope
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.normalize import normalize
from tychora.ports.execution import ExecutionContext

'''
MARK = 'pytestmark = pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")\n\n\n'


def block(path: Path, start: str, end: str) -> tuple[str, str]:
    """Return (moved text, remaining file) for the lines from start up to end."""
    text = path.read_text()
    first = text.index(start)
    last = text.index(end, first)
    return text[first:last], text[:first] + text[last:]


def mark_module(text: str) -> str:
    """Insert the module-wide assets marker before the first decorator or definition."""
    lines = text.splitlines(keepends=True)
    first = next(i for i, line in enumerate(lines) if line.startswith(("@", "def ", "class ")))
    return "".join(lines[:first]) + MARK + "".join(lines[first:])


qual_block, qual_rest = block(
    QUAL,
    '@pytest.fixture(params=["manual@1", "document@1"])',
    "@pytest.mark.qualification\ndef test_real_docling_retains",
)
records_block, records_rest = block(
    RECORDS,
    '@pytest.fixture(scope="module")\ndef document_record_case',
    '@pytest.mark.qualification\n@pytest.mark.parametrize("format", ["pdf", "docx"])\n'
    "def test_standard_document_records",
)
qual_block = qual_block.replace(
    '''    bundle = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    if not bundle:
        pytest.fail(
            "Set TYCHORA_TEST_DOCLING_MODELS to an explicitly provisioned, checksum-verified Docling model bundle; ingestion never downloads models."
        )
    return DoclingParser(execution.owner, execution.registration, Path(bundle))''',
    '''    bundle = asset_path("TYCHORA_TEST_DOCLING_MODELS")
    return DoclingParser(execution.owner, execution.registration, bundle)''',
)
records_block = records_block.replace(
    '''                model_path = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
                if not model_path:
                    pytest.fail(
                        "Set TYCHORA_TEST_DOCLING_MODELS to an explicitly provisioned checksum-verified Docling model bundle; ingestion never downloads models."
                    )
                parsed = DoclingParser(owner, registration, Path(model_path)).extract(''',
    '''                model_path = asset_path("TYCHORA_TEST_DOCLING_MODELS")
                parsed = DoclingParser(owner, registration, model_path).extract(''',
)
assert "os.environ" not in qual_block + records_block
TARGET.write_text(HEADER + "\n" + qual_block + records_block.rstrip() + "\n")
QUAL.write_text(mark_module(qual_rest))
RECORDS.write_text(mark_module(records_rest))

mapping = Path("tests/ingestion/test_docling_mapping.py")
text = mapping.read_text()
text = text.replace(
    "\nfrom tests.qualification import test_docling as qualification\n", "\n"
).replace(
    "execution = qualification.execution\nrich_capture = qualification.rich_capture\n", ""
)
mapping.write_text(text)

records = Path("tests/ingestion/test_document_records.py")
text = records.read_text()
text = text.replace("from tests.qualification import test_docling_records as qualification\n", "")
text = text.replace("document_record_case = qualification.document_record_case\n", "")
records.write_text(mark_module(text))
```

Then remove the imports the moves left unused and format:

```bash
uv run --locked ruff check --fix --select F401,I001 tests/qualification/test_docling.py tests/qualification/test_docling_records.py tests/ingestion/test_docling_mapping.py tests/ingestion/test_document_records.py tests/fixtures/docling.py
uv run --locked ruff format tests/qualification tests/ingestion/test_docling_mapping.py tests/ingestion/test_document_records.py tests/fixtures/docling.py
```

`tests/ingestion/test_docling_mapping.py` keeps its own `document_kind` fixture (it overrides the shared one with `"manual@1"`) and needs no marker: its tests use `execution` and `rich_capture`, which never read the model bundle.

- [ ] **Step 3: Register the fixtures**

In `tests/conftest.py`, after `from tests.helpers import assets`, add:

```python

pytest_plugins = ["tests.fixtures.docling"]
```

- [ ] **Step 4: Convert the direct asset reads**

Apply these two patterns. Import `from tests.helpers.assets import asset_path` at the top of each modified module.

Pattern A — direct index (`os.environ["TYCHORA_TEST_X"]`). Mark the test and read through `asset_path`:

```python
# before (tests/api/test_public_vision.py:43)
    cache = os.environ["TYCHORA_TEST_MODEL_CACHE"]
# after
    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
```

and decorate the enclosing test with `@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")`. Where the value is wrapped, drop the wrapper: `Path(os.environ["TYCHORA_TEST_MODEL_CACHE"])` becomes `asset_path("TYCHORA_TEST_MODEL_CACHE")`.

Sites (the 18 of spec §3.7): `tests/api/test_configured_runtime.py:454`; `tests/api/test_public_async.py:145`; `tests/api/test_public_vision.py:43, 431, 553, 855`; `tests/formatting/test_response_budget.py:26` (inside the helper `counter()`: mark every test that calls `counter()` — lines 86, 101, 105, 132, 230, 272, 315 — or, if all 13 tests of the module use it, add `pytestmark = pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")` after the imports); `tests/ingestion/test_public_vision_selection.py:97` (`TYCHORA_TEST_DOCLING_MODELS`); `tests/ingestion/test_tables.py:854` (`TYCHORA_TEST_DOCLING_MODELS`); `tests/retrieval/test_query.py:404, 573, 704, 903, 1051`; `tests/transports/test_mcp_stdio.py:39`; `tests/qualification/test_rich_document_public.py:27`; `tests/qualification/test_visual_live.py:67`; `tests/qualification/test_voyage_live.py:59`.

Pattern B — read with `.get` then `assert ... is not None` or `pytest.fail`:

```python
# before (tests/api/test_configured_runtime.py:337-338, same at 606, 716, 867)
    cache = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    assert cache is not None
# after
    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
```

plus `@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")` on the test. In `tests/docs/test_examples.py` replace the first five lines of `_bundle`

```python
    supplied = os.environ.get("TYCHORA_TEST_TOKENIZER_ASSETS")
    if not supplied:
        pytest.fail(
            "Set TYCHORA_TEST_TOKENIZER_ASSETS to the provisioned tokenizer fixture directory"
        )
    source = Path(supplied) / "cl100k_base.tiktoken"
```

with

```python
    source = asset_path("TYCHORA_TEST_TOKENIZER_ASSETS") / "cl100k_base.tiktoken"
```

add `pytestmark = pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")` after its imports only if every test calls `_bundle` or `_environment`; otherwise decorate each caller (`test_literal_readme_offline_recipe`, `test_public_python_quickstart`, `test_public_async_python_quickstart`, `test_generic_external_pack_installs_and_survives_reopen`, `test_literal_external_pack_offline_recipe`, `test_framework_adapter_keeps_complete_multiple_citations`, `test_optimized_quickstart_reports_failed_setup`, `test_shipped_pack_project_global_history_and_hierarchy`). Replace `test_required_tokenizer_fixture_fails_clearly` with a single case, because the unset case is now covered by `tests/test_asset_markers.py`:

```python
@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_required_tokenizer_fixture_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYCHORA_TEST_TOKENIZER_ASSETS", str(tmp_path / "missing"))
    with pytest.raises(pytest.fail.Exception, match="TYCHORA_TEST_TOKENIZER_ASSETS"):
        _bundle(tmp_path)
```

Sites that already `pytest.skip` when the variable is unset (`tests/indexing/conftest.py:80`, `tests/indexing/test_sources.py:852`, `tests/ingestion/test_vision.py:1345, 2102, 2410`, `tests/cli/test_output.py:228, 269, 366`, `tests/operations/test_setup.py:111`, `tests/providers/test_reranker.py:536`, `tests/retrieval/test_segments.py:947`, `tests/retrieval/test_query.py:810`) already pass on a clean machine; leave them unless Step 7 shows a failure there.

- [ ] **Step 5: Remove the environment assumptions**

In `tests/test_import.py`, replace the command of `test_console_command_displays_help_without_creating_state`

```python
        ["tychora", "--help"],
```

with

```python
        [str(Path(sys.executable).parent / "tychora"), "--help"],
```

and add `from pathlib import Path` to the imports.

In `tests/transports/test_dashboard_distribution.py` add `import pytest` and decorate the test:

```python
@pytest.mark.release
def test_dashboard_in_built_wheel_and_sdist(tmp_path):
```

(`build.yml` runs `pytest tests/release`, and `dashboard.yml` runs this file by path, so it keeps running in CI.)

- [ ] **Step 6: Verify the moved fixtures still serve their tests**

Run (assets provisioned as in `docs/contributing.md`):

```bash
uv run --locked pytest tests/ingestion/test_docling_mapping.py tests/ingestion/test_document_records.py -q
uv run --locked pytest -m qualification tests/qualification/test_docling.py tests/qualification/test_docling_records.py -q
```

Expected: same pass counts as before the move (the mapping file passed 54 on a fully installed machine).

- [ ] **Step 7: Prove S2**

```bash
env -i PATH="$PATH" HOME="$HOME" uv run --locked pytest tests -q -p no:cacheprovider \
  -m 'not live and not qualification and not release' -rs | tail -40
```

Expected: `0 failed`, `0 errors`; skip reasons read `requires TYCHORA_TEST_... (provision with packaging/provision_ci_assets.py)`. For any remaining failure, apply pattern A or B to that test, then rerun. Also run the isolation pair: `uv run --locked pytest tests/ingestion/test_docling_mapping.py -q` and `uv run --locked pytest tests/ingestion/test_document_records.py -q` (each alone).

- [ ] **Step 8: Commit**

```bash
git add tests
git commit -m "test: skip asset-dependent tests by name and share Docling fixtures"
```

---

### Task 6: Suite speed: pinned tools, parallel runs, scaled waits, slow markers, shared fixtures

Spec §4.2 (pins) and §4.7 "Suite speed", "Runtime budget", "Random order", "Fixture and product hot spots". Measured on 12 cores: serial without coverage 20 min 42 s; `-n auto` 3 min 49 s; `-n auto` with sysmon line coverage 3 min 42 s.

**Files:**
- Modify: `pyproject.toml` (`[dependency-groups]`, `[tool.pytest.ini_options]`, new `[tool.coverage.run]`), `uv.lock`
- Create: `quality/coverage-line.toml`, `tests/helpers/timing.py`, `tests/test_timing.py`
- Modify: the 35 `timeout=10` sites (list in Step 5)
- Modify: `tests/ingestion/test_acquisition.py:107`, `tests/performance/test_cache_states.py:138`, `tests/acceptance/test_crash_matrix.py`, `tests/evaluation/test_evaluation_http.py:44`, `tests/evaluation/test_provenance.py:94`, `tests/ingestion/test_ocr_lineage.py:184`
- Modify: `tests/evaluation/conftest.py`, `tests/evaluation/test_dataset.py`, `tests/application/test_inspection.py`

**Interfaces:**
- Produces: `tests.helpers.timing.scaled(seconds: float) -> float`; markers `slow` and `quarantine(reason, issue)`; fixtures `shared_integrity_corpus` (module scope, `tests/evaluation/conftest.py`) and `published_shared` (module scope, `tests/application/test_inspection.py`); coverage configs `[tool.coverage.run]` (branch) and `quality/coverage-line.toml` (lines).

- [ ] **Step 1: Pin the tool groups**

In `pyproject.toml` replace the `dev` and `audit` lines of `[dependency-groups]` and add `mutation`:

```toml
dev = [
  "pytest==9.1.1",
  "pytest-asyncio==1.4.0",
  "pytest-cov==7.1.0",
  "coverage==7.16.2",
  "pytest-randomly==5.0.0",
  "pytest-xdist==3.8.0",
  "httpx",
  "ruff==0.16.7",
  "ty==0.0.80",
  "import-linter==2.15",
  "vulture==2.16",
  "complexipy==8.0.1",
  "radon==5.1.0",
]
build = ["hatchling==1.32.4", "pyinstaller"]
test-fixtures = ["reportlab", "python-docx"]
audit = ["pip-audit==2.10.1", "pip-licenses"]
mutation = ["mutmut==3.8.0"]
```

Run:

```bash
uv lock
uv sync --locked --all-extras --group dev --group test-fixtures
uv run --locked pytest --version
```

Expected: `pytest 9.1.1`; `uv lock --check` passes afterwards.

- [ ] **Step 2: Register the markers and the coverage configurations**

In `[tool.pytest.ini_options]` extend `markers` (keep the four existing entries):

```toml
  "slow: slower than 30 s locally; excluded from the pull-request test job, run nightly",
  "quarantine(reason, issue): outcome differs between random seeds; excluded from blocking runs, kept nightly",
```

Append to `pyproject.toml`:

```toml
[tool.coverage.run]
branch = true
source = ["tychora"]
```

Create `quality/coverage-line.toml`:

```toml
# Pull-request coverage measures lines only, so COVERAGE_CORE=sysmon applies on
# Python 3.12 (sys.monitoring cannot measure branches before 3.14). Branch
# coverage runs nightly with [tool.coverage.run] from pyproject.toml.
[tool.coverage.run]
branch = false
source = ["tychora"]
```

- [ ] **Step 3: Write the failing timing test**

Create `tests/test_timing.py`:

```python
"""Fixed waits in tests scale with TYCHORA_TEST_TIMEOUT_FACTOR."""

from tests.helpers.timing import scaled


def test_wait_budgets_scale_with_the_factor(monkeypatch):
    monkeypatch.delenv("TYCHORA_TEST_TIMEOUT_FACTOR", raising=False)
    assert scaled(10) == 10
    monkeypatch.setenv("TYCHORA_TEST_TIMEOUT_FACTOR", "3")
    assert scaled(10) == 30
    monkeypatch.setenv("TYCHORA_TEST_TIMEOUT_FACTOR", "1.5")
    assert scaled(2) == 3
```

Run: `uv run --locked pytest tests/test_timing.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'tests.helpers.timing'`.

- [ ] **Step 4: Implement `scaled`**

Create `tests/helpers/timing.py`:

```python
"""Wait budgets for tests, scaled for slow runners."""

from __future__ import annotations

import os


def scaled(seconds: float) -> float:
    """seconds multiplied by TYCHORA_TEST_TIMEOUT_FACTOR (default 1; CI coverage runs use 3)."""
    return seconds * float(os.environ.get("TYCHORA_TEST_TIMEOUT_FACTOR", "1"))
```

Run: `uv run --locked pytest tests/test_timing.py -q` — Expected: PASS.

- [ ] **Step 5: Scale the 35 fixed waits**

Save outside the repository and run from the root with `uv run --locked python $TMPDIR/scale_waits.py`:

```python
"""One-off: replace timeout=10 with timeout=scaled(10) and import scaled."""

import re
import subprocess
from pathlib import Path

IMPORT = "from tests.helpers.timing import scaled\n"
files = subprocess.run(
    ["git", "grep", "-lP", r"timeout=10\b", "--", "tests"],
    capture_output=True, text=True, check=True,
).stdout.split()
for name in files:
    path = Path(name)
    text = re.sub(r"\btimeout=10\b", "timeout=scaled(10)", path.read_text())
    lines = text.splitlines(keepends=True)
    # Before the first top-level import that is not a __future__ import;
    # ruff's isort fix moves it into place afterwards.
    position = next(
        i
        for i, line in enumerate(lines)
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    )
    lines.insert(position, IMPORT)
    path.write_text("".join(lines))
    print(name)
```

Then sort the imports and check nothing else changed:

```bash
uv run --locked ruff check --fix --select I001 tests
uv run --locked ruff format tests
git grep -nP "timeout=10\b" -- tests
git grep -c "timeout=scaled(10)" -- tests | awk -F: '{s+=$2} END {print s}'
```

Expected: no `timeout=10` left; 35 scaled sites in `tests/acceptance/test_crash_matrix.py`, `tests/cli/test_output.py`, `tests/docs/test_examples.py`, `tests/evaluation/conftest.py`, `tests/evaluation/test_cli.py`, `tests/evaluation/test_global_scope_scenario.py`, `tests/evaluation/test_provenance.py`, `tests/evaluation/test_runner.py`, `tests/evaluation/test_temporal_scenarios.py`, `tests/indexing/test_collection_lifecycle.py`, `tests/performance/test_cache_states.py`, `tests/qualification/test_ladybug.py`, `tests/qualification/test_native_stack.py`, `tests/release/test_installed_wheel.py`, `tests/runtime/test_owner.py`, `tests/security/test_scope_paths.py`.

- [ ] **Step 6: Mark the slow tests**

Add `@pytest.mark.slow` to:

- `tests/ingestion/test_acquisition.py::test_default_ten_thousand_file_capture_has_complete_retention` (the 10,000-file acquisition test)
- `tests/performance/test_cache_states.py::test_real_four_states_use_subprocess_and_engine_counters` (the cache-state subprocess benchmark)
- `tests/acceptance/test_crash_matrix.py`: the whole module, with `pytestmark = pytest.mark.slow` after the imports (the crash matrix)
- `tests/evaluation/test_evaluation_http.py::test_http_evaluation_list_and_detail_serialize_real_runs_and_deny_reader` (50.0 s)
- `tests/evaluation/test_provenance.py::test_selected_bindings_are_bounded_and_reused_without_snapshot_rehydration` (47.5 s)
- `tests/ingestion/test_ocr_lineage.py::test_real_sdk_emission_binds_exact_cells_and_fields` (49.9 s)

Then find every other test over 30 s and mark it too:

```bash
uv run --locked pytest tests -n auto -q -p no:randomly --durations=40 \
  -m 'not live and not qualification and not release and not slow' | sed -n '/slowest/,/====/p'
```

Expected after marking: no unmarked test above 30.0 s in that list.

- [ ] **Step 7: Share the read-only evaluation corpus per module**

In `tests/evaluation/conftest.py`, add `from contextlib import contextmanager` to the imports, rename the fixture's function to a context manager and wrap it twice. Replace

```python
@pytest.fixture
def executable_integrity_corpus(tmp_path):
    declaration = json.loads(CORPUS.read_text())
```

with

```python
@contextmanager
def _integrity_corpus(tmp_path: Path):
    """Ingest the declared synthetic corpus and write its digest-pinned gold set."""
    declaration = json.loads(CORPUS.read_text())
```

leave the body unchanged (it ends with `yield kb, gold, tuple(case["case_id"] for case in cases)`), and append:

```python
@pytest.fixture
def executable_integrity_corpus(tmp_path):
    """A fresh corpus for tests that register datasets or run evaluations."""
    with _integrity_corpus(tmp_path) as corpus:
        yield corpus


@pytest.fixture(scope="module")
def shared_integrity_corpus(tmp_path_factory):
    """One corpus per module for read-only tests; never register or run against it."""
    with _integrity_corpus(tmp_path_factory.mktemp("integrity-corpus")) as corpus:
        yield corpus
```

In `tests/evaluation/test_dataset.py` switch the three read-only tests (`test_load_versioned_immutable_dataset`, `test_tampered_cases_fail_version_check`, `test_bytes_registration_uses_same_digest_validation`) from `executable_integrity_corpus` to `shared_integrity_corpus` (parameter name and the unpacking line). Confirm each copies `gold` before changing it (`test_tampered_cases_fail_version_check` must write into its own `tmp_path`, never into the shared `gold`); if it edits `gold` in place, keep it on `executable_integrity_corpus`.

- [ ] **Step 8: Share the read-only inspection workspace per module**

In `tests/application/test_inspection.py`, add `import pytest_asyncio` and `from contextlib import asynccontextmanager`, and turn the `published` fixture body into a context manager:

```python
@asynccontextmanager
async def _published(tmp_path: Path):
    """A workspace with one published synthetic manual and its first unit id."""
    settings = tmp_path / "workspace.toml"
    # ... the existing body of `published`, unchanged, ending in:
    #     yield owner, scope, uid


@pytest.fixture
async def published(tmp_path: Path):
    """A fresh workspace for tests that insert rows or patch services."""
    async with _published(tmp_path) as value:
        yield value


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def published_shared(tmp_path_factory):
    """One workspace per module for tests that only read."""
    async with _published(tmp_path_factory.mktemp("published")) as value:
        yield value
```

Switch these read-only tests to `published_shared` and change their marker to `@pytest.mark.asyncio(loop_scope="module")`: `test_exact_range_and_unknown_id`, `test_empty_selected_range_is_invalid_even_for_retained_unit`, `test_lookup_only_scope_can_inspect_its_source`, `test_denied_scope_cannot_inspect_registered_unit`, `test_explicit_relations_require_authorized_seed_before_graph`, `test_discovery_counts_only_readable_collection_sources`, `test_job_inspection_uses_durable_authorization`, `test_diagnostic_summary_is_scope_limited`. Before switching each, confirm its body contains no `INSERT`, `UPDATE`, `DELETE`, `.add(`, `monkeypatch` or `remove`:

```bash
awk '/^(async )?def test/{name=$0} /INSERT|UPDATE|DELETE|\.add\(|monkeypatch|remove/{print name}' tests/application/test_inspection.py | sort -u
```

None of the eight names may appear in that output. The other tests keep `published`.

- [ ] **Step 9: Run the suite in parallel and in random order, twice**

```bash
uv run --locked pytest tests -n auto -q -m 'not live and not qualification and not release' | tail -3
uv run --locked pytest tests -n auto -q -p randomly --randomly-seed=12345 -m 'not live and not qualification and not release' | tail -3
COVERAGE_CORE=sysmon uv run --locked pytest tests -n auto -q -m 'not live and not qualification and not release and not slow' --cov --cov-config=quality/coverage-line.toml --cov-report=term | tail -5
```

Expected: no failures in either order (assets provisioned); the third run prints a `TOTAL` line near 85% and takes a few minutes. A test that fails only in one order is a shared-state bug: fix it, or if the fix belongs to engine core, mark it `@pytest.mark.quarantine(reason="...", issue="<GitHub issue URL>")` and open the issue.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml uv.lock quality/coverage-line.toml tests
git commit -m "test: run the suite in parallel with scaled waits, slow markers and shared read-only fixtures"
```

---

### Task 7: Remove plan-task references (S5)

Spec §3.6, §4.8. G15 must reach 0 lines and 0 file names.

**Files:**
- Modify: `src/tychora/adapters/sqlite/publication.py:90`, `src/tychora/application/removal.py:509`, `src/tychora/application/reranking.py:406`, `src/tychora/contracts/composition.py:328`, `src/tychora/ingestion/document_parts.py:19`, `src/tychora/retrieval/visuals.py:255`
- Rename: `dashboard/e2e/task5.spec.ts` → `ocr-intake.spec.ts`, `task6.spec.ts` → `pdf-intake.spec.ts`, `task7.spec.ts` → `request-and-restore.spec.ts`; `dashboard/src/test/task5.test.tsx` → `ocr-intake.test.tsx`; the 32 `dashboard/contracts/task*-*.json`
- Modify: `dashboard/contracts/README.md`, `dashboard/e2e/dashboard.spec.ts`, `dashboard/e2e/qualification.py`, `dashboard/playwright.config.ts`, `dashboard/scripts/generate_contracts.py`, `dashboard/src/pages/documents/draft.tsx`, `dashboard/src/test/documents.test.tsx`, `dashboard/src/test/operations.test.tsx`, `dashboard/src/test/pdf-intake.test.tsx`, `tests/application/test_collections.py`, `tests/release/conftest.py`, `docs/dashboard.md`, `docs/distribution.md`, `docs/qualification.md`

**Interfaces:**
- Produces: contract fixture names used by dashboard tests: `collections-*.json`, `models-*.json`, `settings-*.json`, `metadata-*-schema.json`, `ocr-*.json`, `pdf-*.json`.

- [ ] **Step 1: Confirm the starting count**

```bash
git grep -nP '\b[Tt]ask ?[0-9]+\b' -- src tests dashboard ':!dashboard/package-lock.json' | wc -l
git ls-files | grep -P '[Tt]ask ?[0-9]+' | grep -v '^docs/superpowers/' | wc -l
```

Expected: 89 lines, 36 names.

- [ ] **Step 2: Rewrite the six Python comments and docstrings**

| File:line | Before | After |
| --- | --- | --- |
| `adapters/sqlite/publication.py:90` | `"""Exact Task 7 typed retention header for a complete snapshot plan."""` | `"""Canonical bytes of the snapshot manifest, the typed retention header of a complete plan."""` |
| `application/removal.py:509` | `"""Owner-coordinated resume; Task 40 calls this before its copied DB cut."""` | `"""Resume pending payload deletions on the owner before any copy of the database is taken."""` |
| `application/reranking.py:406` | `"""Bind every rendered span and dependency to Task19's canonical ownership."""` | `"""Check that every rendered span and dependency belongs to the canonical owner of its extraction."""` |
| `contracts/composition.py:328` | `"""Preserve the exact Task13 plan form when no region enrichment exists."""` | `"""Serialize the plan unchanged when no region enrichment exists, so its digest stays stable."""` |
| `ingestion/document_parts.py:19` | `# Task34 must disclose these aggregate semantic expansion ceilings. They count` | `# Documented ingestion limits: aggregate semantic expansion ceilings. They count` |
| `retrieval/visuals.py:255` | `"""Read original assets through Task 30's bounded retained-facts loader."""` | `"""Read original assets through the bounded loader of retained facts."""` |

(Paths are under `src/tychora/`.)

- [ ] **Step 3: Rename the dashboard files and every reference in one pass**

Save outside the repository and run from the root with `uv run --locked python $TMPDIR/rename_plan_references.py`:

```python
"""One-off: rename plan-numbered dashboard files and rewrite every reference."""

import subprocess
from pathlib import Path

FILES = {
    "dashboard/e2e/task5.spec.ts": "dashboard/e2e/ocr-intake.spec.ts",
    "dashboard/e2e/task6.spec.ts": "dashboard/e2e/pdf-intake.spec.ts",
    "dashboard/e2e/task7.spec.ts": "dashboard/e2e/request-and-restore.spec.ts",
    "dashboard/src/test/task5.test.tsx": "dashboard/src/test/ocr-intake.test.tsx",
}
CONTRACTS = {
    "task2-collection-archived.json": "collections-archived.json",
    "task2-collection-consent.json": "collections-consent.json",
    "task2-collection.json": "collections-detail.json",
    "task2-collections.json": "collections-page.json",
    "task2-documents.json": "collections-documents.json",
    "task2-handshake.json": "collections-handshake.json",
    "task3-model-readiness.json": "models-readiness.json",
    "task3-model-setup-plan.json": "models-setup-plan.json",
    "task3-models.json": "models-list.json",
    "task3-settings-applied.json": "settings-applied.json",
    "task3-settings-fields.json": "settings-fields.json",
    "task3-settings-preview.json": "settings-preview.json",
    "task4-document-metadata-schema.json": "metadata-document-schema.json",
    "task4-manual-metadata-schema.json": "metadata-manual-schema.json",
}
for name in sorted(Path("dashboard/contracts").glob("task5-ocr-*.json")):
    CONTRACTS[name.name] = name.name.removeprefix("task5-")
for name in sorted(Path("dashboard/contracts").glob("task6-pdf-*.json")):
    CONTRACTS[name.name] = name.name.removeprefix("task6-")

# Longest first, so "task2-collection.json" never rewrites "task2-collection-archived.json".
TEXT = sorted(CONTRACTS.items(), key=lambda item: -len(item[0])) + [
    ("task5.spec.ts", "ocr-intake.spec.ts"),
    ("task6.spec.ts", "pdf-intake.spec.ts"),
    ("task7.spec.ts", "request-and-restore.spec.ts"),
    ("task5.test.tsx", "ocr-intake.test.tsx"),
    ("Task 5 `task5-ocr-*` captures", "The `ocr-*` captures"),
    ("Task 6 `task6-pdf-*-status.json` captures", "The `pdf-*-status.json` captures"),
    ("`task6-pdf-{analysed,", "`pdf-{analysed,"),
    ('f"task4-{kind}-metadata-schema"', 'f"metadata-{kind}-schema"'),
    ('f"task6-pdf-{mode}-status.json"', 'f"pdf-{mode}-status.json"'),
    ('f"task6-pdf-{key}.json"', 'f"pdf-{key}.json"'),
    ("Export a synthetic Task6 real HTTP browser capture", "Export a synthetic PDF intake real HTTP browser capture"),
    ("synthetic-task5-scan", "synthetic-ocr-scan"),
    ("dashboard-e2e/task5-fixtures", "dashboard-e2e/ocr-intake-fixtures"),
    ("dashboard-e2e/task5", "dashboard-e2e/ocr-intake"),
    ("dashboard-e2e/task6", "dashboard-e2e/pdf-intake"),
    ("dashboard-e2e/task7", "dashboard-e2e/request-and-restore"),
    ("Task5 journeys require actual HTTP", "OCR intake journeys require actual HTTP"),
    ("Task7 requires real HTTP", "Request and restore journeys require real HTTP"),
    ('test("task5 ', 'test("OCR intake: '),
    ('test("task6 ', 'test("PDF intake: '),
    ('test("task7 ', 'test("Request and restore: '),
    ("--grep 'task5 qualified", "--grep 'OCR intake: qualified"),
    ("synthetic Task5 workflow", "synthetic OCR intake workflow"),
    ("task5-synthetic", "ocr-intake-synthetic"),
    ("Task5 synthetic", "OCR intake synthetic"),
    ("task6-mixed.pdf", "pdf-intake-mixed.pdf"),
    ("task6-native.pdf", "pdf-intake-native.pdf"),
    ("task6-invalid.pdf", "pdf-intake-invalid.pdf"),
    ("`task6-${mode}.pdf`", "`pdf-intake-${mode}.pdf`"),
    ('f"task6-{mode}.pdf"', 'f"pdf-intake-{mode}.pdf"'),
    ("task7-readable", "restore-readable"),
    ("task7-local-acceptance", "restore-local-acceptance"),
    ("-missing-task7", "-missing-restore"),
    ("task4-fix1-mobile-", "documents-mobile-"),
    ("`task4-${", "`documents-${"),
    ("Task 6 retains this exact client/scope for receipt-bound PDF requests.",
     "PDF intake retains this exact client and scope for receipt-bound requests."),
    ("tychora-task45-uv", "tychora-release-uv"),
    ("Task 45 inventory", "release inventory"),
    ("Task 46 executable report", "executable qualification report"),
]

for old, new in FILES.items():
    subprocess.run(["git", "mv", old, new], check=True)
for old, new in CONTRACTS.items():
    subprocess.run(["git", "mv", f"dashboard/contracts/{old}", f"dashboard/contracts/{new}"], check=True)

tracked = subprocess.run(["git", "ls-files", "dashboard", "tests", "docs", "HANDOFF.md"],
                         capture_output=True, text=True, check=True).stdout.split()
for name in tracked:
    path = Path(name)
    if name.startswith("docs/superpowers/") or name.endswith("package-lock.json") or not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    updated = text
    for old, new in TEXT:
        updated = updated.replace(old, new)
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        print("rewrote", name)
```

- [ ] **Step 4: Check that nothing is left**

```bash
git grep -nP '\b[Tt]ask ?[0-9]+\b' -- src tests dashboard ':!dashboard/package-lock.json'
git ls-files | grep -P '[Tt]ask ?[0-9]+' | grep -v '^docs/superpowers/'
```

Expected: both print nothing. Rewrite any remaining line by hand in present tense, describing the behaviour, not the plan.

- [ ] **Step 5: Run the affected tests**

```bash
npm --prefix dashboard exec -- prettier --write src e2e
uv run --locked ruff format dashboard tests
npm --prefix dashboard test
npm --prefix dashboard run typecheck
npm --prefix dashboard run format:check
npm --prefix dashboard exec -- playwright test --list | grep -E "OCR intake|PDF intake|Request and restore"
uv run --locked pytest tests/application/test_collections.py -q
PYTHONPATH=src uv run --locked python dashboard/scripts/generate_contracts.py --help
```

Expected: Vitest and tsc pass; Playwright lists the renamed journeys; the collections test passes; the generator's help shows the reworded `--pdf-evidence` text.

- [ ] **Step 6: Commit**

```bash
git add -A src dashboard tests docs
git commit -m "chore: name tests, fixtures and comments after behaviour, not plan tasks"
```

---

### Task 8: Dashboard health: import cycle, dead code, coverage and tools

Spec §3.5, §4.2, §4.3 G10, G13, G16 (dashboard), G28.

**Files:**
- Modify: `dashboard/package.json`, `dashboard/package-lock.json`, `dashboard/vite.config.ts`
- Create: `dashboard/knip.json`, `dashboard/src/api/types.ts`
- Modify: `dashboard/src/api/operations.ts:1-13`, `dashboard/src/api/contracts.ts:626`, `dashboard/src/api/administration.ts:8`, `dashboard/src/pages/Documents.tsx:4`, `dashboard/src/api/events.ts:94`, `dashboard/src/App.tsx:25`, `dashboard/src/pages/sources/Coverage.tsx:32`, `dashboard/src/api/contracts.ts` (16 exported types)
- Delete: `dashboard/src/pages/Sources.tsx`

**Interfaces:**
- Produces: npm scripts `knip`, `cycles`, `coverage`; `dashboard/coverage/coverage-summary.json` (read by the checker's dashboard coverage gate, Task 14); `dashboard/node_modules/.bin/jscpd`, `knip`, `madge` (invoked by the checker).

- [ ] **Step 1: Install the pinned tools and drop the unused dependency**

```bash
npm --prefix dashboard install --save-dev --save-exact jscpd@5.4.1 knip@6.41.0 madge@8.0.0 @vitest/coverage-v8@4.1.11
npm --prefix dashboard uninstall @testing-library/user-event
```

Add to `"scripts"` in `dashboard/package.json`:

```json
    "knip": "knip --no-progress",
    "cycles": "madge --circular --extensions ts,tsx --ts-config tsconfig.json src",
    "coverage": "vitest run --coverage",
```

Create `dashboard/knip.json`:

```json
{
  "$schema": "https://unpkg.com/knip@6/schema.json",
  "project": ["src/**/*.{ts,tsx}", "e2e/**/*.ts", "scripts/**/*.mjs"]
}
```

- [ ] **Step 2: See the failures**

```bash
npm --prefix dashboard run knip
npm --prefix dashboard run cycles
```

Expected: knip lists 1 unused file (`src/pages/Sources.tsx`), 3 unused exports (`delay`, `destinations`, `Channels`) and 17 unused exported types; madge prints `1) api/contracts.ts > api/operations.ts`.

- [ ] **Step 3: Break the cycle**

Create `dashboard/src/api/types.ts`:

```ts
/** Job types shared by the contract and operation modules, kept apart to avoid a cycle. */
export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export interface JobHandle {
  id: string;
  operation: string;
  state: JobState;
}
```

In `dashboard/src/api/operations.ts` delete lines 2-13 (the `JobState` and `JobHandle` declarations) and add after the first import:

```ts
import type { JobHandle, JobState } from "./types";
```

(`JobStatus extends JobHandle` and the `state: JobState` fields further down keep using them.)

In `dashboard/src/api/contracts.ts:626` replace `job: import("./operations").JobHandle | null;` with `job: JobHandle | null;` and add at the top `import type { JobHandle } from "./types";`. In `dashboard/src/api/administration.ts:8` and `dashboard/src/pages/Documents.tsx:4` import `JobHandle` from `./types` / `../api/types` instead of `operations`.

- [ ] **Step 4: Remove the dead code**

```bash
git rm dashboard/src/pages/Sources.tsx
```

Remove only the `export` keyword (each name is still used inside its module) from: `delay` (`src/api/events.ts:94`), `destinations` (`src/App.tsx:25`), `Channels` (`src/pages/sources/Coverage.tsx:32`), and from the exported types `Geometry`, `Excerpt`, `EvidenceUnit`, `StructuredRecord`, `RelationPath`, `Filter`, `RenderTarget`, `ModelAssetState`, `ExtractionUnitSummary`, `ExtractionWindow`, `PdfProcessingPolicy`, `PdfAnalysis` (`src/api/contracts.ts`) and `Artifact`, `Extraction`, `Revision`, `CaseRun` (`src/api/operations.ts`).

- [ ] **Step 5: Set coverage thresholds from the measured values**

Add to the `test` block of `dashboard/vite.config.ts`:

```ts
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/test/**"],
      reporter: ["text-summary", "json-summary"],
      thresholds: {
        statements: 81.6,
        branches: 77.0,
        functions: 73.6,
        lines: 83.5,
      },
    },
```

Run `npm --prefix dashboard run coverage`. If a measured value printed by `text-summary` is below its threshold (the include list may differ slightly from the spec's measurement), set that threshold to the measured value rounded down to one decimal, and state the measured values in the commit message.

- [ ] **Step 6: Verify every dashboard gate**

```bash
npm --prefix dashboard run knip
npm --prefix dashboard run cycles
npm --prefix dashboard test
npm --prefix dashboard run typecheck
npm --prefix dashboard run format:check
npm --prefix dashboard run coverage
npm --prefix dashboard run check:build
npm --prefix dashboard audit --audit-level=low
```

Expected: knip prints nothing and exits 0; madge prints "No circular dependency found!"; tests, types, Prettier, coverage thresholds pass; `check:build` reports no drift in `src/tychora/resources/dashboard` (type-only and unused-code changes do not change the bundle); audit finds 0 vulnerabilities.

- [ ] **Step 7: Commit**

```bash
git add dashboard
git commit -m "chore: remove the dashboard import cycle and dead code, add coverage and analysis tools"
```

---

### Task 9: Public API docstrings and the comment rule

Spec §4.8 "Docstrings" and "Rule". G14's hard part requires a docstring on every public name of `tychora.api` and `tychora.clients` (65 today, including methods of the underscore-named resource classes that back `kb.sources`, `kb.jobs`, ...).

**Files:**
- Modify: `src/tychora/api.py`, `src/tychora/clients/sync.py`, `src/tychora/clients/async_.py`
- Modify: `docs/contributing.md`

**Interfaces:**
- Consumes: nothing. Produces: docstrings only (no behaviour change).

- [ ] **Step 1: List what is missing**

```bash
uv run --locked python - <<'EOF'
import ast, pathlib
paths = [pathlib.Path("src/tychora/api.py"), *sorted(pathlib.Path("src/tychora/clients").glob("*.py"))]
def walk(node, prefix=""):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield prefix + child.name, child
            yield from walk(child, f"{prefix}{child.name}.")
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield prefix + child.name, child
missing = [f"{p}::{n}" for p in paths for n, d in walk(ast.parse(p.read_text())) if not d.name.startswith("_") and ast.get_docstring(d) is None]
print(len(missing)); print("\n".join(missing))
EOF
```

Expected: `65`, followed by the names below.

- [ ] **Step 2: Add the docstrings**

Add each docstring as the first statement of the definition. `clients/async_.py` has the same resource classes and methods as `clients/sync.py`; give each async method the same docstring as its sync counterpart.

| Definition | Docstring |
| --- | --- |
| `api.py::_LazyOutputCounter.count` | `"""Count output tokens of text, loading the verified tokenizer on first use."""` |
| `api.py::_LazyOutputCounter.ensure_available` | `"""Load the verified cl100k_base tokenizer from the model cache if it is not loaded yet."""` |
| `api.py::LocalWorkspace` | `"""An open local workspace: its owner, services, request scope and provider bindings."""` |
| `api.py::LocalWorkspace.default_collection_id` | `"""Identifier of the default collection; raises FORBIDDEN_SCOPE when outside the grant."""` |
| `api.py::LocalWorkspace.require_open` | `"""Raise RuntimeError if the client has been closed."""` |
| `api.py::LocalWorkspace.close` | `"""Stop source jobs, release provider runtimes and close the workspace; safe to repeat."""` |
| `api.py::LocalWorkspace.parse_source` | `"""Return a SourceInput, resolving the collection alias "default" to its identifier."""` |
| `_Sources.add` | `"""Register a source and start ingesting it; returns the job handle."""` |
| `_Sources.sync` | `"""Capture a registered source again and ingest what changed; returns the job handle."""` |
| `_Sources.revisions` | `"""Page through the retained revisions of a source."""` |
| `_Sources.extractions` | `"""Page through the extractions of one source revision."""` |
| `_Sources.channels` | `"""Page through the indexed channels of one extraction."""` |
| `_Sources.selections` | `"""Page through the evidence selections of one extraction."""` |
| `_Evidence.get` | `"""Inspect one evidence unit in full, or only its characters from start to end."""` |
| `_Assets.get` | `"""Inspect one registered asset with its metadata and payload."""` |
| `_Assets.read` | `"""Return the bytes of a registered asset; raises NOT_FOUND when the payload is gone."""` |
| `_Relations.traverse` | `"""Follow relations from authorized seed evidence within the caller's scope."""` |
| `_Jobs.get` | `"""Return the persisted status of a job."""` |
| `_Jobs.wait` | `"""Wait until a job finishes or timeout seconds pass (default 30) and return its status."""` |
| `_Jobs.cancel` | `"""Request cancellation of a job, signal its worker and return its status."""` |
| `_Jobs.events` | `"""Yield the job's events after the given sequence until the job reaches a final state."""` |
| `_Jobs.events_after` | `"""Return up to 100 job events after the given sequence."""` |
| `_Evaluations.run` | `"""Run a retrieval evaluation against a registered gold dataset; returns the job handle."""` |
| `_Evaluations.register_dataset` | `"""Register a local gold dataset directory holding manifest.json and cases.jsonl."""` |
| `_Evaluations.get` | `"""Return the report of one evaluation run."""` |
| `_Evaluations.list` | `"""List the saved evaluation run summaries the caller may read."""` |
| `_Management.run` | `"""Run a maintenance request, such as backup, health or rebuild, as a job."""` |
| `Tychora` | `"""Synchronous client for a local workspace; use it as a context manager or call close()."""` |
| `AsyncTychora` | `"""Asynchronous client for a local workspace; use it with async with or await close()."""` |
| `Tychora.open` / `AsyncTychora.open` | `"""Open, or create, the workspace at path and return a client bound to it."""` |
| `Tychora.connect` / `AsyncTychora.connect` | `"""Return a client for a workspace served over HTTP by `tychora serve`."""` |
| `Tychora.close` / `AsyncTychora.close` | `"""Close the workspace and release its resources."""` |
| `Tychora.default_collection_id` / `AsyncTychora.default_collection_id` | `"""Identifier of the default collection of this workspace."""` |
| `Tychora.scope` / `AsyncTychora.scope` | `"""The request scope every call of this client runs under."""` |
| `Tychora.discover` / `AsyncTychora.discover` | `"""List kinds, sources or other discovery subjects the scope can read."""` |
| `Tychora.inspect` / `AsyncTychora.inspect` | `"""Inspect evidence, originals, assets, jobs or query runs by identifier."""` |
| `Tychora.search` / `AsyncTychora.search` | `"""Search the workspace and return cited evidence segments."""` |

Run Step 1 again. Expected: `0`.

- [ ] **Step 3: Write the comment rule into the contributing guide**

Append to `docs/contributing.md`, before `## Documentation and publication`:

```markdown
## Comments and docstrings

Comments and docstrings say what the code does and why, in present tense, for a reader who has not seen any plan or review. They state intent, units, invariants or reasons that the code cannot show. They never cite plan tasks, plan file names or review identifiers; `python -m tools.health check` fails on a reference such as "Task 7".

Two rewrites from this code base:

- `publication_manifest` said "Exact Task 7 typed retention header for a complete snapshot plan." It now says "Canonical bytes of the snapshot manifest, the typed retention header of a complete plan."
- A reranking check said "Bind every rendered span and dependency to Task19's canonical ownership." It now says "Check that every rendered span and dependency belongs to the canonical owner of its extraction."

Every public class, function and method of `tychora.api` and `tychora.clients` has a docstring, because [the API reference](api.md) documents that surface and editors show it.
```

- [ ] **Step 4: Verify and commit**

```bash
uv run --locked ruff check . && uv run --locked ty check src tests
uv run --locked pytest tests/api tests/docs -q -n auto
git add src/tychora/api.py src/tychora/clients docs/contributing.md
git commit -m "docs: document every public client method and the comment rule"
```

---

### Task 10: Layering contracts in import-linter (S4)

Spec §4.4. Four contracts; today's violating edges listed as exceptions with file and line; every contract kept.

**Files:**
- Modify: `pyproject.toml` (new `[tool.importlinter]` block)
- Modify: `docs/architecture.md` (new subsection after the table of section 1)

**Interfaces:**
- Consumes: explicit packages (Task 3).
- Produces: contract ids `layers`, `adapters-implement-ports`, `storage-through-ports`, `pure-contracts`; `lint-imports` exit code (G7 hard part); the number of `ignore_imports` entries per contract id (G7 ratchet, read by `tools/health/layering.py` in Task 14).

- [ ] **Step 1: See the contracts fail without exceptions**

Append the block of Step 2 without any `ignore_imports` lists and run `uv run --locked lint-imports`. Expected: `Contracts: 0 kept, 4 broken.` Then continue with the full block.

- [ ] **Step 2: Add the contracts with their exceptions**

Append to `pyproject.toml` (this exact block was verified on a copy of `b1cdb87` with the packages of Task 3: 4 kept, 0 broken):

```toml
[tool.importlinter]
root_package = "tychora"
include_external_packages = true

[[tool.importlinter.contracts]]
id = "layers"
name = "Engine layers point downwards"
type = "layers"
layers = [
  "tychora.interfaces | tychora.clients",
  "tychora.api",
  "tychora.evaluation",
  "tychora.application",
  "tychora.retrieval : tychora.indexing : tychora.ingestion : tychora.formatting",
  "tychora.configuration",
  "tychora.ports",
  "tychora.contracts",
]
# Upward edges measured on b91d808 (17), the two client-to-HTTP-client edges, and the
# fifteen pipeline-to-adapter edges of contract "storage-through-ports", whose chains
# through tychora.adapters reach upper layers.
ignore_imports = [
  "tychora.application.default_bindings -> tychora.api",  # tychora/application/default_bindings.py:27
  "tychora.application.owner -> tychora.evaluation.runner",  # tychora/application/owner.py:56
  "tychora.application.services -> tychora.api",  # tychora/application/services.py:33
  "tychora.application.services -> tychora.evaluation.runner",  # tychora/application/services.py:27
  "tychora.contracts.code -> tychora.ports.language",  # tychora/contracts/code.py:10
  "tychora.indexing.coordinator -> tychora.application.collection_policy",  # tychora/indexing/coordinator.py:16
  "tychora.ingestion.composition -> tychora.application.pdf_processing",  # tychora/ingestion/composition.py:271
  "tychora.ports.acquisition -> tychora.configuration.models",  # tychora/ports/acquisition.py:6
  "tychora.ports.parsers -> tychora.configuration.models",  # tychora/ports/parsers.py:5
  "tychora.ports.query -> tychora.application.owner",  # tychora/ports/query.py:14
  "tychora.ports.query -> tychora.retrieval.cache",  # tychora/ports/query.py:11
  "tychora.retrieval.eligibility -> tychora.application.owner",  # tychora/retrieval/eligibility.py:140
  "tychora.retrieval.rerank -> tychora.application.owner",  # tychora/retrieval/rerank.py:40
  "tychora.retrieval.rerank -> tychora.application.reranking",  # tychora/retrieval/rerank.py:9
  "tychora.retrieval.segments -> tychora.application.setup",  # tychora/retrieval/segments.py:38
  "tychora.retrieval.visuals -> tychora.application.owner",  # tychora/retrieval/visuals.py:18
  "tychora.retrieval.visuals -> tychora.application.reranking",  # tychora/retrieval/visuals.py:8
  "tychora.clients.async_ -> tychora.interfaces.http.client",  # tychora/clients/async_.py:405
  "tychora.clients.sync -> tychora.interfaces.http.client",  # tychora/clients/sync.py:352
  "tychora.contracts.documents -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/documents.py:304
  "tychora.contracts.extraction -> tychora.adapters.docling.ocr_evidence",  # tychora/contracts/extraction.py:217
  "tychora.contracts.extraction -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/extraction.py:193
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.corpus",  # tychora/indexing/coordinator.py:13
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.graph",  # tychora/indexing/coordinator.py:14
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.publication",  # tychora/indexing/coordinator.py:15
  "tychora.ingestion.acquire -> tychora.adapters.code.capture",  # tychora/ingestion/acquire.py:12
  "tychora.ingestion.acquire -> tychora.adapters.files.cas",  # tychora/ingestion/acquire.py:13
  "tychora.ingestion.acquire -> tychora.adapters.files.references",  # tychora/ingestion/acquire.py:14
  "tychora.ingestion.document_parts -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_parts.py:7
  "tychora.ingestion.document_records -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_records.py:9
  "tychora.ingestion.document_views -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_views.py:8
  "tychora.ingestion.documents -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/documents.py:8
  "tychora.retrieval.eligibility -> tychora.adapters.ladybug.traversal",  # tychora/retrieval/eligibility.py:118
  "tychora.retrieval.rerank -> tychora.adapters.voyage.rerank",  # tychora/retrieval/rerank.py:39
]

[[tool.importlinter.contracts]]
id = "adapters-implement-ports"
name = "Adapters implement ports and never import the layers that use them"
type = "forbidden"
source_modules = ["tychora.adapters"]
forbidden_modules = [
  "tychora.application",
  "tychora.retrieval",
  "tychora.indexing",
  "tychora.ingestion",
  "tychora.evaluation",
  "tychora.formatting",
  "tychora.interfaces",
  "tychora.clients",
  "tychora.api",
]
ignore_imports = [
  "tychora.adapters.chroma.index -> tychora.indexing.receipts",  # tychora/adapters/chroma/index.py:31
  "tychora.adapters.docling.ocr_evidence -> tychora.application.pdf_processing",  # tychora/adapters/docling/ocr_evidence.py:162
  "tychora.adapters.docling.ocr_evidence -> tychora.ingestion.vision",  # tychora/adapters/docling/ocr_evidence.py:386
  "tychora.adapters.docling.parser -> tychora.application.owner",  # tychora/adapters/docling/parser.py:18
  "tychora.adapters.docling.parser -> tychora.application.pdf_processing",  # tychora/adapters/docling/parser.py:429
  "tychora.adapters.docling.parser -> tychora.application.runtime",  # tychora/adapters/docling/parser.py:614
  "tychora.adapters.docling.parser -> tychora.ingestion.document_records",  # tychora/adapters/docling/parser.py:964
  "tychora.adapters.docling.pdf_batches -> tychora.application.owner",  # tychora/adapters/docling/pdf_batches.py:195
  "tychora.adapters.docling.pdf_batches -> tychora.application.runtime",  # tychora/adapters/docling/pdf_batches.py:196
  "tychora.adapters.files.url_acquisition -> tychora.application.runtime",  # tychora/adapters/files/url_acquisition.py:13
  "tychora.adapters.sqlite.corpus -> tychora.application.collection_policy",  # tychora/adapters/sqlite/corpus.py:8
  "tychora.adapters.sqlite.corpus -> tychora.application.leases",  # tychora/adapters/sqlite/corpus.py:9
  "tychora.adapters.sqlite.eligibility -> tychora.application.collection_policy",  # tychora/adapters/sqlite/eligibility.py:9
  "tychora.adapters.sqlite.eligibility -> tychora.indexing.representations",  # tychora/adapters/sqlite/eligibility.py:39
  "tychora.adapters.sqlite.eligibility -> tychora.retrieval.eligibility",  # tychora/adapters/sqlite/eligibility.py:40
  "tychora.adapters.sqlite.evaluations -> tychora.evaluation.dataset",  # tychora/adapters/sqlite/evaluations.py:14
  "tychora.adapters.sqlite.history -> tychora.retrieval.response",  # tychora/adapters/sqlite/history.py:23
  "tychora.adapters.sqlite.lexical -> tychora.retrieval.lexical_query",  # tychora/adapters/sqlite/lexical.py:12
  "tychora.adapters.sqlite.publication -> tychora.application.collection_policy",  # tychora/adapters/sqlite/publication.py:15
  "tychora.adapters.sqlite.publication -> tychora.indexing.receipts",  # tychora/adapters/sqlite/publication.py:31
  "tychora.adapters.vision.anthropic -> tychora.application.owner",  # tychora/adapters/vision/anthropic.py:10
  "tychora.adapters.vision.local -> tychora.application.owner",  # tychora/adapters/vision/local.py:12
  "tychora.adapters.voyage.query -> tychora.application.owner",  # tychora/adapters/voyage/query.py:17
  "tychora.adapters.voyage.query -> tychora.application.query",  # tychora/adapters/voyage/query.py:53
  "tychora.adapters.voyage.query -> tychora.application.visual",  # tychora/adapters/voyage/query.py:9
  "tychora.adapters.voyage.query -> tychora.retrieval.cache",  # tychora/adapters/voyage/query.py:14
  "tychora.adapters.voyage.rerank -> tychora.application.reranking",  # tychora/adapters/voyage/rerank.py:24
  "tychora.adapters.voyage.runtime -> tychora.retrieval.cache",  # tychora/adapters/voyage/runtime.py:11
  "tychora.adapters.voyage.visual -> tychora.application.visual",  # tychora/adapters/voyage/visual.py:15
]

[[tool.importlinter.contracts]]
id = "storage-through-ports"
name = "Pipeline, configuration, ports and contracts reach storage only through ports"
type = "forbidden"
source_modules = [
  "tychora.contracts",
  "tychora.ports",
  "tychora.configuration",
  "tychora.ingestion",
  "tychora.indexing",
  "tychora.retrieval",
  "tychora.formatting",
]
forbidden_modules = ["tychora.adapters"]
# The fifteen pipeline-to-adapter edges, then the upward edges into tychora.application
# of contract "layers", whose chains continue from the composition root into adapters.
ignore_imports = [
  "tychora.contracts.documents -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/documents.py:304
  "tychora.contracts.extraction -> tychora.adapters.docling.ocr_evidence",  # tychora/contracts/extraction.py:217
  "tychora.contracts.extraction -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/extraction.py:193
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.corpus",  # tychora/indexing/coordinator.py:13
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.graph",  # tychora/indexing/coordinator.py:14
  "tychora.indexing.coordinator -> tychora.adapters.sqlite.publication",  # tychora/indexing/coordinator.py:15
  "tychora.ingestion.acquire -> tychora.adapters.code.capture",  # tychora/ingestion/acquire.py:12
  "tychora.ingestion.acquire -> tychora.adapters.files.cas",  # tychora/ingestion/acquire.py:13
  "tychora.ingestion.acquire -> tychora.adapters.files.references",  # tychora/ingestion/acquire.py:14
  "tychora.ingestion.document_parts -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_parts.py:7
  "tychora.ingestion.document_records -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_records.py:9
  "tychora.ingestion.document_views -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/document_views.py:8
  "tychora.ingestion.documents -> tychora.adapters.docling.ocr_evidence",  # tychora/ingestion/documents.py:8
  "tychora.retrieval.eligibility -> tychora.adapters.ladybug.traversal",  # tychora/retrieval/eligibility.py:118
  "tychora.retrieval.rerank -> tychora.adapters.voyage.rerank",  # tychora/retrieval/rerank.py:39
  "tychora.indexing.coordinator -> tychora.application.collection_policy",  # tychora/indexing/coordinator.py:16
  "tychora.ingestion.composition -> tychora.application.pdf_processing",  # tychora/ingestion/composition.py:271
  "tychora.ports.query -> tychora.application.owner",  # tychora/ports/query.py:14
  "tychora.retrieval.eligibility -> tychora.application.owner",  # tychora/retrieval/eligibility.py:140
  "tychora.retrieval.rerank -> tychora.application.owner",  # tychora/retrieval/rerank.py:40
  "tychora.retrieval.rerank -> tychora.application.reranking",  # tychora/retrieval/rerank.py:9
  "tychora.retrieval.segments -> tychora.application.setup",  # tychora/retrieval/segments.py:38
  "tychora.retrieval.visuals -> tychora.application.owner",  # tychora/retrieval/visuals.py:18
  "tychora.retrieval.visuals -> tychora.application.reranking",  # tychora/retrieval/visuals.py:8
]

[[tool.importlinter.contracts]]
id = "pure-contracts"
name = "Contracts import no frameworks, database clients or SDKs"
type = "forbidden"
source_modules = ["tychora.contracts"]
forbidden_modules = [
  "sqlite3",
  "fastapi",
  "typer",
  "rich",
  "chromadb",
  "ladybug",
  "voyageai",
  "anthropic",
  "docling",
  "mcp",
  "httpx",
]
# The three Docling adapter edges of contract "storage-through-ports"; every chain to a
# framework starts with one of them.
ignore_imports = [
  "tychora.contracts.documents -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/documents.py:304
  "tychora.contracts.extraction -> tychora.adapters.docling.ocr_evidence",  # tychora/contracts/extraction.py:217
  "tychora.contracts.extraction -> tychora.adapters.docling.text_coordinates",  # tychora/contracts/extraction.py:193
]
```

- [ ] **Step 3: Verify the contracts hold and catch a new upward import**

```bash
uv run --locked lint-imports
printf '"""Probe."""\n\nfrom tychora.application.owner import WorkspaceOwner\n' > src/tychora/contracts/_probe.py
uv run --locked lint-imports --no-cache; echo "exit=$?"
rm src/tychora/contracts/_probe.py
```

Expected: first run `Contracts: 4 kept, 0 broken.`; the probe run reports `tychora.contracts is not allowed to import tychora.application` and `exit=1`.

- [ ] **Step 4: State the dependency direction in the architecture page**

In `docs/architecture.md`, after the paragraph that follows the table of "## 1. Application layers and libraries" (the one ending "optional dependencies are declared in `pyproject.toml`."), insert:

```markdown
### Dependency direction

Four import-linter contracts in `pyproject.toml` (`[tool.importlinter]`) are the source of truth; `python -m tools.health check` runs them on every pull request.

1. **Layers.** Higher packages may import lower ones, never the reverse: `interfaces` and `clients` (independent of each other), then `api`, `evaluation`, `application`, then `retrieval`, `indexing`, `ingestion` and `formatting` (which may import each other), then `configuration`, `ports` and `contracts`. Evaluation sits above application because it drives the production query path.
2. **Adapters implement ports.** `adapters` never imports `application`, `retrieval`, `indexing`, `ingestion`, `evaluation`, `formatting`, `interfaces`, `clients` or `api`. The application layer and `api` are the composition root and may import adapters.
3. **Storage through ports.** `contracts`, `ports`, `configuration`, `ingestion`, `indexing`, `retrieval` and `formatting` never import `adapters`.
4. **Pure contracts.** `contracts` imports no database client, web framework, command-line library or model SDK.

Today's violations are listed in each contract's `ignore_imports`, one line per import with its file and line. The list may only shrink.
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml docs/architecture.md
git commit -m "build: encode the layering contracts with today's exceptions listed"
```

---

### Task 11: Dead code: vulture configuration and reviewed whitelist (S6)

Spec §3.5, §4.3 G12, §7. Every finding is triaged: removed, kept with a reason, or owned by a later sub-project.

**Files:**
- Modify: `pyproject.toml` (`[tool.vulture]`, `[tool.ruff] extend-exclude`)
- Create: `quality/vulture_whitelist.py`
- Modify: `src/tychora/evaluation/metrics.py:41-44` (remove `span_coverage`)

**Interfaces:**
- Produces: `uv run --locked vulture` with no arguments runs the configured paths (read by `tools/health/external.py::dead_code`, Task 14); whitelist size is the G12 ratchet.

- [ ] **Step 1: Configure vulture**

Append to `pyproject.toml`:

```toml
[tool.vulture]
paths = ["src/tychora", "quality/vulture_whitelist.py"]
min_confidence = 60
ignore_decorators = [
  "@field_validator",
  "@model_validator",
  "@field_serializer",
  "@model_serializer",
  "@computed_field",
  "@property",
  "@contextmanager",
  "@asynccontextmanager",
  "@app.*",
  "@router.*",
  "@*.command",
  "@*.callback",
  "@*.tool",
  "@*.resource",
  "@*.get",
  "@*.post",
  "@*.put",
  "@*.delete",
  "@*.patch",
  "@*.websocket",
  "@*.checks",
]
```

and extend `[tool.ruff]` (the whitelist is a vulture input of bare names, not importable Python):

```toml
[tool.ruff]
target-version = "py312"
extend-exclude = ["quality/vulture_whitelist.py"]
```

- [ ] **Step 2: See the findings**

```bash
mkdir -p quality && : > quality/vulture_whitelist.py
uv run --locked vulture | grep -v "unused variable"
```

Expected: 50 lines (functions, methods, classes, attributes), starting with `src/tychora/__init__.py:33: unused function '__getattr__'`.

- [ ] **Step 3: Remove the one confirmed dead function**

`span_coverage` in `src/tychora/evaluation/metrics.py` has no caller in `src/`, `tests/` or `docs/` (`git grep -n span_coverage` shows only its definition). Delete it:

```python
def span_coverage(
    expected: set[tuple[str, int, int]], returned: set[tuple[str, int, int]]
) -> float | None:
    return len(expected & returned) / len(expected) if expected else None
```

(and the blank lines around it, keeping two blank lines between the neighbours).

- [ ] **Step 4: Write the reviewed whitelist**

Create `quality/vulture_whitelist.py`:

```python
# Reviewed dead-code allow-list for vulture (gate G12). One name per line; the comment
# gives the reason and, for code a later sub-project calls or deletes, its owner.
# The number of entries may only fall; `python -m tools.health check` enforces it.

__getattr__  # public API: lazy attribute export of the tychora package (src/tychora/__init__.py:33)
_.context_spans  # LanguageAdapter port method implemented by the Python and TypeScript adapters
_.readinto  # file-object protocol method called by the standard library HTTP response reader
_.makefile  # socket protocol method called by http.client
UrlAcquisition  # URL acquisition adapter; engine core v2 wires it into source registration
_.hydrate  # CorpusStore port method; engine core v2 repository layer decides its callers
_.purge  # retention purge path (flow review C5); owned by engine core v2
_.purge_results  # query cache purge (flow review C5); owned by engine core v2
_.expire_diagnostics  # retention purge path (flow review C5); owned by engine core v2
_.record_review  # review-state write exercised by tests; engine core v2 repository layer owns it
_.raise_access_floor  # collection policy write exercised by tests; engine core v2 owns it
_.record_assertion_decision  # assertion decision write exercised by tests; engine core v2 owns it
_.is_pinned  # lease query exercised by tests; engine core v2 snapshot leases own it
_.migrate  # maintenance migration entry point; engine core v2 (new workspace format) owns it
_.reconcile  # recovery entry point exercised by tests; engine core v2 recovery owns it
_.prune_native  # explicit native index garbage collection; engine core v2 recovery owns it
_.rebuild_native  # native index rebuild; engine core v2 `manage rebuild` owns it
create_reranker  # reranker factory exercised by tests; model defaults (sub-project 3) owns provider construction
_.traverse  # public client method kb.relations.traverse (sync, async and HTTP clients)
_.discover_kinds  # registry query exercised by tests; kind ontology (sub-project 5) owns it
_.hint_contributions  # registry query exercised by tests; kind ontology (sub-project 5) owns it
import_inclusive_end  # date bound helper exercised by tests; kind ontology facets (sub-project 5) own it
DateInterval  # contract type exercised by tests; kind ontology facets (sub-project 5) own it
vector_record_id  # callers added by engine core v2 §4.4
new_family_nonce  # callers added by engine core v2 §4.4
repository_manifest  # code repository manifest exercised by tests; chunking v2 code ingestion owns it
process_memory  # benchmark helper exercised by tests; engine core v2 benchmark harness owns it
recall  # evaluation metric exercised by tests; kept beside the runner's typed metrics
_.binding_count  # evaluation provenance counter read by tests
graph_value_sha  # graph receipt digest exercised by tests; engine core v2 incremental graph writes own it
proxy_context_allowed  # visual proxy policy exercised by tests; Docling enrichment (sub-project 4) owns it
validate_document_source_parts  # document part validation exercised by tests; Docling enrichment owns it
reconstruct_rows  # table reconstruction exercised by tests; Docling enrichment (sub-project 4) owns it
selective_interpret  # vision selection exercised by tests; Docling enrichment (sub-project 4) owns it
_.should_exit  # uvicorn server flag set to stop the MCP HTTP server
Acquisition  # port protocol, implemented structurally by the acquisition adapters
UploadPolicy  # port protocol, implemented structurally by CanonicalUploadPolicy
LanguageAdapter  # port protocol, implemented by the Python and TypeScript code adapters
Reranker  # port protocol, implemented by the Voyage reranker
```

- [ ] **Step 5: Verify**

```bash
uv run --locked vulture | grep -v "unused variable"; echo "remaining=$?"
uv run --locked ruff check .
uv run --locked pytest tests/evaluation -q -n auto
```

Expected: no line printed (`remaining=1` from grep's empty match); ruff clean; evaluation tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml quality/vulture_whitelist.py src/tychora/evaluation/metrics.py
git commit -m "chore: triage dead code into a reviewed vulture whitelist"
```

---

### Task 12: Health checker core: metrics, baseline and ratchet rules

Spec §4.1, §4.5, §6.2. Pure Python, no tools invoked.

**Files:**
- Create: `tools/__init__.py`, `tools/health/__init__.py`, `tools/health/model.py`
- Test: `tools/health/tests/test_model.py`

**Interfaces:**
- Produces (used by every later checker task):
  - `Metric(name: str, value: float, direction: Literal["lower","higher"]="lower", kind: Literal["hard","ratchet"]="ratchet", per_key: dict[str, float] | None=None, tolerance: float=0.0, relative_tolerance: float=0.0, new_keys_allowed: bool=False, stale_on_improvement: bool=True)`
  - `Finding(metric: str, message: str, stale: bool=False)`
  - `compare(metric: Metric, entry: dict | None) -> list[Finding]`
  - `updated_entry(metric: Metric, entry: dict | None, commit: str, *, initial: bool) -> dict` (raises `UpdateRefused`)
  - `load_baseline(path: Path) -> dict[str, dict]`, `save_baseline(path: Path, metrics: dict[str, dict]) -> None`
  - constants `BASELINE = Path("quality/baseline.json")`, `UPDATE_HINT`
  - baseline file shape: `{"schema": 1, "metrics": {name: {"value", "direction", "measured_at", "per_key"?}}}`

- [ ] **Step 1: Create the packages**

`tools/__init__.py`:

```python
"""Repository maintenance tools that are not part of the tychora package."""
```

`tools/health/__init__.py`:

```python
"""Code-health gates, the ratchet checker and architecture reports."""
```

- [ ] **Step 2: Write the failing tests**

Create `tools/health/tests/test_model.py`:

```python
"""Ratchet rules: global and per-key regressions, new keys, stale baselines."""

import pytest

from tools.health.model import (
    Metric,
    UpdateRefused,
    compare,
    load_baseline,
    save_baseline,
    updated_entry,
)


def entry(value, per_key=None):
    data = {"value": value, "direction": "lower", "measured_at": "abc"}
    if per_key is not None:
        data["per_key"] = per_key
    return data


def test_hard_metric_fails_on_any_finding_and_ignores_the_baseline():
    assert compare(Metric("plan_references", 0, kind="hard"), None) == []
    [finding] = compare(Metric("plan_references", 2, kind="hard"), entry(5))
    assert "allows none" in finding.message
    assert not finding.stale


def test_global_regression_fails():
    [finding] = compare(Metric("raw_sql", 238), entry(237))
    assert finding.message == "238 is worse than baseline 237"
    assert not finding.stale


def test_higher_is_better_metric_fails_when_it_drops():
    metric = Metric("coverage_line", 85.0, direction="higher", tolerance=0.1)
    [finding] = compare(metric, {"value": 85.25, "measured_at": "abc"})
    assert "worse" in finding.message


def test_tolerance_absorbs_noise_in_both_directions():
    base = {"value": 85.25, "measured_at": "abc"}
    for value in (85.16, 85.25, 85.34):
        metric = Metric("coverage_line", value, direction="higher", tolerance=0.1)
        assert compare(metric, base) == []


def test_relative_tolerance_allows_fifteen_percent_slower_runs():
    metric = Metric(
        "test_runtime_seconds",
        1150,
        relative_tolerance=0.15,
        stale_on_improvement=False,
    )
    assert compare(metric, entry(1000)) == []
    assert compare(
        Metric("test_runtime_seconds", 1151, relative_tolerance=0.15), entry(1000)
    )
    faster = Metric(
        "test_runtime_seconds", 500, relative_tolerance=0.15, stale_on_improvement=False
    )
    assert compare(faster, entry(1000)) == []


def test_stale_baseline_fails_with_update_instruction():
    [finding] = compare(Metric("raw_sql", 230), entry(237))
    assert finding.stale
    assert "python -m tools.health update" in finding.message


def test_per_key_growth_fails_even_when_the_total_improves():
    metric = Metric("function_length", 2, per_key={"a.py::f": 120, "b.py::g": 81})
    findings = compare(
        metric, entry(3, {"a.py::f": 100, "b.py::g": 81, "c.py::h": 90})
    )
    messages = [f.message for f in findings if not f.stale]
    assert messages == ["a.py::f: 120 is worse than 100"]


def test_new_key_with_a_value_fails():
    metric = Metric("function_length", 1, per_key={"new.py::long": 90})
    [finding] = [f for f in compare(metric, entry(1, {})) if not f.stale]
    assert finding.message == "new.py::long: new entry with value 90"


def test_new_key_allowed_for_global_only_metrics():
    metric = Metric(
        "coverage_line_core",
        90,
        direction="higher",
        per_key={"new.py": 10},
        new_keys_allowed=True,
    )
    assert compare(metric, {"value": 90, "measured_at": "x", "per_key": {}}) == []


def test_removed_key_is_a_stale_improvement():
    metric = Metric("function_length", 0, per_key={})
    findings = compare(metric, entry(0, {"gone.py::f": 95}))
    assert [f.stale for f in findings] == [True]


def test_missing_entry_asks_for_an_update():
    [finding] = compare(Metric("raw_sql", 1), None)
    assert "no baseline entry" in finding.message


def test_update_records_improvements_and_keeps_unchanged_commit():
    improved = updated_entry(Metric("raw_sql", 230), entry(237), "new", initial=False)
    assert improved["value"] == 230
    assert improved["measured_at"] == "new"
    same = updated_entry(Metric("raw_sql", 237), entry(237), "new", initial=False)
    assert same["measured_at"] == "abc"


def test_update_refuses_a_regression():
    with pytest.raises(UpdateRefused, match="worse"):
        updated_entry(Metric("raw_sql", 238), entry(237), "new", initial=False)


def test_update_accepts_a_rename_but_not_a_new_long_function():
    renamed = Metric("function_length", 1, per_key={"a.py::new_name": 90})
    result = updated_entry(
        renamed, entry(1, {"a.py::old_name": 90}), "c", initial=False
    )
    assert result["per_key"] == {"a.py::new_name": 90}
    grown = Metric("function_length", 2, per_key={"a.py::f": 90, "a.py::g": 85})
    with pytest.raises(UpdateRefused, match="only renames"):
        updated_entry(
            grown, entry(2, {"a.py::f": 90, "b.py::x": 81}), "c", initial=False
        )


def test_update_without_baseline_requires_initial():
    with pytest.raises(UpdateRefused, match="--initial"):
        updated_entry(Metric("raw_sql", 1), None, "c", initial=False)
    assert updated_entry(Metric("raw_sql", 1), None, "c", initial=True)["value"] == 1


def test_hand_edited_worse_value_passes_only_when_it_matches_the_current_value():
    # A reviewer sees the hand edit in the diff; the checker cannot tell it apart.
    assert compare(Metric("raw_sql", 240), entry(240)) == []
    [finding] = compare(Metric("raw_sql", 239), entry(240))
    assert finding.stale


def test_baseline_round_trip_is_sorted_and_versioned(tmp_path):
    path = tmp_path / "quality" / "baseline.json"
    save_baseline(path, {"b": entry(1), "a": entry(2, {"z": 1, "y": 2})})
    assert load_baseline(path)["a"]["per_key"] == {"y": 2, "z": 1}
    assert path.read_text().index('"a"') < path.read_text().index('"b"')
    assert load_baseline(tmp_path / "missing.json") == {}
    path.write_text('{"schema": 2, "metrics": {}}')
    with pytest.raises(ValueError, match="schema"):
        load_baseline(path)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --locked pytest tools/health/tests/test_model.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'tools.health.model'`.

- [ ] **Step 4: Implement the model**

Create `tools/health/model.py`:

```python
"""Metrics, the committed baseline and the ratchet rules that compare them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Direction = Literal["lower", "higher"]
Kind = Literal["hard", "ratchet"]

BASELINE = Path("quality/baseline.json")
UPDATE_HINT = "run `python -m tools.health update` and commit quality/baseline.json"


@dataclass(frozen=True)
class Metric:
    """One measured quantity, optionally broken down per file, rule or function.

    A hard metric counts findings and must be zero. A ratchet metric is compared
    with its baseline entry: it may not get worse, and an improvement must be
    recorded in the baseline in the same change.
    """

    name: str
    value: float
    direction: Direction = "lower"
    kind: Kind = "ratchet"
    per_key: dict[str, float] | None = None
    tolerance: float = 0.0
    relative_tolerance: float = 0.0
    new_keys_allowed: bool = False
    stale_on_improvement: bool = True


@dataclass(frozen=True)
class Finding:
    """A regression, or an improvement the baseline has not recorded yet (stale)."""

    metric: str
    message: str
    stale: bool = False


class UpdateRefused(Exception):
    """The current values cannot replace the baseline without hiding a regression."""


def _worse(direction: Direction, current: float, base: float, slack: float) -> bool:
    if direction == "lower":
        return current > base + slack
    return current < base - slack


def _better(direction: Direction, current: float, base: float, slack: float) -> bool:
    if direction == "lower":
        return current < base - slack
    return current > base + slack


def _slack(metric: Metric, base: float) -> float:
    return max(metric.tolerance, abs(base) * metric.relative_tolerance)


def compare(metric: Metric, entry: dict[str, Any] | None) -> list[Finding]:
    """Apply the hard rule, or the global, per-key and stale-baseline rules."""
    if metric.kind == "hard":
        if metric.value:
            return [
                Finding(
                    metric.name, f"{metric.value:g} finding(s); this gate allows none"
                )
            ]
        return []
    if entry is None:
        return [Finding(metric.name, f"no baseline entry; {UPDATE_HINT}")]
    findings: list[Finding] = []
    base = float(entry["value"])
    slack = _slack(metric, base)
    if _worse(metric.direction, metric.value, base, slack):
        findings.append(
            Finding(metric.name, f"{metric.value:g} is worse than baseline {base:g}")
        )
    elif metric.stale_on_improvement and _better(
        metric.direction, metric.value, base, slack
    ):
        findings.append(
            Finding(
                metric.name,
                f"improved from {base:g} to {metric.value:g}; {UPDATE_HINT}",
                stale=True,
            )
        )
    findings.extend(_compare_keys(metric, entry.get("per_key") or {}))
    return findings


def _compare_keys(metric: Metric, base_keys: dict[str, float]) -> list[Finding]:
    if metric.per_key is None:
        return []
    findings: list[Finding] = []
    for key, value in sorted(metric.per_key.items()):
        if key not in base_keys:
            if not metric.new_keys_allowed and _worse(metric.direction, value, 0, 0):
                findings.append(
                    Finding(metric.name, f"{key}: new entry with value {value:g}")
                )
            continue
        base = float(base_keys[key])
        if _worse(metric.direction, value, base, metric.tolerance):
            findings.append(
                Finding(metric.name, f"{key}: {value:g} is worse than {base:g}")
            )
        elif metric.stale_on_improvement and _better(
            metric.direction, value, base, metric.tolerance
        ):
            findings.append(
                Finding(
                    metric.name,
                    f"{key}: improved from {base:g} to {value:g}; {UPDATE_HINT}",
                    stale=True,
                )
            )
    if metric.stale_on_improvement and metric.direction == "lower":
        for key in sorted(set(base_keys) - set(metric.per_key)):
            if float(base_keys[key]) > 0:
                findings.append(
                    Finding(
                        metric.name,
                        f"{key}: no longer present; {UPDATE_HINT}",
                        stale=True,
                    )
                )
    return findings


def updated_entry(
    metric: Metric, entry: dict[str, Any] | None, commit: str, *, initial: bool
) -> dict[str, Any]:
    """Return the baseline entry after recording improvements, or refuse."""
    if entry is None:
        if not initial:
            raise UpdateRefused(
                f"{metric.name}: no baseline entry; run `update --initial` once"
            )
        return _entry(metric, metric.value, metric.per_key, commit)
    base = float(entry["value"])
    slack = _slack(metric, base)
    if _worse(metric.direction, metric.value, base, slack):
        raise UpdateRefused(f"{metric.name}: {metric.value:g} is worse than {base:g}")
    improved = metric.stale_on_improvement and _better(
        metric.direction, metric.value, base, slack
    )
    value = metric.value if improved else base
    per_key = None
    if metric.per_key is not None:
        per_key = _updated_keys(metric, entry.get("per_key") or {})
    changed = value != base or per_key != entry.get("per_key")
    return _entry(metric, value, per_key, commit if changed else entry["measured_at"])


def _updated_keys(metric: Metric, base_keys: dict[str, float]) -> dict[str, float]:
    current = metric.per_key or {}
    kept: dict[str, float] = {}
    for key, value in current.items():
        if key not in base_keys:
            continue
        base = float(base_keys[key])
        if _worse(metric.direction, value, base, metric.tolerance):
            raise UpdateRefused(
                f"{metric.name}: {key} {value:g} is worse than {base:g}"
            )
        better = _better(metric.direction, value, base, metric.tolerance)
        kept[key] = value if better else base
    added = {key: value for key, value in current.items() if key not in base_keys}
    removed = {key: float(v) for key, v in base_keys.items() if key not in current}
    if (
        metric.direction == "lower"
        and not metric.new_keys_allowed
        and sum(added.values()) > sum(removed.values())
    ):
        raise UpdateRefused(
            f"{metric.name}: new entries {sorted(added)} outweigh the removed "
            f"ones {sorted(removed)}; only renames and moves can be recorded"
        )
    kept.update(added)
    return dict(sorted(kept.items()))


def _entry(
    metric: Metric, value: float, per_key: dict[str, float] | None, commit: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "value": value,
        "direction": metric.direction,
        "measured_at": commit,
    }
    if per_key is not None:
        entry["per_key"] = dict(sorted(per_key.items()))
    return entry


def load_baseline(path: Path) -> dict[str, dict[str, Any]]:
    """Read quality/baseline.json; a missing file is an empty baseline."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("schema") != 1:
        raise ValueError(f"{path}: unsupported baseline schema {data.get('schema')!r}")
    return data["metrics"]


def save_baseline(path: Path, metrics: dict[str, dict[str, Any]]) -> None:
    """Write the baseline with sorted keys so every update is a readable diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "metrics": metrics}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
```

- [ ] **Step 5: Run the tests and the static checks**

```bash
uv run --locked pytest tools/health/tests/test_model.py -q
uv run --locked ruff format tools && uv run --locked ruff check tools && uv run --locked ty check tools
```

Expected: 17 passed; ruff and ty clean.

- [ ] **Step 6: Commit**

```bash
git add tools
git commit -m "feat(health): ratchet model comparing metrics with the committed baseline"
```

---

### Task 13: Static measures and the import graph

Spec §4.3 G5, G8, G14 (hard part), G15, G23, G24, G26, G27; §3.9 coupling. AST and Git only.

**Files:**
- Create: `tools/health/static.py`, `tools/health/graph.py`
- Test: `tools/health/tests/test_static.py`

**Interfaces:**
- Consumes: `Metric` (Task 12).
- Produces: each measure has signature `(root: Path) -> list[Metric]`:
  - `static.function_length` → `function_length` (per key `path::qualified.name`, value = lines > 80)
  - `static.raw_sql` → `raw_sql_outside_adapters` (per file)
  - `static.integrity_raises` → `integrity_raises_without_reason` (per file); `static.by_package(per_key) -> dict[str, float]`
  - `static.plan_references(root, files: list[str] | None = None)` → hard `plan_references`
  - `static.public_api_docstrings` → hard `public_api_docstrings`
  - `static.quarantined_tests` → `quarantined_tests`
  - `graph.module_level_cycles` → hard `module_level_cycles`; `graph.all_import_cycles` → `modules_in_import_cycles`
  - helpers reused in Task 18: `static.python_files(root, base=Path("src/tychora"))`, `static.relative(root, path)`, `graph.import_graph(root, *, top_level_only) -> dict[str, set[str]]`, `graph.strongly_connected(graph) -> list[list[str]]`, `graph.component(module) -> str`, `graph.module_name(root, path) -> str`, `graph.coupling(graph) -> dict[str, tuple[int, int]]`

- [ ] **Step 1: Write the failing tests**

Create `tools/health/tests/test_static.py`:

```python
"""AST and text measures over a throwaway source tree."""

import subprocess
from pathlib import Path

from tools.health import graph, static


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def long_function(name: str, lines: int) -> str:
    body = "".join(f"    x{i} = {i}\n" for i in range(lines - 2))
    return f"def {name}():\n{body}    return 0\n"


def test_function_length_keys_long_functions_by_qualified_name(tmp_path):
    write(tmp_path, "src/tychora/a.py", long_function("short", 80) + long_function("long", 81))
    write(
        tmp_path,
        "src/tychora/b.py",
        "class Owner:\n"
        + "".join("    " + line + "\n" for line in long_function("open", 90).splitlines()),
    )
    [metric] = static.function_length(tmp_path)
    assert metric.per_key == {
        "src/tychora/a.py::long": 81,
        "src/tychora/b.py::Owner.open": 90,
    }
    assert metric.value == 2


def test_duplicate_qualified_names_get_a_suffix(tmp_path):
    write(tmp_path, "src/tychora/a.py", long_function("f", 81) + long_function("f", 82))
    [metric] = static.function_length(tmp_path)
    assert metric.per_key == {"src/tychora/a.py::f": 81, "src/tychora/a.py::f#2": 82}


def test_raw_sql_counts_calls_outside_adapters_only(tmp_path):
    write(
        tmp_path,
        "src/tychora/application/q.py",
        "db.execute('x')\ndb.executemany('y', [])\nrun()\n",
    )
    write(tmp_path, "src/tychora/adapters/sqlite/s.py", "db.execute('x')\n")
    [metric] = static.raw_sql(tmp_path)
    assert metric.per_key == {"src/tychora/application/q.py": 2}


def test_integrity_raises_without_reason(tmp_path):
    write(
        tmp_path,
        "src/tychora/application/x.py",
        "raise TychoraError('INTEGRITY_ERROR', 'a')\n"
        "raise errors.TychoraError('INTEGRITY_ERROR', 'b', reason='stale_manifest')\n"
        "raise TychoraError('NOT_FOUND', 'c')\n",
    )
    [metric] = static.integrity_raises(tmp_path)
    assert metric.value == 1
    assert metric.per_key is not None
    assert static.by_package(metric.per_key) == {"application": 1}


def test_plan_references_in_text_and_file_names(tmp_path):
    write(tmp_path, "src/tychora/a.py", '"""Bind Task19 ownership."""\n')
    write(tmp_path, "dashboard/e2e/task5.spec.ts", "test('x', () => {});\n")
    write(tmp_path, "docs/superpowers/plans/p.md", "Task 7\n")
    write(tmp_path, "tests/ok.py", "multitask 5 is not a reference\n")
    files = [
        "src/tychora/a.py",
        "dashboard/e2e/task5.spec.ts",
        "docs/superpowers/plans/p.md",
        "tests/ok.py",
    ]
    [metric] = static.plan_references(tmp_path, files)
    assert metric.per_key == {"src/tychora/a.py": 1, "dashboard/e2e/task5.spec.ts": 1}
    assert metric.kind == "hard"


def test_plan_references_reads_tracked_files(tmp_path):
    write(tmp_path, "tests/t.py", "# Task 3 leftover\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    [metric] = static.plan_references(tmp_path)
    assert metric.value == 1


def test_public_api_docstrings_include_methods_of_private_resource_classes(tmp_path):
    write(
        tmp_path,
        "src/tychora/api.py",
        '"""API."""\n\nclass LocalWorkspace:\n    """Doc."""\n    def close(self):\n        pass\n',
    )
    write(
        tmp_path,
        "src/tychora/clients/sync.py",
        "class _Sources:\n    def add(self):\n        def inner():\n            pass\n"
        "    def _private(self):\n        pass\n",
    )
    [metric] = static.public_api_docstrings(tmp_path)
    assert set(metric.per_key or {}) == {
        "src/tychora/api.py::LocalWorkspace.close",
        "src/tychora/clients/sync.py::_Sources.add",
    }


def test_quarantined_tests_are_counted(tmp_path):
    write(
        tmp_path,
        "tests/t.py",
        "import pytest\n@pytest.mark.quarantine(reason='r', issue='1')\ndef test_x():\n    pass\n",
    )
    [metric] = static.quarantined_tests(tmp_path)
    assert metric.value == 1


def test_cycles_split_module_level_and_function_body_imports(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(tmp_path, "src/tychora/a.py", "from tychora import b\n")
    write(tmp_path, "src/tychora/b.py", "def f():\n    from tychora.a import x\n")
    write(tmp_path, "src/tychora/c.py", "from . import d\n")
    write(tmp_path, "src/tychora/d.py", "from .c import y\n")
    [hard] = graph.module_level_cycles(tmp_path)
    assert hard.per_key == {"tychora.c": 1, "tychora.d": 1}
    [ratchet] = graph.all_import_cycles(tmp_path)
    assert ratchet.value == 4


def test_coupling_counts_distinct_components(tmp_path):
    write(tmp_path, "src/tychora/__init__.py", "")
    write(tmp_path, "src/tychora/contracts/__init__.py", "")
    write(tmp_path, "src/tychora/contracts/ids.py", "")
    write(tmp_path, "src/tychora/application/__init__.py", "")
    write(
        tmp_path,
        "src/tychora/application/owner.py",
        "from tychora.contracts.ids import x\n",
    )
    write(tmp_path, "src/tychora/adapters/__init__.py", "")
    write(tmp_path, "src/tychora/adapters/sqlite/__init__.py", "")
    write(
        tmp_path,
        "src/tychora/adapters/sqlite/corpus.py",
        "from tychora.application.owner import y\n",
    )
    coupling = graph.coupling(graph.import_graph(tmp_path, top_level_only=False))
    assert coupling["contracts"] == (1, 0)
    assert coupling["application"] == (1, 1)
    assert coupling["adapters.sqlite"] == (0, 1)
```

Run: `uv run --locked pytest tools/health/tests/test_static.py -q`
Expected: `ModuleNotFoundError: No module named 'tools.health.static'`.

- [ ] **Step 2: Implement `tools/health/static.py`**

```python
"""AST and text measurements: function length, raw SQL, integrity raises, docstrings, plan references."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from tools.health.model import Metric

SOURCE = Path("src/tychora")
FUNCTION_LINE_LIMIT = 80
SQL_METHODS = frozenset({"execute", "executemany", "executescript"})
PLAN_REFERENCE = re.compile(r"\b[Tt]ask ?[0-9]+\b")
PLAN_TEXT_ROOTS = frozenset({"src", "tests", "dashboard"})
PLAN_SKIPPED = ("docs/superpowers/", "dashboard/package-lock.json")
PUBLIC_API = ("src/tychora/api.py", "src/tychora/clients")

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def python_files(root: Path, base: Path = SOURCE) -> list[Path]:
    """Every Python file under base, in a stable order."""
    return sorted(
        path for path in (root / base).rglob("*.py") if "__pycache__" not in path.parts
    )


def relative(root: Path, path: Path) -> str:
    """POSIX path of path relative to root, used as a per-key name."""
    return path.relative_to(root).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def functions(tree: ast.AST) -> Iterator[tuple[str, FunctionNode]]:
    """Yield (qualified name, node) for every function, nested ones included."""

    def visit(node: ast.AST, prefix: str) -> Iterator[tuple[str, FunctionNode]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = prefix + child.name
                yield name, child
                yield from visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    seen: dict[str, int] = {}
    for name, node in visit(tree, ""):
        seen[name] = seen.get(name, 0) + 1
        yield (name if seen[name] == 1 else f"{name}#{seen[name]}"), node


def function_length(root: Path) -> list[Metric]:
    """G5: functions longer than 80 physical lines, keyed by path::qualified name."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        for name, node in functions(_parse(path)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > FUNCTION_LINE_LIMIT:
                per_key[f"{relative(root, path)}::{name}"] = length
    return [Metric("function_length", len(per_key), per_key=per_key)]


def raw_sql(root: Path) -> list[Metric]:
    """G8: .execute/.executemany/.executescript calls outside tychora/adapters."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        rel = relative(root, path)
        if rel.startswith("src/tychora/adapters/"):
            continue
        count = sum(
            1
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_METHODS
        )
        if count:
            per_key[rel] = count
    return [Metric("raw_sql_outside_adapters", sum(per_key.values()), per_key=per_key)]


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def integrity_raises(root: Path) -> list[Metric]:
    """G24: TychoraError("INTEGRITY_ERROR", ...) constructions without reason=."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        count = 0
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and _called_name(node.func) == "TychoraError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "INTEGRITY_ERROR"
                and not any(keyword.arg == "reason" for keyword in node.keywords)
            ):
                count += 1
        if count:
            per_key[relative(root, path)] = count
    total = sum(per_key.values())
    print(f"[G24] reason-less integrity raises by package: {by_package(per_key)}")
    return [Metric("integrity_raises_without_reason", total, per_key=per_key)]


def by_package(per_key: dict[str, float]) -> dict[str, float]:
    """Sum per-file counts by top-level tychora package, for the report."""
    totals: dict[str, float] = {}
    for key, value in per_key.items():
        parts = key.split("/")
        package = parts[2].removesuffix(".py") if len(parts) > 2 else key
        totals[package] = totals.get(package, 0) + value
    return dict(sorted(totals.items()))


def tracked_files(root: Path) -> list[str]:
    """Files Git tracks, so ignored build output never counts."""
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [name for name in output.decode().split("\0") if name]


def plan_references(root: Path, files: list[str] | None = None) -> list[Metric]:
    """G15: plan-task references in source, test and dashboard text and in file names."""
    per_key: dict[str, float] = {}
    for rel in tracked_files(root) if files is None else files:
        if rel.startswith(PLAN_SKIPPED):
            continue
        hits = 1 if PLAN_REFERENCE.search(Path(rel).name) else 0
        if rel.split("/", 1)[0] in PLAN_TEXT_ROOTS and (root / rel).is_file():
            try:
                text = (root / rel).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = ""
            hits += sum(1 for line in text.splitlines() if PLAN_REFERENCE.search(line))
        if hits:
            per_key[rel] = hits
    return [
        Metric("plan_references", sum(per_key.values()), kind="hard", per_key=per_key)
    ]


def _public_definitions(
    node: ast.AST, prefix: str = ""
) -> Iterator[tuple[str, ast.ClassDef | FunctionNode]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield prefix + child.name, child
            yield from _public_definitions(child, f"{prefix}{child.name}.")
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield prefix + child.name, child


def public_api_docstrings(root: Path) -> list[Metric]:
    """G14 (hard part): every public class, function and method of api.py and clients/.

    Methods of underscore-named resource classes count too: `kb.sources.add`
    is public even though its class is `_Sources`.
    """
    paths = [root / PUBLIC_API[0], *sorted((root / PUBLIC_API[1]).glob("*.py"))]
    missing: dict[str, float] = {}
    for path in paths:
        for name, node in _public_definitions(_parse(path)):
            if node.name.startswith("_"):
                continue
            if ast.get_docstring(node) is None:
                missing[f"{relative(root, path)}::{name}"] = 1
    return [Metric("public_api_docstrings", len(missing), kind="hard", per_key=missing)]


def quarantined_tests(root: Path) -> list[Metric]:
    """G23: tests carrying @pytest.mark.quarantine; the count may only fall."""
    per_key: dict[str, float] = {}
    for path in python_files(root, Path("tests")):
        count = sum(
            1
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Attribute)
            and node.attr == "quarantine"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        )
        if count:
            per_key[relative(root, path)] = count
    return [Metric("quarantined_tests", sum(per_key.values()), per_key=per_key)]
```

- [ ] **Step 3: Implement `tools/health/graph.py`**

```python
"""Module import graph of src/tychora, its cycles and its component coupling."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from tools.health.model import Metric
from tools.health.static import SOURCE, python_files

Graph = dict[str, set[str]]


def module_name(root: Path, path: Path) -> str:
    """Dotted module name of a file under src/, packages named without __init__."""
    parts = list(path.relative_to(root / SOURCE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _targets(node: ast.Import | ast.ImportFrom, package: list[str]) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        base = package[: len(package) - node.level + 1]
        module = ".".join([*base, *([node.module] if node.module else [])])
    else:
        module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def import_graph(root: Path, *, top_level_only: bool) -> Graph:
    """Edges between tychora modules; top_level_only keeps module-body imports."""
    trees: dict[str, tuple[ast.Module, list[str]]] = {}
    for path in python_files(root):
        name = module_name(root, path)
        parts = name.split(".")
        package = parts if path.name == "__init__.py" else parts[:-1]
        trees[name] = (ast.parse(path.read_text(encoding="utf-8")), package)
    known = set(trees)

    def resolve(target: str) -> str | None:
        while target and target not in known:
            target = target.rpartition(".")[0]
        return target or None

    graph: Graph = {name: set() for name in trees}
    for name, (tree, package) in trees.items():
        nodes = tree.body if top_level_only else list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                for target in _targets(node, package):
                    if not target.startswith("tychora"):
                        continue
                    resolved = resolve(target)
                    if resolved and resolved != name:
                        graph[name].add(resolved)
    return graph


def strongly_connected(graph: Graph) -> list[list[str]]:
    """Tarjan's algorithm without recursion; returns components of two or more."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        result.append(sorted(component))
    return sorted(result, key=lambda c: (-len(c), c))


def module_level_cycles(root: Path) -> list[Metric]:
    """G26: modules in cycles through module-body imports; must stay zero."""
    components = strongly_connected(import_graph(root, top_level_only=True))
    per_key = {member: 1.0 for component in components for member in component}
    return [Metric("module_level_cycles", len(per_key), kind="hard", per_key=per_key)]


def all_import_cycles(root: Path) -> list[Metric]:
    """G27: modules in cycles when function-body imports count too."""
    components = strongly_connected(import_graph(root, top_level_only=False))
    return [Metric("modules_in_import_cycles", sum(len(c) for c in components))]


def component(module: str) -> str:
    """Top-level package; adapters.* and interfaces.* are split by child."""
    parts = module.split(".")
    if len(parts) < 2:
        return "tychora"
    if parts[1] in {"adapters", "interfaces"} and len(parts) > 3:
        return f"{parts[1]}.{parts[2]}"
    return parts[1]


def coupling(graph: Graph) -> dict[str, tuple[int, int]]:
    """Afferent and efferent coupling (Ca, Ce) per component."""
    efferent: dict[str, set[str]] = defaultdict(set)
    afferent: dict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            a, b = component(source), component(target)
            if a != b:
                efferent[a].add(b)
                afferent[b].add(a)
    names = {component(module) for module in graph}
    return {name: (len(afferent[name]), len(efferent[name])) for name in sorted(names)}
```

- [ ] **Step 4: Run the tests and measure the real tree**

```bash
uv run --locked pytest tools/health/tests -q
uv run --locked python - <<'EOF'
from pathlib import Path
from tools.health import graph, static
for measure in (static.function_length, static.raw_sql, static.integrity_raises,
                static.plan_references, static.public_api_docstrings,
                static.quarantined_tests, graph.module_level_cycles, graph.all_import_cycles):
    for metric in measure(Path(".")):
        print(metric.name, metric.value)
EOF
```

Expected: tests pass (10 new). On the reference copy before Tasks 7 and 9 the values were `function_length 158`, `raw_sql_outside_adapters 240`, `integrity_raises_without_reason 380`, `plan_references 125`, `public_api_docstrings 65`, `quarantined_tests 0`, `module_level_cycles 0`, `modules_in_import_cycles 76`; after Tasks 7 and 9 `plan_references` and `public_api_docstrings` are 0.

- [ ] **Step 5: Commit**

```bash
git add tools/health/static.py tools/health/graph.py tools/health/tests/test_static.py
git commit -m "feat(health): AST measures for size, SQL, integrity raises, docstrings and import cycles"
```

---

### Task 14: Tool-backed and report-backed measures, the advisory register and jscpd configuration

Spec §4.3 G1–G4, G6, G7, G9–G14, G16–G22, G25, G28; §8 R1. Each runner calls one tool; each parser is a pure function tested with recorded output.

**Files:**
- Create: `tools/health/external.py`, `tools/health/layering.py`, `tools/health/advisories.py`, `tools/health/testruns.py`
- Create: `.jscpd.json`, `quality/advisories.toml`
- Test: `tools/health/tests/test_parsers.py`
- Modify (only if Chroma 1.6.0 is on PyPI, Step 7): `pyproject.toml`, `uv.lock`, `quality/advisories.toml`, `HANDOFF.md`

**Interfaces:**
- Consumes: `Metric` (Task 12); `format:check`, `typecheck` npm scripts (Tasks 1, 8); `[tool.importlinter]` (Task 10); `[tool.vulture]` and the whitelist (Task 11); dashboard binaries `jscpd`, `knip`, `madge` (Task 8).
- Produces (all `(root: Path) -> list[Metric]`): `external.lint`, `format_check`, `types`, `size_rules`, `body_imports`, `docstrings`, `complexity`, `duplication`, `dead_code`, `dashboard_dead_code`, `dashboard_cycles`; `layering.layering`; `advisories.advisories`, `advisories.lock_freshness`; `testruns.line_coverage`, `branch_coverage`, `dashboard_coverage`, `clean_suite`, `runtime`, `mutation`, `gold_set`. Also `external.MissingReport`, `external.run(command, cwd, env=None)`, `external.venv_tool(name)`, `external.read_report(path)`, `external.JSCPD_SCOPES`, `external.jscpd_command(root, scope, *, update=False)`, `external.complexity_from_json(items)`, `testruns.flaky_tests(reports: list[str]) -> list[str]`, `testruns.CORE_MODULES`.
- Report files read (written by CI steps in Tasks 16 and 20): `reports/coverage.json`, `reports/coverage-branch.json`, `dashboard/coverage/coverage-summary.json`, `reports/junit.xml`, `reports/clean-junit.xml`, `reports/mutmut-results.txt`, `reports/code-gold.json`; jscpd writes `reports/jscpd/<scope>.json`.

- [ ] **Step 1: Write the failing parser tests**

Create `tools/health/tests/test_parsers.py`:

```python
"""Recorded tool output reduced to metrics, without running the tools."""

import datetime as dt
import json
from pathlib import Path

import pytest

from tools.health import advisories, external, layering, testruns

ROOT = Path("/repo")


def test_ruff_counts_per_file_and_rule():
    output = json.dumps(
        [
            {"filename": "/repo/src/tychora/a.py", "code": "PLR0913"},
            {"filename": "/repo/src/tychora/a.py", "code": "PLR0913"},
            {"filename": "/repo/src/tychora/b.py", "code": "PLR0915"},
        ]
    )
    assert external.ruff_counts(output, ROOT, by_rule=True) == {
        "src/tychora/a.py::PLR0913": 2,
        "src/tychora/b.py::PLR0915": 1,
    }
    assert external.ruff_counts(output, ROOT, by_rule=False) == {
        "src/tychora/a.py": 2,
        "src/tychora/b.py": 1,
    }


def test_complexity_keeps_functions_above_fifteen():
    items = [
        {"path": "src/tychora/v.py", "function_name": "validate_bundle", "complexity": 240},
        {"path": "src/tychora/v.py", "function_name": "small", "complexity": 15},
        {"path": "src/tychora/v.py", "function_name": "validate_bundle", "complexity": 16},
    ]
    metric = external.complexity_from_json(items)
    assert metric.per_key == {
        "src/tychora/v.py::validate_bundle": 240,
        "src/tychora/v.py::validate_bundle#2": 16,
    }


def test_jscpd_report_gives_percentage_and_new_clones():
    report = {"statistics": {"total": {"percentage": 1.321182, "newClones": 1}}}
    duplication, new = external.jscpd_metrics("python", report)
    assert (duplication.name, duplication.value) == ("duplication_python", 1.32)
    assert (new.name, new.value, new.kind) == ("new_clones_python", 1, "hard")
    assert [m.name for m in external.jscpd_metrics("tests", report)] == [
        "duplication_tests"
    ]


def test_vulture_ignores_unused_variables():
    output = (
        "src/tychora/a.py:3: unused function 'old' (60% confidence)\n"
        "src/tychora/a.py:9: unused variable 'field' (60% confidence)\n"
        "src/tychora/b.py:1: unused method 'purge' (60% confidence)\n"
    )
    assert external.vulture_findings(output) == {
        "src/tychora/a.py::old": 1,
        "src/tychora/b.py::purge": 1,
    }
    assert external.whitelist_size("# header\n\nfoo  # reason\n_.bar  # reason\n") == 2


def test_knip_counts_every_listed_finding():
    report = {
        "issues": [
            {"file": "src/pages/Sources.tsx", "files": [{"name": "src/pages/Sources.tsx"}], "exports": []},
            {"file": "package.json", "devDependencies": [{"name": "a"}, {"name": "b"}]},
            {"file": "src/api/contracts.ts", "types": [{"name": "Geometry"}], "exports": [{"name": "x"}]},
        ]
    }
    assert external.knip_count(report) == 5


def test_layering_exception_counts_per_contract():
    pyproject = {
        "tool": {
            "importlinter": {
                "contracts": [
                    {"id": "layers", "ignore_imports": ["a -> b", "c -> d"]},
                    {"id": "pure-contracts"},
                ]
            }
        }
    }
    assert layering.exception_counts(pyproject) == {"layers": 2, "pure-contracts": 0}


def coverage_report(covered_lines, branches=(1, 2)):
    files = {
        f"src/{module}": {
            "summary": {
                "covered_lines": 9,
                "num_statements": 10,
                "covered_branches": branches[0],
                "num_branches": branches[1],
            }
        }
        for module in testruns.CORE_MODULES
    }
    totals = {
        "covered_lines": covered_lines,
        "num_statements": 1000,
        "covered_branches": 700,
        "num_branches": 1000,
    }
    return {"totals": totals, "files": files}


def test_line_and_branch_coverage_with_core_module_floors():
    [line] = testruns.coverage_metrics(coverage_report(852), branch=False)
    assert (line.name, line.value, line.direction) == ("coverage_line", 85.2, "higher")
    assert line.per_key and line.per_key["tychora/contracts/identity.py"] == 90.0
    [branch] = testruns.coverage_metrics(coverage_report(852), branch=True)
    assert (branch.name, branch.value) == ("coverage_branch", 70.0)
    assert branch.per_key and branch.per_key["tychora/retrieval/fusion.py"] == 50.0


def test_missing_core_module_is_an_error_not_a_zero():
    report = coverage_report(1)
    del report["files"]["src/tychora/retrieval/fusion.py"]
    with pytest.raises(KeyError, match="fusion"):
        testruns.coverage_metrics(report, branch=False)


JUNIT = """<testsuites><testsuite name="pytest" time="221.5">
<testcase classname="tests.a" name="test_ok" time="1.0"/>
<testcase classname="tests.a" name="test_fail" time="40.2"><failure message="x"/></testcase>
<testcase classname="tests.b" name="test_skip" time="0.1"><skipped message="requires TYCHORA_TEST_X"/></testcase>
</testsuite></testsuites>"""


def test_junit_outcomes_runtime_and_slowest():
    assert testruns.outcomes(JUNIT) == {
        "tests.a::test_ok": "passed",
        "tests.a::test_fail": "failed",
        "tests.b::test_skip": "skipped",
    }
    assert testruns.suite_seconds(JUNIT) == 221.5
    assert testruns.slowest(JUNIT, 1) == [(40.2, "tests.a::test_fail")]


def test_flaky_tests_differ_between_seeds():
    other = JUNIT.replace('<failure message="x"/>', "")
    assert testruns.flaky_tests([JUNIT, other, JUNIT]) == ["tests.a::test_fail"]
    assert testruns.flaky_tests([JUNIT, JUNIT]) == []


def test_mutation_scores_exclude_documented_equivalents():
    results = (
        "    tychora.contracts.identity.x_digest__mutmut_1: killed\n"
        "    tychora.contracts.identity.x_digest__mutmut_2: survived\n"
        "    tychora.contracts.identity.x_canonical_bytes__mutmut_3: survived\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_1: timeout\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_2: no tests\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_3: not checked\n"
    )
    equivalent = {"tychora.contracts.identity.x_canonical_bytes__mutmut_3"}
    assert testruns.mutation_scores(results, equivalent) == {
        "tychora.contracts.identity": 50.0,
        "tychora.retrieval.fusion": 50.0,
    }


def test_missing_report_names_the_file(tmp_path):
    with pytest.raises(external.MissingReport, match="reports/junit.xml"):
        testruns.runtime(tmp_path)


def test_gold_report_is_keyed_by_variant_and_metric(tmp_path):
    report = tmp_path / "reports" / "code-gold.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps({"variants": {"lexical": {"file_recall": 0.5, "file_mrr": 0.4}}})
    )
    [metric] = testruns.gold_set(tmp_path)
    assert metric.value == 0.5
    assert metric.per_key == {"lexical::file_recall": 0.5, "lexical::file_mrr": 0.4}


REGISTER = """
[[advisory]]
id = "PYSEC-2026-311"
package = "chromadb"
reason = "Not reachable from the embedded PersistentClient."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15
"""


def test_advisory_register_lists_unlisted_expired_and_obsolete_entries():
    waivers = advisories.load_register(REGISTER)
    reported = {"PYSEC-2026-311": "chromadb", "GHSA-new": "fastapi"}
    today = dt.date(2026, 10, 20)
    assert advisories.advisory_findings(reported, waivers, today) == {
        "fastapi:GHSA-new: not in quality/advisories.toml": 1
    }
    later = dt.date(2026, 11, 16)
    assert "chromadb:PYSEC-2026-311: waiver expired 2026-11-15" in advisories.advisory_findings(
        {"PYSEC-2026-311": "chromadb"}, waivers, later
    )
    assert advisories.advisory_findings({}, waivers, today) == {
        "PYSEC-2026-311: no longer reported; remove it from quality/advisories.toml": 1
    }


def test_waiver_longer_than_thirty_days_is_rejected():
    with pytest.raises(ValueError, match="at most 30 days"):
        advisories.load_register(REGISTER.replace("2026-11-15", "2026-12-15"))


def test_pip_audit_and_npm_audit_parsing():
    pip = {
        "dependencies": [
            {"name": "chromadb", "version": "1.5.9", "vulns": [{"id": "PYSEC-2026-311"}]},
            {"name": "x", "vulns": []},
        ]
    }
    assert advisories.reported_advisories(pip) == {"PYSEC-2026-311": "chromadb"}
    assert advisories.npm_vulnerabilities({"metadata": {"vulnerabilities": {"total": 0}}}) == 0
```

Run: `uv run --locked pytest tools/health/tests/test_parsers.py -q`
Expected: `ModuleNotFoundError: No module named 'tools.health.advisories'`.

- [ ] **Step 2: Implement `tools/health/external.py`**

```python
"""Gates backed by external tools: each runner calls the tool, each parser reduces its output."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health.model import Metric

COMPLEXITY_LIMIT = 15
JSCPD_SCOPES: dict[str, tuple[str, list[str]]] = {
    "python": ("python", ["src/tychora"]),
    "typescript": ("typescript,tsx", ["dashboard/src", "dashboard/e2e"]),
    "tests": ("python", ["tests"]),
}
VULTURE_LINE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+): unused "
    r"(?P<kind>function|method|class|property|attribute|import) '(?P<name>[^']+)'"
)


class MissingReport(RuntimeError):
    """A report file a gate reads was not produced by the step that should write it."""


def venv_tool(name: str) -> str:
    """Console script installed next to the running interpreter."""
    return str(Path(sys.executable).parent / name)


def run(
    command: list[str], root: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a tool from the given directory, capturing its output as text."""
    return subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def read_report(path: Path) -> str:
    """Read a report written by an earlier CI step, or fail naming the file."""
    if not path.is_file():
        raise MissingReport(
            f"{path} is missing; run the step that writes it (see .github/workflows)"
        )
    return path.read_text(encoding="utf-8")


def command_gate(name: str, commands: list[list[str]], root: Path) -> Metric:
    """Hard gate: the number of commands that exit non-zero (output is printed)."""
    failures = 0
    for command in commands:
        result = run(command, root)
        if result.returncode != 0:
            failures += 1
            print(
                f"[{name}] {' '.join(command)} failed:\n{result.stdout}{result.stderr}"
            )
    return Metric(name, failures, kind="hard")


def lint(root: Path) -> list[Metric]:
    """G1: ruff with the configured rule set."""
    return [command_gate("lint", [[venv_tool("ruff"), "check", "."]], root)]


def format_check(root: Path) -> list[Metric]:
    """G2: ruff format and Prettier report no changes."""
    commands = [
        [venv_tool("ruff"), "format", "--check", "."],
        ["npm", "--prefix", "dashboard", "run", "format:check"],
    ]
    return [command_gate("format", commands, root)]


def types(root: Path) -> list[Metric]:
    """G3: ty over Python sources, tests and tools; tsc over the dashboard."""
    commands = [
        [venv_tool("ty"), "check", "src", "tests", "tools"],
        ["npm", "--prefix", "dashboard", "run", "typecheck"],
    ]
    return [command_gate("types", commands, root)]


def ruff_counts(output: str, root: Path, *, by_rule: bool) -> dict[str, float]:
    """Per-file (or per-file-and-rule) counts from `ruff --output-format json`."""
    counts: Counter[str] = Counter()
    for item in json.loads(output):
        path = Path(item["filename"])
        rel = (
            path.relative_to(root.resolve()).as_posix()
            if path.is_absolute()
            else path.as_posix()
        )
        counts[f"{rel}::{item['code']}" if by_rule else rel] += 1
    return dict(sorted(counts.items()))


def _ruff_select(root: Path, rules: str) -> str:
    result = run(
        [
            venv_tool("ruff"),
            "check",
            "--select",
            rules,
            "--output-format",
            "json",
            "--exit-zero",
            "src/tychora",
        ],
        root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ruff failed: {result.stderr}")
    return result.stdout


def size_rules(root: Path) -> list[Metric]:
    """G4: too many returns, arguments or statements, per file and rule."""
    per_key = ruff_counts(
        _ruff_select(root, "PLR0911,PLR0913,PLR0915"), root, by_rule=True
    )
    return [Metric("size_rules", sum(per_key.values()), per_key=per_key)]


def body_imports(root: Path) -> list[Metric]:
    """G6: imports inside function bodies (ruff PLC0415), per file."""
    per_key = ruff_counts(_ruff_select(root, "PLC0415"), root, by_rule=False)
    return [Metric("function_body_imports", sum(per_key.values()), per_key=per_key)]


def docstrings(root: Path) -> list[Metric]:
    """G14 (ratchet part): missing module, class, method, function, package docstrings."""
    rules = "D100,D101,D102,D103,D104,D419"
    per_key = ruff_counts(_ruff_select(root, rules), root, by_rule=True)
    return [Metric("missing_docstrings", sum(per_key.values()), per_key=per_key)]


def complexity_from_json(items: list[dict[str, Any]]) -> Metric:
    """G25: functions above cognitive complexity 15, keyed by path::function."""
    per_key: dict[str, float] = {}
    for item in items:
        if item["complexity"] > COMPLEXITY_LIMIT:
            key = f"{item['path']}::{item['function_name']}"
            suffix = 2
            while key in per_key:
                key = f"{item['path']}::{item['function_name']}#{suffix}"
                suffix += 1
            per_key[key] = item["complexity"]
    return Metric("cognitive_complexity", len(per_key), per_key=per_key)


def complexity(root: Path) -> list[Metric]:
    """G25: run complexipy and keep the functions above the limit."""
    output = root / "reports" / "complexipy.json"
    output.parent.mkdir(exist_ok=True)
    result = run(
        [
            venv_tool("complexipy"),
            "src/tychora",
            "--output-format",
            "json",
            "--output",
            str(output),
            "--quiet",
            "--ignore-complexity",
        ],
        root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"complexipy failed: {result.stderr}")
    return [complexity_from_json(json.loads(read_report(output)))]


def jscpd_metrics(scope: str, report: dict[str, Any]) -> list[Metric]:
    """G9-G11: duplicated-line percentage, plus new clones for production scopes."""
    total = report["statistics"]["total"]
    metrics = [
        Metric(f"duplication_{scope}", round(total["percentage"], 2), tolerance=0.01)
    ]
    if scope != "tests":
        metrics.append(Metric(f"new_clones_{scope}", total["newClones"], kind="hard"))
    return metrics


def jscpd_command(root: Path, scope: str, *, update: bool = False) -> list[str]:
    """The jscpd invocation for one scope; update rewrites its fingerprint allow-list."""
    formats, paths = JSCPD_SCOPES[scope]
    command = [
        str(root / "dashboard" / "node_modules" / ".bin" / "jscpd"),
        "--config",
        ".jscpd.json",
        "--format",
        formats,
        "--report-name",
        scope,
        "--baseline",
        f"quality/jscpd/{scope}.json",
        *paths,
    ]
    return [*command, "--update-baseline"] if update else command


def duplication(root: Path) -> list[Metric]:
    """G9, G10, G11: run jscpd per scope and read its JSON reports."""
    metrics: list[Metric] = []
    for scope in JSCPD_SCOPES:
        result = run(jscpd_command(root, scope), root)
        if result.returncode != 0:
            raise RuntimeError(f"jscpd {scope} failed: {result.stdout}{result.stderr}")
        report = json.loads(read_report(root / "reports" / "jscpd" / f"{scope}.json"))
        metrics.extend(jscpd_metrics(scope, report))
    return metrics


def vulture_findings(output: str) -> dict[str, float]:
    """Unused functions, methods, classes, properties and attributes (not variables)."""
    findings: dict[str, float] = {}
    for line in output.splitlines():
        match = VULTURE_LINE.match(line)
        if match:
            findings[f"{match['path']}::{match['name']}"] = 1
    return findings


def whitelist_size(text: str) -> int:
    """Entries in quality/vulture_whitelist.py (one name per non-comment line)."""
    return sum(
        1 for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


def dead_code(root: Path) -> list[Metric]:
    """G12: vulture with the reviewed whitelist; the whitelist may only shrink."""
    result = run([venv_tool("vulture")], root)
    findings = vulture_findings(result.stdout)
    whitelist = (root / "quality" / "vulture_whitelist.py").read_text()
    return [
        Metric("dead_code_python", len(findings), kind="hard", per_key=findings),
        Metric("dead_code_whitelist", whitelist_size(whitelist)),
    ]


def knip_count(report: dict[str, Any]) -> int:
    """Total knip findings: files, dependencies, exports, types and the rest."""
    return sum(
        len(value)
        for issue in report["issues"]
        for value in issue.values()
        if isinstance(value, list)
    )


def dashboard_dead_code(root: Path) -> list[Metric]:
    """G13: knip over the dashboard."""
    dashboard = root / "dashboard"
    result = run(
        [
            str(dashboard / "node_modules" / ".bin" / "knip"),
            "--reporter",
            "json",
            "--no-progress",
        ],
        dashboard,
    )
    return [
        Metric(
            "dead_code_dashboard", knip_count(json.loads(result.stdout)), kind="hard"
        )
    ]


def dashboard_cycles(root: Path) -> list[Metric]:
    """G28: madge import cycles in dashboard/src."""
    dashboard = root / "dashboard"
    result = run(
        [
            str(dashboard / "node_modules" / ".bin" / "madge"),
            "--circular",
            "--json",
            "--extensions",
            "ts,tsx",
            "--ts-config",
            "tsconfig.json",
            "src",
        ],
        dashboard,
    )
    cycles = json.loads(result.stdout[result.stdout.index("[") :])
    return [Metric("dashboard_import_cycles", len(cycles), kind="hard")]
```

- [ ] **Step 3: Implement `tools/health/layering.py`**

```python
"""G7: import-linter contracts and the size of their exception lists."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tools.health.external import run, venv_tool
from tools.health.model import Metric


def exception_counts(pyproject: dict) -> dict[str, float]:
    """Number of ignore_imports entries per contract id."""
    contracts = pyproject.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return {
        contract["id"]: float(len(contract.get("ignore_imports", [])))
        for contract in contracts
    }


def layering(root: Path) -> list[Metric]:
    """Broken contracts must be zero; exceptions may be removed, never added."""
    result = run(
        [venv_tool("lint-imports"), "--no-cache"],
        root,
        env={"PYTHONPATH": str(root / "src")},
    )
    if result.returncode != 0:
        print(result.stdout + result.stderr)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    per_key = exception_counts(pyproject)
    return [
        Metric("layering", 0 if result.returncode == 0 else 1, kind="hard"),
        Metric("layering_exceptions", sum(per_key.values()), per_key=per_key),
    ]
```

(`PYTHONPATH` makes grimp analyse the tree under `root/src`, which matters for the throwaway package of Task 15's integration test; in the repository it is the same tree as the editable install.)

- [ ] **Step 4: Implement `tools/health/advisories.py`**

```python
"""G19 and G20: dependency advisories against the waiver register, and lock freshness."""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.health.external import command_gate, run, venv_tool
from tools.health.model import Metric

REGISTER = Path("quality/advisories.toml")
MAX_WAIVER_DAYS = 30


@dataclass(frozen=True)
class Waiver:
    """One owner-approved, time-limited exception for an advisory without a fix."""

    id: str
    package: str
    reason: str
    reviewer: str
    approved: dt.date
    expires: dt.date


def load_register(text: str) -> list[Waiver]:
    """Parse quality/advisories.toml; every field is required."""
    data = tomllib.loads(text)
    waivers = [Waiver(**entry) for entry in data.get("advisory", [])]
    for waiver in waivers:
        if (waiver.expires - waiver.approved).days > MAX_WAIVER_DAYS:
            raise ValueError(
                f"{waiver.id}: a waiver lasts at most {MAX_WAIVER_DAYS} days"
            )
    return waivers


def reported_advisories(pip_audit: dict[str, Any]) -> dict[str, str]:
    """Advisory id to package name from `pip-audit --format json`."""
    found: dict[str, str] = {}
    for dependency in pip_audit.get("dependencies", []):
        for vulnerability in dependency.get("vulns", []):
            found[vulnerability["id"]] = dependency["name"]
    return found


def advisory_findings(
    reported: dict[str, str], waivers: list[Waiver], today: dt.date
) -> dict[str, float]:
    """Unlisted or expired advisories, and waivers no advisory needs any more."""
    by_id = {waiver.id: waiver for waiver in waivers}
    findings: dict[str, float] = {}
    for advisory, package in sorted(reported.items()):
        waiver = by_id.get(advisory)
        if waiver is None:
            findings[f"{package}:{advisory}: not in {REGISTER}"] = 1
        elif waiver.expires < today:
            findings[f"{package}:{advisory}: waiver expired {waiver.expires}"] = 1
    for advisory in sorted(set(by_id) - set(reported)):
        findings[f"{advisory}: no longer reported; remove it from {REGISTER}"] = 1
    return findings


def npm_vulnerabilities(audit: dict[str, Any]) -> int:
    """Total vulnerabilities of every severity from `npm audit --json`."""
    return int(audit["metadata"]["vulnerabilities"]["total"])


def advisories(root: Path, today: dt.date | None = None) -> list[Metric]:
    """Hard gate over pip-audit (with the register) and npm audit."""
    pip = run([venv_tool("pip-audit"), "--local", "--format", "json"], root)
    reported = reported_advisories(json.loads(pip.stdout))
    waivers = load_register((root / REGISTER).read_text())
    findings = advisory_findings(
        reported, waivers, today or dt.datetime.now(tz=dt.UTC).date()
    )
    npm = run(
        ["npm", "--prefix", "dashboard", "audit", "--audit-level=low", "--json"], root
    )
    npm_total = npm_vulnerabilities(json.loads(npm.stdout))
    if npm_total:
        findings["dashboard: npm audit"] = npm_total
    return [Metric("advisories", sum(findings.values()), kind="hard", per_key=findings)]


def lock_freshness(root: Path) -> list[Metric]:
    """G20: uv.lock matches pyproject.toml (npm ci in CI proves package-lock.json)."""
    return [command_gate("lock", [["uv", "lock", "--check"]], root)]
```

- [ ] **Step 5: Implement `tools/health/testruns.py`**

```python
"""Gates that read reports written by test runs: coverage, runtime, clean suite, flakes, mutation, gold set."""

from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health.external import read_report
from tools.health.model import Metric

CORE_MODULES = (
    "tychora/contracts/identity.py",
    "tychora/contracts/publication.py",
    "tychora/adapters/sqlite/publication.py",
    "tychora/retrieval/eligibility.py",
    "tychora/adapters/sqlite/eligibility.py",
    "tychora/retrieval/lexical_query.py",
    "tychora/adapters/sqlite/lexical.py",
    "tychora/retrieval/fusion.py",
)
COVERAGE_TOLERANCE = 0.1
RUNTIME_TOLERANCE = 0.15
DETECTED = frozenset({"killed", "timeout"})
NOT_RUN = frozenset({"not checked", "skipped"})


def _percent(covered: int, total: int) -> float:
    return round(100.0 * covered / total, 2) if total else 100.0


def _core_file(files: dict[str, Any], module: str) -> dict[str, Any]:
    for path, data in files.items():
        if path.replace("\\", "/").endswith(module):
            return data["summary"]
    raise KeyError(f"{module} is missing from the coverage report")


def coverage_metrics(report: dict[str, Any], *, branch: bool) -> list[Metric]:
    """G16: total and per-core-module line (pull requests) or branch (nightly) coverage."""
    kind = "branch" if branch else "line"
    covered_key, total_key = (
        ("covered_branches", "num_branches")
        if branch
        else ("covered_lines", "num_statements")
    )
    totals = report["totals"]
    per_key = {
        module: _percent(
            _core_file(report["files"], module)[covered_key],
            _core_file(report["files"], module)[total_key],
        )
        for module in CORE_MODULES
    }
    return [
        Metric(
            f"coverage_{kind}",
            _percent(totals[covered_key], totals[total_key]),
            direction="higher",
            tolerance=COVERAGE_TOLERANCE,
            per_key=per_key,
        )
    ]


def line_coverage(root: Path) -> list[Metric]:
    """Pull-request coverage from reports/coverage.json (sysmon, lines only)."""
    return coverage_metrics(
        json.loads(read_report(root / "reports/coverage.json")), branch=False
    )


def branch_coverage(root: Path) -> list[Metric]:
    """Nightly coverage from reports/coverage-branch.json."""
    report = json.loads(read_report(root / "reports/coverage-branch.json"))
    return coverage_metrics(report, branch=True)


def dashboard_coverage(root: Path) -> list[Metric]:
    """G16 (dashboard): Vitest V8 statement and branch percentages."""
    summary = json.loads(read_report(root / "dashboard/coverage/coverage-summary.json"))
    total = summary["total"]
    return [
        Metric(
            f"dashboard_coverage_{name}",
            total[name]["pct"],
            direction="higher",
            tolerance=COVERAGE_TOLERANCE,
        )
        for name in ("statements", "branches")
    ]


def outcomes(junit: str) -> dict[str, str]:
    """Test id to passed, failed or skipped from a JUnit XML report."""
    results: dict[str, str] = {}
    for case in ET.fromstring(junit).iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None or case.find("error") is not None:
            results[test_id] = "failed"
        elif case.find("skipped") is not None:
            results[test_id] = "skipped"
        else:
            results[test_id] = "passed"
    return results


def suite_seconds(junit: str) -> float:
    """Wall time of the session recorded by pytest's JUnit writer."""
    root = ET.fromstring(junit)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return round(sum(float(suite.get("time", 0)) for suite in suites), 1)


def slowest(junit: str, count: int = 20) -> list[tuple[float, str]]:
    """The slowest test cases, printed on every run so their cost stays visible."""
    cases = [
        (float(case.get("time", 0)), f"{case.get('classname')}::{case.get('name')}")
        for case in ET.fromstring(junit).iter("testcase")
    ]
    return sorted(cases, reverse=True)[:count]


def clean_suite(root: Path) -> list[Metric]:
    """G17: the default selection with no TYCHORA_TEST_* variable has no failure."""
    results = outcomes(read_report(root / "reports/clean-junit.xml"))
    failed = {test: 1.0 for test, outcome in results.items() if outcome == "failed"}
    return [Metric("clean_machine_failures", len(failed), kind="hard", per_key=failed)]


def runtime(root: Path) -> list[Metric]:
    """G18: pull-request suite wall time; at most 15% above the baseline."""
    junit = read_report(root / "reports/junit.xml")
    for seconds, test in slowest(junit):
        print(f"[slowest] {seconds:7.1f}s {test}")
    return [
        Metric(
            "test_runtime_seconds",
            suite_seconds(junit),
            relative_tolerance=RUNTIME_TOLERANCE,
            stale_on_improvement=False,
        )
    ]


def flaky_tests(reports: list[str]) -> list[str]:
    """Tests whose outcome differs between runs with different random seeds."""
    runs = [outcomes(report) for report in reports]
    tests = set().union(*runs)
    return sorted(t for t in tests if len({run.get(t, "absent") for run in runs}) > 1)


def mutation_scores(results: str, equivalent: set[str]) -> dict[str, float]:
    """Score per module: killed or timed-out mutants over all non-equivalent ones."""
    detected: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for line in results.splitlines():
        name, _, status = line.strip().partition(": ")
        if not status or status in NOT_RUN or name in equivalent:
            continue
        module = name.rsplit(".", 1)[0]
        total[module] += 1
        detected[module] += status in DETECTED
    return {
        module: _percent(detected[module], total[module]) for module in sorted(total)
    }


def mutation(root: Path) -> list[Metric]:
    """G21: nightly mutation score per core module from `mutmut results --all true`."""
    equivalent_file = tomllib.loads(
        (root / "quality/equivalent-mutants.toml").read_text()
    )
    equivalent = {entry["name"] for entry in equivalent_file.get("mutant", [])}
    scores = mutation_scores(
        read_report(root / "reports/mutmut-results.txt"), equivalent
    )
    overall = round(sum(scores.values()) / len(scores), 2) if scores else 0.0
    return [
        Metric(
            "mutation_score",
            overall,
            direction="higher",
            tolerance=COVERAGE_TOLERANCE,
            per_key=scores,
            new_keys_allowed=True,
        )
    ]


def gold_set(root: Path) -> list[Metric]:
    """G22: mean code gold-set metrics per variant, written by the evaluation test."""
    report = json.loads(read_report(root / "reports/code-gold.json"))
    per_key = {
        f"{variant}::{metric}": value
        for variant, metrics in report["variants"].items()
        for metric, value in metrics.items()
    }
    overall = per_key.get("lexical::file_recall", 0.0)
    return [
        Metric(
            "code_gold",
            overall,
            direction="higher",
            tolerance=0.0,
            per_key=per_key,
            new_keys_allowed=True,
        )
    ]
```

- [ ] **Step 6: Add the jscpd configuration and the advisory register**

Create `.jscpd.json` (minimum 50 tokens, as measured in spec §3.2):

```json
{
  "minTokens": 50,
  "reporters": ["json", "silent"],
  "output": "reports/jscpd",
  "ignore": ["**/node_modules/**", "**/__pycache__/**", "src/tychora/resources/**"],
  "gitignore": true
}
```

List the advisories pip-audit reports today:

```bash
uv run --locked --group audit pip-audit --local --format json | uv run --locked python -c "import json,sys; d=json.load(sys.stdin); print(sorted({(v['id'], x['name'], x['version']) for x in d['dependencies'] for v in x.get('vulns', [])}))"
```

Expected (spec §3.1): `PYSEC-2026-311`, `PYSEC-2026-3813`, `PYSEC-2026-3814`, `PYSEC-2026-3815`, all `chromadb 1.5.9`. If the identifiers differ, use the reported ones below. Create `quality/advisories.toml` with `approved` set to today and `expires` exactly 30 days later (`date -v+30d +%F` on macOS, `date -d '+30 days' +%F` on Linux):

```toml
# Waiver register for dependency advisories that have no fixed release (gate G19).
# Each entry is approved by the product owner in review, expires after at most 30
# days and is reviewed monthly. The gate fails on an unlisted or expired advisory and
# on an entry whose advisory is no longer reported.

[[advisory]]
id = "PYSEC-2026-311"
package = "chromadb"
reason = "No fixed release on PyPI. Not reachable: Tychora uses the embedded PersistentClient and never starts the Chroma HTTP server. Upgrade to 1.6.0 when published."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15

[[advisory]]
id = "PYSEC-2026-3813"
package = "chromadb"
reason = "No fixed release on PyPI. Not reachable: Tychora uses the embedded PersistentClient and never starts the Chroma HTTP server. Upgrade to 1.6.0 when published."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15

[[advisory]]
id = "PYSEC-2026-3814"
package = "chromadb"
reason = "No fixed release on PyPI. Not reachable: Tychora uses the embedded PersistentClient and never starts the Chroma HTTP server. Upgrade to 1.6.0 when published."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15

[[advisory]]
id = "PYSEC-2026-3815"
package = "chromadb"
reason = "No fixed release on PyPI. Not reachable: Tychora uses the embedded PersistentClient and never starts the Chroma HTTP server. Upgrade to 1.6.0 when published."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15
```

- [ ] **Step 7: Check whether Chroma 1.6.0 has been published**

```bash
curl -s https://pypi.org/pypi/chromadb/json | uv run --locked python -c "import json,sys; print('1.6.0' in json.load(sys.stdin)['releases'])"
```

If `False`, go to Step 8. If `True`: replace `chromadb==1.5.9` by `chromadb==1.6.0` in the `vector` and `full` extras of `pyproject.toml`; run `uv lock`; confirm `src/tychora/adapters/chroma/index.py` still compares `collection.configuration` with `_QUALIFIED_CONFIGURATION` exactly (`uv run --locked pytest tests -k chroma -q` and `uv run --locked pytest -m qualification tests/qualification/test_native_stack.py -q`); rerun the pip-audit listing; delete the register entries whose identifiers are no longer reported, and for the two tenant-authorization advisories that remain change the reason to "Fixed upstream in chromadb PR #7602, not yet released. Not reachable: Tychora uses the embedded PersistentClient and never starts the Chroma HTTP server."; update the "Security gate" paragraph of `HANDOFF.md` to name the waiver register.

- [ ] **Step 8: Run the tests and try the gates on the repository**

```bash
uv run --locked pytest tools/health/tests -q
uv run --locked --group audit python -c "from pathlib import Path; from tools.health import advisories; print(advisories.advisories(Path('.'))[0])"
uv run --locked python -c "from pathlib import Path; from tools.health import layering, external; print(layering.layering(Path('.'))[1].value, external.dead_code(Path('.'))[0].value, external.dashboard_cycles(Path('.'))[0].value)"
```

Expected: all checker tests pass; the advisory metric value is `0`; layering exceptions `90.0`, dead code `0`, dashboard cycles `0`.

- [ ] **Step 9: Commit**

```bash
git add tools/health .jscpd.json quality/advisories.toml
git commit -m "feat(health): measures backed by ruff, jscpd, vulture, knip, madge, pip-audit and test reports"
```

---

### Task 15: Gate registry, command line and the checker's integration test (S1)

Spec §4.1, §4.5, §6.2, §4.7 "Isolation check", §4.10 (`--fast`).

**Files:**
- Create: `tools/health/gates.py`, `tools/health/__main__.py`, `tools/health/isolation.txt`
- Test: `tools/health/tests/test_gates.py`, `tools/health/tests/test_integration.py`

**Interfaces:**
- Consumes: every measure of Tasks 13 and 14; `model` (Task 12).
- Produces:
  - `Gate(id: str, title: str, group: Literal["pr","nightly"], measure: Callable[[Path], list[Metric]], fast: bool=False)`; `GATES: tuple[Gate, ...]`; `select(group: str, ids: str = "", *, fast: bool = False) -> list[Gate]`
  - CLI: `python -m tools.health check [--group pr|nightly|all] [--gates G5,G8] [--fast] [--root PATH]`, `update [--initial]`, `report`, `flaky REPORT...`, `isolate`
  - `tools.health.__main__.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tools/health/tests/test_gates.py`:

```python
"""The registry holds every gate of the design, grouped for pull requests and nightly."""

import pytest

from tools.health.gates import GATES, select


def test_every_spec_gate_is_registered():
    ids = {gate.id for gate in GATES}
    expected = ({f"G{i}" for i in range(1, 29)} - {"G9", "G10", "G11"}) | {"G9-G11"}
    assert ids == expected


def test_fast_selection_is_the_local_subset():
    assert [gate.id for gate in select("pr", fast=True)] == ["G1", "G2", "G3", "G7", "G15"]


def test_nightly_group_holds_branch_coverage_mutation_and_gold():
    assert [(gate.id, gate.title) for gate in select("nightly")] == [
        ("G16", "Coverage, branches"),
        ("G21", "Mutation score"),
        ("G22", "Code gold set"),
    ]


def test_unknown_gate_is_rejected():
    with pytest.raises(SystemExit, match="G99"):
        select("pr", "G99")
```

Create `tools/health/tests/test_integration.py`:

```python
"""S1: a throwaway package with three injected regressions makes `check` fail on each."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.health.__main__ import main

REPO = Path(__file__).resolve().parents[3]
JSCPD = REPO / "dashboard" / "node_modules" / ".bin" / "jscpd"

PYPROJECT = """
[tool.importlinter]
root_package = "tychora"

[[tool.importlinter.contracts]]
id = "layers"
name = "Layers"
type = "layers"
layers = ["tychora.application", "tychora.contracts"]
"""

BLOCK = "".join(
    f"    total_{i} = sum(value * {i} for value in values if value > {i})\n"
    for i in range(12)
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write(tmp_path, "pyproject.toml", PYPROJECT)
    write(tmp_path, ".jscpd.json", (REPO / ".jscpd.json").read_text())
    for package in ("", "contracts/", "application/"):
        write(tmp_path, f"src/tychora/{package}__init__.py", '"""Package."""\n')
    write(tmp_path, "src/tychora/contracts/ids.py", '"""Ids."""\n\nVALUE = 1\n')
    write(
        tmp_path,
        "src/tychora/application/service.py",
        '"""Service."""\n\nfrom tychora.contracts.ids import VALUE\n\n\n'
        f"def compute(values):\n{BLOCK}    return VALUE\n",
    )
    write(tmp_path, "dashboard/src/index.ts", "export const one = 1;\n")
    write(tmp_path, "dashboard/e2e/smoke.spec.ts", "export const two = 2;\n")
    write(tmp_path, "tests/test_smoke.py", "def test_smoke():\n    assert True\n")
    (tmp_path / "dashboard" / "node_modules" / ".bin").mkdir(parents=True)
    os.symlink(JSCPD, tmp_path / "dashboard" / "node_modules" / ".bin" / "jscpd")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "base"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.mark.skipif(
    not JSCPD.exists(), reason="run `npm ci` in dashboard/ to install jscpd"
)
def test_three_injected_regressions_fail_with_named_gates(project, capsys):
    gates = "G5,G7,G9-G11"
    assert main(["update", "--initial", "--gates", gates, "--root", str(project)]) == 0
    assert main(["check", "--gates", gates, "--root", str(project)]) == 0
    capsys.readouterr()

    write(
        project,
        "src/tychora/contracts/long.py",
        '"""Long."""\n\n\ndef long():\n'
        + "".join(f"    x{i} = {i}\n" for i in range(88))
        + "    return 0\n",
    )
    write(
        project,
        "src/tychora/contracts/upward.py",
        '"""Upward."""\n\nfrom tychora.application.service import compute\n',
    )
    write(
        project,
        "src/tychora/contracts/copy.py",
        f'"""Copy."""\n\n\ndef again(values):\n{BLOCK}    return 0\n',
    )

    assert main(["check", "--gates", gates, "--root", str(project)]) == 1
    output = capsys.readouterr().out
    assert "src/tychora/contracts/long.py::long: new entry with value 90" in output
    assert "layering" in output and "FAIL" in output
    assert "new_clones_python" in output


def test_module_entry_point_prints_usage():
    result = subprocess.run(
        [sys.executable, "-m", "tools.health", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "check" in result.stdout and "update" in result.stdout
```

Run: `uv run --locked pytest tools/health/tests/test_gates.py tools/health/tests/test_integration.py -q`
Expected: `ModuleNotFoundError: No module named 'tools.health.gates'`.

- [ ] **Step 2: Implement `tools/health/gates.py`**

```python
"""The gate registry: identifiers G1-G28, their group and the function that measures them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.health import advisories, external, graph, layering, static, testruns
from tools.health.model import Metric

Group = Literal["pr", "nightly"]


@dataclass(frozen=True)
class Gate:
    """One gate of the code-health design (section 4.3)."""

    id: str
    title: str
    group: Group
    measure: Callable[[Path], list[Metric]]
    fast: bool = False


GATES: tuple[Gate, ...] = (
    Gate("G1", "Lint", "pr", external.lint, fast=True),
    Gate("G2", "Format", "pr", external.format_check, fast=True),
    Gate("G3", "Types", "pr", external.types, fast=True),
    Gate("G4", "Size rules", "pr", external.size_rules),
    Gate("G5", "Function length", "pr", static.function_length),
    Gate("G6", "Function-body imports", "pr", external.body_imports),
    Gate("G7", "Layering", "pr", layering.layering, fast=True),
    Gate("G8", "Raw SQL outside adapters", "pr", static.raw_sql),
    Gate("G9-G11", "Duplication", "pr", external.duplication),
    Gate("G12", "Dead code, Python", "pr", external.dead_code),
    Gate("G13", "Dead code, TypeScript", "pr", external.dashboard_dead_code),
    Gate("G14", "Docstrings", "pr", external.docstrings),
    Gate("G14", "Public API docstrings", "pr", static.public_api_docstrings),
    Gate("G15", "Plan references", "pr", static.plan_references, fast=True),
    Gate("G16", "Coverage, lines", "pr", testruns.line_coverage),
    Gate("G16", "Coverage, dashboard", "pr", testruns.dashboard_coverage),
    Gate("G16", "Coverage, branches", "nightly", testruns.branch_coverage),
    Gate("G17", "Clean-machine suite", "pr", testruns.clean_suite),
    Gate("G18", "Test runtime", "pr", testruns.runtime),
    Gate("G19", "Advisories", "pr", advisories.advisories),
    Gate("G20", "Lock freshness", "pr", advisories.lock_freshness),
    Gate("G21", "Mutation score", "nightly", testruns.mutation),
    Gate("G22", "Code gold set", "nightly", testruns.gold_set),
    Gate("G23", "Quarantined tests", "pr", static.quarantined_tests),
    Gate("G24", "Integrity errors without a reason", "pr", static.integrity_raises),
    Gate("G25", "Cognitive complexity", "pr", external.complexity),
    Gate("G26", "Module-level import cycles", "pr", graph.module_level_cycles),
    Gate("G27", "Modules in import cycles", "pr", graph.all_import_cycles),
    Gate("G28", "Dashboard import cycles", "pr", external.dashboard_cycles),
)


def select(group: str, ids: str = "", *, fast: bool = False) -> list[Gate]:
    """Gates of a group ("pr", "nightly" or "all"), optionally narrowed by id or --fast."""
    wanted = {gate_id.strip() for gate_id in ids.split(",") if gate_id.strip()}
    chosen = [
        gate
        for gate in GATES
        if (group == "all" or gate.group == group)
        and (not wanted or gate.id in wanted)
        and (not fast or gate.fast)
    ]
    if wanted - {gate.id for gate in chosen}:
        raise SystemExit(
            f"unknown or unselected gates: {sorted(wanted - {g.id for g in chosen})}"
        )
    return chosen
```

- [ ] **Step 3: Implement `tools/health/__main__.py`**

```python
"""`python -m tools.health check|update|report|flaky|isolate`: the code-health command line."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tools.health.external import JSCPD_SCOPES, jscpd_command, run
from tools.health.gates import Gate, select
from tools.health.model import (
    BASELINE,
    Finding,
    Metric,
    UpdateRefused,
    compare,
    load_baseline,
    save_baseline,
    updated_entry,
)
from tools.health.testruns import flaky_tests

DEFAULT_SELECTION = "not live and not qualification and not release"
ISOLATION_LIST = Path("tools/health/isolation.txt")


def commit(root: Path) -> str:
    """Short hash of HEAD, recorded as measured_at for changed baseline entries."""
    return subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def measure(root: Path, gates: list[Gate]) -> list[tuple[Gate, Metric]]:
    """Run every selected gate once and keep which gate produced each metric."""
    return [(gate, metric) for gate in gates for metric in gate.measure(root)]


def check(root: Path, gates: list[Gate]) -> int:
    """Print one line per metric and return 1 when any finding exists."""
    baseline = load_baseline(root / BASELINE)
    failed = False
    for gate, metric in measure(root, gates):
        findings: list[Finding] = compare(metric, baseline.get(metric.name))
        entry = baseline.get(metric.name)
        base = "-" if metric.kind == "hard" or entry is None else f"{entry['value']:g}"
        status = (
            "ok"
            if not findings
            else ("stale" if all(f.stale for f in findings) else "FAIL")
        )
        print(f"{gate.id:7} {metric.name:34} {metric.value:>10g} {base:>10} {status}")
        for finding in findings:
            print(f"        - {finding.message}")
        failed = failed or bool(findings)
    return 1 if failed else 0


def refresh_clone_allowlists(root: Path) -> None:
    """Rewrite quality/jscpd/*.json; called only when no new clone exists."""
    (root / "quality" / "jscpd").mkdir(parents=True, exist_ok=True)
    for scope in JSCPD_SCOPES:
        result = run(jscpd_command(root, scope, update=True), root)
        if result.returncode != 0:
            raise RuntimeError(f"jscpd {scope}: {result.stdout}{result.stderr}")


def update(root: Path, gates: list[Gate], *, initial: bool) -> int:
    """Record improvements; refuse regressions, unfixed hard gates and new over-limit keys."""
    duplication = any(gate.id == "G9-G11" for gate in gates)
    if initial and duplication:
        refresh_clone_allowlists(root)
    measured = measure(root, gates)
    hard = [m for _, m in measured if m.kind == "hard" and m.value and not initial]
    if hard:
        print("fix the hard gates first: " + ", ".join(m.name for m in hard))
        return 1
    baseline = load_baseline(root / BASELINE)
    head = commit(root)
    new = dict(baseline)
    try:
        for _, metric in measured:
            if metric.kind == "ratchet":
                new[metric.name] = updated_entry(
                    metric, baseline.get(metric.name), head, initial=initial
                )
    except UpdateRefused as refused:
        print(f"update refused: {refused}")
        return 1
    if duplication and not initial:
        refresh_clone_allowlists(root)
    for name in sorted(new):
        before = baseline.get(name, {}).get("value")
        if before != new[name]["value"]:
            print(f"{name}: {before} -> {new[name]['value']}")
    save_baseline(root / BASELINE, new)
    return 0


def report(root: Path, gates: list[Gate]) -> int:
    """Print current values without comparing them; never fails on a value."""
    for gate, metric in measure(root, gates):
        print(f"{gate.id:7} {metric.name:34} {metric.value:>10g}")
    return 0


def isolate(root: Path) -> int:
    """Run each listed test file alone to prove it inherits no fixture by accident."""
    failures = 0
    for line in (root / ISOLATION_LIST).read_text().splitlines():
        test_file = line.strip()
        if not test_file or test_file.startswith("#"):
            continue
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                test_file,
                "-q",
                "-p",
                "no:randomly",
                "-m",
                DEFAULT_SELECTION,
            ],
            cwd=root,
            check=False,
        )
        failures += result.returncode not in (0, 5)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to one sub-command."""
    parser = argparse.ArgumentParser(prog="python -m tools.health")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "update", "report"):
        command = commands.add_parser(name)
        command.add_argument("--group", choices=("pr", "nightly", "all"), default="pr")
        command.add_argument(
            "--gates", default="", help="comma-separated ids, e.g. G5,G8"
        )
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument(
            "--fast", action="store_true", help="G1-G3, G7 and G15 only"
        )
        if name == "update":
            command.add_argument("--initial", action="store_true")
    flaky = commands.add_parser("flaky")
    flaky.add_argument("reports", nargs="+", type=Path)
    isolation = commands.add_parser("isolate")
    isolation.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "flaky":
        differing = flaky_tests([path.read_text() for path in args.reports])
        for test in differing:
            print(f"flaky: {test}")
        return 1 if differing else 0
    if args.command == "isolate":
        return isolate(args.root)
    gates = select(args.group, args.gates, fast=args.fast)
    if args.command == "check":
        return check(args.root, gates)
    if args.command == "update":
        return update(args.root, gates, initial=args.initial)
    return report(args.root, gates)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: List the isolation files**

Create `tools/health/isolation.txt` (the two Docling files, then the eight largest default-selection files by test functions):

```
# Each file runs alone in CI (python -m tools.health isolate) to prove that no
# fixture is inherited by accident from another module.
tests/ingestion/test_docling_mapping.py
tests/ingestion/test_document_records.py
tests/security/test_acquisition.py
tests/configuration/test_resolve.py
tests/storage/test_corpus.py
tests/ingestion/test_tables.py
tests/configuration/test_registry.py
tests/transports/test_http.py
tests/retrieval/test_eligibility.py
tests/providers/test_text.py
```

- [ ] **Step 5: Run the checker tests, then the checker itself**

```bash
uv run --locked pytest tools/health/tests -q
uv run --locked python -m tools.health check --fast
uv run --locked python -m tools.health report --gates G4,G5,G6,G8,G24,G25,G27
env -i PATH="$PATH" HOME="$HOME" uv run --locked python -m tools.health isolate
```

Expected: all checker tests pass, including the integration test (dashboard `node_modules` installed in Task 8); `check --fast` exits 1 with exactly one finding, `layering_exceptions: no baseline entry` (the baseline is committed in Task 16), while `lint`, `format`, `types`, `layering` and `plan_references` print `ok`; `report` prints `size_rules 163`, `function_length 158`, `function_body_imports 336`, `raw_sql_outside_adapters 240`, `integrity_raises_without_reason 380`, `cognitive_complexity 229`, `modules_in_import_cycles 76` (± changes made by earlier tasks); `isolate` exits 0.

- [ ] **Step 6: Document the local entry point**

In `docs/contributing.md`, in "## Development environment", replace the three lint lines of the command block (`ruff check`, `ruff format --check`, `ty check`) with:

```bash
uv run --locked python -m tools.health check --fast
```

and add after the block:

```markdown
`check --fast` runs lint, format, types, the layering contracts and the plan-reference rule in a few seconds. `uv run --locked python -m tools.health check` runs every pull-request gate; it needs the reports of a full test run (`reports/`), so it normally runs in CI. When a metric improves, run `uv run --locked python -m tools.health update` and commit `quality/baseline.json` with the change. A hand edit that makes a baseline value worse needs a one-line justification in the pull request.
```

- [ ] **Step 7: Commit**

```bash
git add tools/health docs/contributing.md
git commit -m "feat(health): gate registry, check/update/report CLI and integration test"
```

---

### Task 16: CI: pull-request jobs, the measured baseline and the health job (S1, S2, S3, S9)

Spec §4.10, §6.1. Ubuntu is the reference runner; the baseline is measured there, not on a laptop.

**Files:**
- Modify: `.github/workflows/check.yml` (rewrite)
- Create: `.github/pull_request_template.md`
- Create (from the CI artifact): `quality/baseline.json`, `quality/jscpd/python.json`, `quality/jscpd/typescript.json`, `quality/jscpd/tests.json`
- Modify: `quality/coverage-line.toml` (`fail_under`)

**Interfaces:**
- Consumes: the checker (Tasks 12–15); reports written here: `reports/coverage.json`, `reports/junit.xml` (job `tests`, Ubuntu), `reports/clean-junit.xml` (job `clean-machine`), `dashboard/coverage/coverage-summary.json` (job `health`).
- Produces: artifact names `test-reports`, `clean-reports`, `quality-baseline`; workflow input `baseline` (boolean) on `workflow_dispatch`.

- [ ] **Step 1: Rewrite `.github/workflows/check.yml`**

```yaml
name: Locked checks

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      baseline:
        description: Measure quality/baseline.json on the reference runner and upload it
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  tests:
    name: Tests (${{ matrix.runner }})
    strategy:
      fail-fast: false
      matrix:
        runner: [ubuntu-22.04, macos-15]
    runs-on: ${{ matrix.runner }}
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked stack
        run: uv sync --locked --all-extras --group dev --group test-fixtures
      - name: Provision pinned public test assets
        run: python packaging/provision_ci_assets.py --cache "$RUNNER_TEMP/tychora-public-models" --workspace "$RUNNER_TEMP/tychora-asset-setup" --github-env "$GITHUB_ENV"
      - name: Unit and local integration tests (parallel, random order, line coverage)
        env:
          COVERAGE_CORE: sysmon
          TYCHORA_TEST_TIMEOUT_FACTOR: "3"
        run: >-
          uv run --locked pytest tests -q -n auto
          -m 'not live and not qualification and not release and not slow and not quarantine'
          --cov --cov-config=quality/coverage-line.toml --cov-report=json:reports/coverage.json
          --junitxml=reports/junit.xml
      - name: Keep reports for the health job
        if: always() && matrix.runner == 'ubuntu-22.04'
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: test-reports
          path: reports/
          retention-days: 7

  clean-machine:
    name: Clean machine and isolation
    runs-on: ubuntu-22.04
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked stack
        run: uv sync --locked --all-extras --group dev --group test-fixtures
      - name: Default suite without assets or variables
        run: >-
          env -i PATH="$PATH" HOME="$HOME"
          uv run --locked pytest tests -q -n auto -rs
          -m 'not live and not qualification and not release'
          --junitxml=reports/clean-junit.xml
      - name: Each listed test file alone
        run: env -i PATH="$PATH" HOME="$HOME" uv run --locked python -m tools.health isolate
      - name: Keep the clean-machine report
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: clean-reports
          path: reports/clean-junit.xml
          retention-days: 7

  health:
    name: Code-health gates
    needs: [tests, clean-machine]
    runs-on: ubuntu-22.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0
        with:
          node-version-file: dashboard/.nvmrc
          cache: npm
          cache-dependency-path: dashboard/package-lock.json
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked checks and resolved stack
        run: uv sync --locked --all-extras --group dev --group audit --group test-fixtures
      - name: Install dashboard tools without lifecycle scripts
        working-directory: dashboard
        run: npm ci --ignore-scripts
      - name: Dashboard unit coverage
        working-directory: dashboard
        run: npm run coverage
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: test-reports
          path: reports
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: clean-reports
          path: reports
      - name: Checker self-tests
        run: uv run --locked pytest tools/health/tests -q
      - name: Measure the initial baseline (manual dispatch only)
        if: github.event_name == 'workflow_dispatch' && inputs.baseline
        run: uv run --locked python -m tools.health update --initial
      - name: Upload the measured baseline
        if: github.event_name == 'workflow_dispatch' && inputs.baseline
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: quality-baseline
          path: quality/
          retention-days: 7
      - name: Code-health gates
        run: uv run --locked python -m tools.health check
```

(The previous single `quality` job's lint, format, type and pip-audit steps are now gates G1–G3 and G19 inside `check`. The action SHAs for `upload-artifact` v4.6.2 and `download-artifact` v4.3.0 were confirmed with `gh api repos/actions/upload-artifact/git/ref/tags/v4.6.2 --jq .object.sha`; re-run that check before committing.)

- [ ] **Step 2: Validate the workflow syntax**

```bash
uv run --locked python -c "import yaml; [yaml.safe_load(open(p)) for p in ('.github/workflows/check.yml',)]; print('ok')"
```

Expected: `ok` (PyYAML is in the locked full environment).

- [ ] **Step 3: Add the pull-request template**

Create `.github/pull_request_template.md`:

```markdown
## Summary

## Code health

- [ ] `uv run --locked python -m tools.health check --fast` passes locally.
- [ ] Improved metrics are recorded in `quality/baseline.json` (`uv run --locked python -m tools.health update`).
- Hand edits that make a `quality/baseline.json` value worse, one line each with its reason: none.
```

- [ ] **Step 4: Measure the baseline on the reference runner**

Push the branch, then:

```bash
gh workflow run check.yml --ref "$(git branch --show-current)" -f baseline=true
gh run list --workflow check.yml --limit 1
gh run watch <run-id>
gh run download <run-id> -n quality-baseline -D quality
git status --short quality
```

Expected: `quality/baseline.json` and `quality/jscpd/{python,typescript,tests}.json` appear. The `health` job of that run passes (the check runs after the update). Compare the values with spec §3 and record the differences in the commit body: `size_rules` ≈ 163, `function_length` 158, `function_body_imports` 336, `raw_sql_outside_adapters` 240, `duplication_python` 1.32, `duplication_typescript` ≈ 4.08 (lower after Task 7's renames and Task 8's removals), `duplication_tests` 4.74, `missing_docstrings` lower than 1,055 after Task 9, `integrity_raises_without_reason` 380, `cognitive_complexity` 229, `modules_in_import_cycles` 76, `layering_exceptions` 90, `dead_code_whitelist` 39, `quarantined_tests` 0, `coverage_line` near 85.

- [ ] **Step 5: Set the pull-request coverage floor**

Append to `quality/coverage-line.toml`, using the `coverage_line` value of the baseline rounded down to one decimal (for example `85.2` for 85.27):

```toml
[tool.coverage.report]
fail_under = 85.2
```

- [ ] **Step 6: Commit and confirm the pull request is green**

```bash
git add .github/workflows/check.yml .github/pull_request_template.md quality
git commit -m "ci: run tests in parallel on Linux and macOS, prove the clean machine and enforce the health gates"
git push
gh pr checks --watch
```

Expected: `Tests (ubuntu-22.04)`, `Tests (macos-15)`, `Clean machine and isolation`, `Code-health gates` all pass; `Tests (ubuntu-22.04)` finishes in under 5 minutes of test time (the slowest-20 list is printed by the health job).

---

### Task 17: Mutation testing and the identity survivors (S7)

Spec §3.7, §4.7 "Mutation testing", §7 ("Surviving mutants in `contracts/identity.py`: 36 → 0 or each documented as equivalent").

**Files:**
- Modify: `pyproject.toml` (`[tool.mutmut]`, `pythonpath`)
- Modify: `tests/contracts/test_identity.py` (append)
- Create: `quality/equivalent-mutants.toml`

**Interfaces:**
- Consumes: `testruns.mutation` (Task 14) reads `quality/equivalent-mutants.toml` (`[[mutant]] name, reason`) and `reports/mutmut-results.txt`.
- Produces: the nightly mutation command (Task 20): `uv run --no-sync mutmut run --max-children 4` then `uv run --no-sync mutmut results --all true > reports/mutmut-results.txt`.

- [ ] **Step 1: Configure mutmut**

In `[tool.pytest.ini_options]` change `pythonpath = ["."]` to `pythonpath = [".", "src"]` (mutmut must import the mutated copy under `mutants/src`). Append to `pyproject.toml`:

```toml
[tool.mutmut]
source_paths = ["src"]
only_mutate = [
  "src/tychora/contracts/identity.py",
  "src/tychora/contracts/publication.py",
  "src/tychora/adapters/sqlite/publication.py",
  "src/tychora/retrieval/eligibility.py",
  "src/tychora/adapters/sqlite/eligibility.py",
  "src/tychora/retrieval/lexical_query.py",
  "src/tychora/adapters/sqlite/lexical.py",
  "src/tychora/retrieval/fusion.py",
]
also_copy = ["tests"]
pytest_add_cli_args = ["-p", "no:randomly", "-p", "no:cacheprovider"]
pytest_add_cli_args_test_selection = ["tests/contracts", "tests/retrieval", "tests/storage", "tests/indexing"]
```

- [ ] **Step 2: Run mutmut on the identity module to see the survivors**

```bash
uv sync --locked --no-install-project --all-extras --group dev --group mutation --group test-fixtures
uv run --no-sync mutmut run --max-children 6 "tychora.contracts.identity*"
uv run --no-sync mutmut results
```

Expected: 36 `survived` and 10 `no tests` for `tychora.contracts.identity` (spec §3.7): error-message mutations in `_json_primitive`, `normalize_source_key` and `repository_manifest`; `ensure_ascii`/`allow_nan`/encoding mutations of `canonical_bytes`; the `None` check of `_json_primitive`; renamed manifest keys; `family_id` has no test at all.

- [ ] **Step 3: Write the tests that kill them**

Append to `tests/contracts/test_identity.py`:

```python
def test_canonical_bytes_are_compact_sorted_utf8():
    assert canonical_bytes({"é": [1, 2.5, True, None]}) == '{"é":[1,2.5,true,null]}'.encode()
    assert canonical_bytes(None) == b"null"
    assert canonical_bytes(("a",)) == b'["a"]'
    assert canonical_bytes([]) == b"[]"


def test_non_json_values_fail_with_their_rule():
    with pytest.raises(ValueError, match=r"^JSON object keys must be strings$"):
        canonical_bytes({1: "bad"})
    with pytest.raises(TypeError, match=r"^Only JSON values are supported$"):
        canonical_bytes({"x": object()})


def test_source_key_errors_name_the_rule():
    from tychora.contracts.identity import normalize_source_key

    for key in ("a\x00b", "a\\b", "/abs", "C:/file", "a/../b", ""):
        with pytest.raises(
            ValueError,
            match=r"^Source key must be a relative POSIX path without parent traversal$",
        ):
            normalize_source_key(key)
    with pytest.raises(ValueError, match=r"^Source key must name a source$"):
        normalize_source_key("./.")
    assert normalize_source_key("a//b/./c") == "a/b/c"


def test_identity_formulas_are_pinned_to_their_published_digests():
    from uuid import UUID

    from tychora.contracts import identity as ids

    assert digest("revision-v1", "s", "o", "e", "m") == (
        "61fe8bc2c51051183c620769103182359a3f6825e71d4a3be6f01e77f21e2431"
    )
    assert ids.family_id("w", "c", "n") == digest("family-v1", "w", "c", "n")
    assert ids.family_id("w", "c", "n") == (
        "597768bb3e4d8482a4cb6872a364198735f0449c6a5f9150c59f72750d216127"
    )
    assert ids.vector_record_id("f", "c", "i") == (
        "f9ead3014f9bb291caaf04c98c5719e645ed9d74e6e0a02a46e10d893a078fbb"
    )
    assert ids.source_id(UUID(int=1), "docs/file.md") == "ace3e175-b406-55e1-accd-45a027c107ae"


def test_repository_manifest_has_exact_shape():
    from tychora.contracts.identity import repository_manifest

    files = [("b", "100644", "sha-b", "ref-b"), ("./a", "100755", "sha-a", "ref-a")]
    assert repository_manifest(files, commit="abc", dirty=False) == {
        "files": [
            {"relative_path": "a", "mode": "100755", "blob_sha": "sha-a", "artifact_ref": "ref-a"},
            {"relative_path": "b", "mode": "100644", "blob_sha": "sha-b", "artifact_ref": "ref-b"},
        ],
        "commit": "abc",
        "dirty": False,
    }
    with pytest.raises(ValueError, match=r"^Duplicate repository path$"):
        repository_manifest([*files, ("a", "100644", "x", "y")], commit=None, dirty=True)
```

The digests were computed from the current implementation; they pin the identity formulas the programme forbids breaking.

- [ ] **Step 4: Run the tests, then mutmut again**

```bash
uv run --no-sync pytest tests/contracts/test_identity.py -q
rm -rf mutants
uv run --no-sync mutmut run --max-children 6 "tychora.contracts.identity*"
uv run --no-sync mutmut results
```

Expected: 20 passed; mutmut reports `177 killed` and exactly three survivors: `x_canonical_bytes__mutmut_3`, `x_canonical_bytes__mutmut_6`, `x_canonical_bytes__mutmut_19` (verified on a copy). Inspect each with `uv run --no-sync mutmut show <name>`; they are equivalent mutants. If others survive, add a test asserting the behaviour each changes and rerun.

- [ ] **Step 5: Document the equivalent mutants**

Create `quality/equivalent-mutants.toml`:

```toml
# Mutants that cannot change behaviour, excluded from the mutation score (gate G21).
# Add an entry only with a reason a reviewer can check against `mutmut show <name>`.

[[mutant]]
name = "tychora.contracts.identity.x_canonical_bytes__mutmut_3"
reason = "ensure_ascii=None is falsy, which json.dumps treats exactly like ensure_ascii=False."

[[mutant]]
name = "tychora.contracts.identity.x_canonical_bytes__mutmut_6"
reason = "allow_nan=None is falsy, which json.dumps treats exactly like allow_nan=False."

[[mutant]]
name = "tychora.contracts.identity.x_canonical_bytes__mutmut_19"
reason = "Codec names are case-insensitive: encode('UTF-8') is encode('utf-8')."
```

Check the score: `uv run --no-sync mutmut results --all true > reports/mutmut-results.txt && uv run --no-sync python -c "from pathlib import Path; from tools.health.testruns import mutation; print(mutation(Path('.')))"` — expected `per_key` `{'tychora.contracts.identity': 100.0}` (only that module was run).

- [ ] **Step 6: Restore the normal environment and commit**

```bash
uv sync --locked --all-extras --group dev --group test-fixtures
uv run --locked pytest tests/contracts -q -n auto
git add pyproject.toml tests/contracts/test_identity.py quality/equivalent-mutants.toml
git commit -m "test: kill the surviving identity mutants and configure nightly mutation testing"
```

---

### Task 18: Architecture report, first committed report and review template (S10)

Spec §3.9, §4.11.

**Files:**
- Create: `tools/health/architecture.py`, `tools/health/tests/test_architecture.py`
- Modify: `tools/health/__main__.py` (`report --architecture`)
- Create: `docs/quality/README.md`, `docs/quality/2026-10-15-baseline.md`, `docs/quality/2026-10-15-baseline.json`
- Modify: `mkdocs.yml` (`exclude_docs`)

**Interfaces:**
- Consumes: `graph.import_graph`, `graph.coupling`, `graph.component`, `graph.module_name`, `graph.strongly_connected`, `static.python_files`, `static.relative` (Task 13); `external.complexity_from_json`, `external.run`, `external.venv_tool` (Task 14).
- Produces: `python -m tools.health report --architecture OUTPUT.md [--previous PREVIOUS.md]` writing `OUTPUT.md` and `OUTPUT.json` (the JSON is what the next report's delta reads); functions `lcom4(root)`, `depth_and_pass_through(root)`, `render(data, previous) -> str`, `write_report(root, output, previous)`.

- [ ] **Step 1: Write the failing tests**

Create `tools/health/tests/test_architecture.py`:

```python
"""Architecture report sections over a throwaway package."""

from pathlib import Path

from tools.health import architecture


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_lcom4_finds_two_unrelated_method_clusters(tmp_path):
    write(
        tmp_path,
        "src/tychora/store.py",
        "class Store:\n"
        "    def read(self):\n        return self.db\n"
        "    def write(self):\n        self.db = 1\n"
        "    def render(self):\n        return self.template\n",
    )
    [row] = architecture.lcom4(tmp_path)
    assert row["lcom4"] == 2
    assert row["class"].endswith("Store")


def test_pass_through_detects_exact_forwarding_only(tmp_path):
    write(
        tmp_path,
        "src/tychora/client.py",
        "def add(source, key):\n    return service.add(source, key)\n"
        "def wait(job):\n    return service.wait(job, timeout=30)\n",
    )
    depth, forwarding = architecture.depth_and_pass_through(tmp_path)
    assert forwarding == ["src/tychora/client.py:1 add"]
    assert depth[0]["module"] == "src/tychora/client.py"


def test_render_shows_deltas_against_the_previous_report():
    data = {
        "coupling": [
            {"component": "application", "modules": 31, "ca": 14, "ce": 18, "i": 0.56, "a": 0.0, "d": 0.44}
        ],
        "cycles": [60, 4],
        "complexity": [("src/tychora/x.py::f", 240)],
        "complexity_over_15": 228,
        "lcom4": [],
        "maintainability_c": [],
        "depth_median": 12.4,
        "shallowest": [],
        "co_change": {"commits": 10, "median_files": 4, "share_three_or_more_packages": 0.4},
        "co_change_pairs": [],
        "pass_through": [],
    }
    previous = {
        "coupling": [{"component": "application", "ca": 15, "ce": 18}],
        "cycles": [63, 4, 3, 2, 2, 2],
        "complexity_over_15": 229,
        "depth_median": 12.4,
    }
    text = architecture.render(data, previous)
    assert "| `application` | 31 | 14 (-1) | 18 (=) |" in text
    assert "64 modules (-12)" in text
    assert "Functions above 15: 228 (-1)." in text
```

Run: `uv run --locked pytest tools/health/tests/test_architecture.py -q`
Expected: `ImportError: cannot import name 'architecture'`.

- [ ] **Step 2: Implement `tools/health/architecture.py`**

```python
"""Architecture report: coupling, cycles, complexity, cohesion, depth, co-change, pass-through."""

from __future__ import annotations

import ast
import json
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health import graph
from tools.health.external import complexity_from_json, run, venv_tool
from tools.health.static import python_files, relative

ABSTRACT_BASES = frozenset({"Protocol", "ABC"})


def _bases(node: ast.ClassDef) -> set[str]:
    return {getattr(base, "id", getattr(base, "attr", "")) for base in node.bases}


def martin_metrics(root: Path) -> list[dict[str, Any]]:
    """Ca, Ce, instability I, abstractness A and distance D per component."""
    imports = graph.import_graph(root, top_level_only=False)
    coupling = graph.coupling(imports)
    classes: Counter[str] = Counter()
    abstract: Counter[str] = Counter()
    modules: Counter[str] = Counter()
    for path in python_files(root):
        name = graph.component(graph.module_name(root, path))
        modules[name] += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                classes[name] += 1
                abstract[name] += bool(_bases(node) & ABSTRACT_BASES)
    rows = []
    for name, (ca, ce) in coupling.items():
        instability = ce / (ca + ce) if ca + ce else 0.0
        abstractness = abstract[name] / classes[name] if classes[name] else 0.0
        rows.append(
            {
                "component": name,
                "modules": modules[name],
                "ca": ca,
                "ce": ce,
                "i": round(instability, 2),
                "a": round(abstractness, 2),
                "d": round(abs(abstractness + instability - 1), 2),
            }
        )
    return rows


def lcom4(root: Path) -> list[dict[str, Any]]:
    """Connected components of each non-protocol class with three or more methods."""
    rows = []
    for path in python_files(root):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or _bases(node) & ABSTRACT_BASES:
                continue
            methods = [
                item
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and not (item.name.startswith("__") and item.name.endswith("__"))
                and not any(
                    getattr(d, "id", "") == "staticmethod" for d in item.decorator_list
                )
            ]
            if len(methods) < 3:
                continue
            names = {method.name for method in methods}
            uses = {
                method.name: {
                    sub.attr
                    for sub in ast.walk(method)
                    if isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id in {"self", "cls"}
                }
                for method in methods
            }
            parent = {name: name for name in names}
            ordered = sorted(names)
            for index, first in enumerate(ordered):
                for second in ordered[index + 1 :]:
                    shared = (uses[first] - names) & (uses[second] - names)
                    if shared or second in uses[first] or first in uses[second]:
                        parent[_find(parent, first)] = _find(parent, second)
            components = len({_find(parent, name) for name in names})
            if components > 1:
                rows.append(
                    {
                        "class": f"{relative(root, path)}:{node.lineno} {node.name}",
                        "lcom4": components,
                        "methods": len(methods),
                    }
                )
    return sorted(rows, key=lambda row: (-row["lcom4"], row["class"]))


def _find(parent: dict[str, str], name: str) -> str:
    while parent[name] != name:
        parent[name] = parent[parent[name]]
        name = parent[name]
    return name


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = node.args
    names = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return [name for name in names if name not in {"self", "cls"}]


def depth_and_pass_through(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Lines per interface element per module, and exact forwarding functions."""
    depth, forwarding = [], []
    for path in python_files(root):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        code = sum(
            1
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        elements = 0
        for node in tree.body:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ) and not node.name.startswith("_"):
                elements += 1 + len(_parameters(node))
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                elements += 1
                for item in node.body:
                    if isinstance(
                        item, ast.FunctionDef | ast.AsyncFunctionDef
                    ) and not item.name.startswith("_"):
                        elements += 1 + len(_parameters(item))
        if elements:
            depth.append(
                {
                    "module": relative(root, path),
                    "lines": code,
                    "depth": round(code / elements, 1),
                }
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _forwards(
                node
            ):
                forwarding.append(f"{relative(root, path)}:{node.lineno} {node.name}")
    return sorted(depth, key=lambda row: row["depth"]), forwarding


def _forwards(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [
        s
        for s in node.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return | ast.Expr):
        return False
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    parameters = _parameters(node)
    if not isinstance(value, ast.Call) or not parameters:
        return False
    passed = [a.id if isinstance(a, ast.Name) else None for a in value.args]
    passed += [
        k.value.id if isinstance(k.value, ast.Name) else None for k in value.keywords
    ]
    return None not in passed and sorted(p for p in passed if p) == sorted(parameters)


def co_change(
    root: Path, limit: int = 20
) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
    """Share of commits touching three or more packages, and the strongest file pairs."""
    log = subprocess.run(
        ["git", "log", "--name-only", "--format=@@%n", "--", "src/tychora"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    commits = [
        sorted({line for line in chunk.splitlines() if line.startswith("src/tychora/")})
        for chunk in log.split("@@")
        if chunk.strip()
    ]
    commits = [files for files in commits if files]
    touches: Counter[str] = Counter(f for files in commits for f in files)
    pairs: Counter[tuple[str, str]] = Counter()
    for files in commits:
        for index, first in enumerate(files):
            for second in files[index + 1 :]:
                if first.split("/")[2] != second.split("/")[2]:
                    pairs[(first, second)] += 1
    wide = sum(1 for files in commits if len({f.split("/")[2] for f in files}) >= 3)
    summary = {
        "commits": len(commits),
        "median_files": statistics.median(len(files) for files in commits)
        if commits
        else 0,
        "share_three_or_more_packages": round(wide / len(commits), 2) if commits else 0,
    }
    strongest = sorted(
        (
            (a, b, round(n / min(touches[a], touches[b]), 2))
            for (a, b), n in pairs.items()
            if n >= 3
        ),
        key=lambda row: -row[2],
    )[:limit]
    return summary, strongest


def maintainability(root: Path) -> list[dict[str, Any]]:
    """Files ranked C by radon's maintainability index."""
    result = run([venv_tool("radon"), "mi", "-j", "src/tychora"], root)
    data = json.loads(result.stdout)
    return sorted(
        (
            {"file": path, "mi": round(item["mi"], 1)}
            for path, item in data.items()
            if item.get("rank") == "C"
        ),
        key=lambda row: row["mi"],
    )


def collect(root: Path) -> dict[str, Any]:
    """Every report section as plain data, also written next to the Markdown."""
    complexipy = root / "reports" / "complexipy.json"
    complexipy.parent.mkdir(exist_ok=True)
    run(
        [
            venv_tool("complexipy"),
            "src/tychora",
            "--output-format",
            "json",
            "--output",
            str(complexipy),
            "--quiet",
            "--ignore-complexity",
        ],
        root,
    )
    complexity = complexity_from_json(json.loads(complexipy.read_text()))
    cycles = graph.strongly_connected(graph.import_graph(root, top_level_only=False))
    depth, forwarding = depth_and_pass_through(root)
    summary, pairs = co_change(root)
    return {
        "coupling": martin_metrics(root),
        "cycles": [len(component) for component in cycles],
        "complexity": sorted((complexity.per_key or {}).items(), key=lambda kv: -kv[1])[
            :20
        ],
        "complexity_over_15": complexity.value,
        "lcom4": lcom4(root),
        "maintainability_c": maintainability(root),
        "depth_median": statistics.median(row["depth"] for row in depth),
        "shallowest": [row for row in depth if row["lines"] >= 40][:10],
        "co_change": summary,
        "co_change_pairs": pairs,
        "pass_through": forwarding,
    }


def _delta(current: float, previous: float | None) -> str:
    if previous is None:
        return ""
    change = round(current - previous, 2)
    return f" ({change:+g})" if change else " (=)"


def render(data: dict[str, Any], previous: dict[str, Any] | None) -> str:
    """Markdown with a delta against the previous report's JSON where one exists."""
    old = previous or {}
    old_cycles = sum(old["cycles"]) if "cycles" in old else None
    old_rows = {row["component"]: row for row in old.get("coupling", [])}
    lines = [
        "# Architecture quality report",
        "",
        "## Coupling (Martin metrics)",
        "",
        "| Component | Modules | Ca | Ce | I | A | D |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in data["coupling"]:
        before = old_rows.get(row["component"], {})
        lines.append(
            f"| `{row['component']}` | {row['modules']} | {row['ca']}{_delta(row['ca'], before.get('ca'))} "
            f"| {row['ce']}{_delta(row['ce'], before.get('ce'))} | {row['i']} | {row['a']} | {row['d']} |"
        )
    lines += [
        "",
        "## Cycles (all imports)",
        "",
        (
            f"{len(data['cycles'])} strongly connected components, sizes {data['cycles']}, "
            f"{sum(data['cycles'])} modules{_delta(sum(data['cycles']), old_cycles)}."
        ),
        "",
        "## Cognitive complexity (top 20)",
        "",
        f"Functions above 15: {data['complexity_over_15']}{_delta(data['complexity_over_15'], old.get('complexity_over_15'))}.",
        "",
    ]
    lines += [f"- `{key}`: {value:g}" for key, value in data["complexity"]]
    lines += ["", "## Class cohesion (LCOM4 > 1)", ""]
    lines += [
        f"- `{row['class']}`: LCOM4 {row['lcom4']} over {row['methods']} methods"
        for row in data["lcom4"]
    ]
    lines += ["", "## Maintainability index rank C", ""]
    lines += [f"- `{row['file']}`: {row['mi']}" for row in data["maintainability_c"]]
    lines += [
        "",
        "## Module depth",
        "",
        (
            f"Median lines per interface element: {data['depth_median']}"
            f"{_delta(data['depth_median'], old.get('depth_median'))}."
        ),
        "",
    ]
    lines += [
        f"- `{row['module']}`: {row['depth']} ({row['lines']} lines)"
        for row in data["shallowest"]
    ]
    summary = data["co_change"]
    lines += [
        "",
        "## Change amplification",
        "",
        (
            f"{summary['commits']} commits; median {summary['median_files']} files; "
            f"{summary['share_three_or_more_packages']:.0%} touch three or more packages."
        ),
        "",
    ]
    lines += [
        f"- `{a}` with `{b}`: {share:.0%}" for a, b, share in data["co_change_pairs"]
    ]
    lines += [
        "",
        "## Pass-through functions",
        "",
        f"{len(data['pass_through'])} exact forwarding functions.",
        "",
    ]
    lines += [f"- `{item}`" for item in data["pass_through"]]
    return "\n".join(lines) + "\n"


def write_report(root: Path, output: Path, previous: Path | None) -> None:
    """Write OUTPUT (Markdown) and OUTPUT with a .json suffix for the next delta."""
    data = collect(root)
    earlier = (
        json.loads(previous.with_suffix(".json").read_text()) if previous else None
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(data, earlier))
    output.with_suffix(".json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
```

- [ ] **Step 3: Wire the report into the command line**

In `tools/health/__main__.py` add `from tools.health.architecture import write_report` to the imports. In `main`, after `command.add_argument("--initial", action="store_true")` add:

```python
        if name == "report":
            command.add_argument("--architecture", type=Path, metavar="MARKDOWN")
            command.add_argument("--previous", type=Path, metavar="MARKDOWN")
```

and before `gates = select(...)` add:

```python
    if args.command == "report" and args.architecture:
        write_report(args.root, args.architecture, args.previous)
        return 0
```

- [ ] **Step 4: Run the tests and write the first report**

```bash
uv run --locked pytest tools/health/tests -q
uv run --locked python -m tools.health report --architecture docs/quality/2026-10-15-baseline.md
head -30 docs/quality/2026-10-15-baseline.md
```

Expected: tests pass; the report's coupling table matches spec §3.9 (`contracts` Ca 22 Ce 2, `configuration` 16/1, `ports` 19/4, `application` 15/18, `adapters.sqlite` 3/9); cycles `[63, 4, 3, 2, 2, 2]`, 76 modules; LCOM4 lists `ReadingOrder` 8 and `CorpusStore` 5; 14 pass-through functions. Prepend this line under the title of the generated Markdown: `Baseline of the code-health sub-project, measured on <short commit> (spec section 3.9).`

- [ ] **Step 5: Add the review template and keep the reports off the site**

Create `docs/quality/README.md`:

```markdown
# Architecture quality reports

Each sub-project of the Tychora v2 programme ends by committing `docs/quality/<date>-<sub-project>.md`, written by

    uv run --locked python -m tools.health report --architecture docs/quality/<date>-<sub-project>.md --previous docs/quality/<previous report>.md

The nightly workflow also uploads a report as a CI artifact (kept 90 days); those are not committed.

## Review section "Architecture quality"

Copy this section into the final review of every sub-project and fill it in.

1. **Reports.** Link the report before and after the sub-project.
2. **Rubric** (code-health design 4.11.2). For the code the sub-project touched, answer with numbers:
   - Deep modules: does each new module hide more than it exposes? (module depth, public symbols)
   - Information leakage: does knowledge of one representation live in one module? (raw SQL outside adapters, co-change pairs)
   - Pass-through methods and variables: does each layer add behaviour? (pass-through list)
   - Change amplification: how many files does one conceptual change touch? (co-change)
   - Cognitive load: can a reader hold each function in their head? (cognitive complexity, function length)
   - Define errors out of existence: could an error be made impossible instead of checked? (integrity raises without a reason)
   - Comments say what the code cannot: does each comment add intent, units, invariants or reasons? (docstring gates, plan references)
   - Pull complexity downwards: does the callee absorb configuration instead of every caller? (constructor parameters, PLR0913)
3. **SOLID rows** (design 4.11.3) the sub-project was expected to remove, with evidence: removed `ignore_imports` lines, lower integrity-raise count, smaller co-change sets.
4. **Worse report metrics**, each explained. A worse report metric does not block the merge; an unexplained one blocks the review.
```

In `mkdocs.yml` extend `exclude_docs`:

```yaml
exclude_docs: |
  /superpowers/
  /quality/
```

Run `uv run --project packaging/docs --locked mkdocs build --strict` — expected: success.

- [ ] **Step 6: Commit**

```bash
git add tools/health docs/quality mkdocs.yml
git commit -m "feat(health): architecture report with deltas, first baseline report and review template"
```

---

### Task 19: Code gold set (S8)

Spec §4.9, §8 R2 (fallback: Tychora's own source at a pinned commit). The corpus never enters the repository; queries and labels do.

**Files:**
- Create: `tools/health/convert_gold.py`, `tools/health/tests/test_convert_gold.py`
- Create: `tests/fixtures/evaluation/code-gold/queries.json`, `tests/fixtures/evaluation/code-gold/cases.template.jsonl` (generated), `tests/fixtures/evaluation/code-gold/baseline.json` (generated)
- Create: `tests/helpers/code_gold.py`, `tests/evaluation/test_code_gold.py`
- Modify: `packaging/provision_ci_assets.py`, `tests/release/test_ci_assets.py`

**Interfaces:**
- Consumes: `asset_path`, marker `assets("TYCHORA_TEST_CODE_CORPUS")` (Task 4); `scaled` (Task 6); `tests.code.conftest.git`; `tests.evaluation.conftest.SyntheticCounter`.
- Produces: `provision_code_corpus(repository: Path, destination: Path, *, commit=CODE_CORPUS_COMMIT, tree=CODE_CORPUS_TREE) -> Path` and `--code-corpus PATH` in `packaging/provision_ci_assets.py` (exports `TYCHORA_TEST_CODE_CORPUS`); `TYCHORA_CODE_GOLD_REPORT` (when set, the gold test writes `{"dataset", "variants": {variant: {metric: mean}}}` there; nightly sets `reports/code-gold.json`, read by `testruns.gold_set`).

- [ ] **Step 1: Write the failing tests for the converter and the corpus export**

Create `tools/health/tests/test_convert_gold.py`:

```python
"""Golden-query conversion keeps families inside one split."""

from tools.health.convert_gold import convert, development_families


def entry(case_id, family, required=True):
    return {
        "id": case_id,
        "query": f"where is {case_id}",
        "expected_files": [
            {"path": "src/tychora/a.py", "required": required, "max_rank": 3},
            {"path": "src/tychora/b.py", "required": False, "max_rank": 10},
        ],
        "expected_symbols": [{"path": "src/tychora/a.py", "name": "a"}],
        "tags": [family, "extra"],
    }


def test_conversion_shape_and_required_paths():
    [template] = convert([entry("one", "identity")])
    assert template["required_paths"] == ["src/tychora/a.py"]
    assert template["optional_paths"] == ["src/tychora/b.py"]
    assert template["max_rank"] == 3
    assert template["family_key"] == "identity"
    assert template["benchmark"] == "semantic_code"
    assert template["query"]["question"] == "where is one"
    assert template["query"]["modalities"] == ["lexical"]


def test_at_least_ten_development_cases_and_families_never_split():
    entries = [entry(f"case-{i}", f"family-{i % 7}") for i in range(30)]
    templates = convert(entries)
    development = [t for t in templates if t["split"] == "development"]
    assert len(development) >= 10
    splits_by_family: dict[str, set[str]] = {}
    for template in templates:
        splits_by_family.setdefault(template["family_key"], set()).add(template["split"])
    assert all(len(splits) == 1 for splits in splits_by_family.values())
    assert development_families(entries) == development_families(list(reversed(entries)))
```

Append to `tests/release/test_ci_assets.py`:

```python
def test_code_corpus_export_is_pinned_by_git_tree(tmp_path: Path):
    module = _provisioner()
    repo = tmp_path / "repo"
    (repo / "src/tychora").mkdir(parents=True)
    (repo / "src/tychora/a.py").write_text("VALUE = 1\n")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD:src/tychora")
    exported = module.provision_code_corpus(repo, tmp_path / "corpus", commit=commit, tree=tree)
    assert (exported / "src/tychora/a.py").read_text() == "VALUE = 1\n"
    with pytest.raises(SystemExit, match="differs from the pinned tree"):
        module.provision_code_corpus(repo, tmp_path / "other", commit=commit, tree="0" * 40)
```

Run: `uv run --locked pytest tools/health/tests/test_convert_gold.py tests/release/test_ci_assets.py -q`
Expected: `ModuleNotFoundError: No module named 'tools.health.convert_gold'` and `AttributeError: module 'provision_ci_assets' has no attribute 'provision_code_corpus'`.

- [ ] **Step 2: Implement the converter**

Create `tools/health/convert_gold.py`:

```python
"""Convert golden queries into Tychora code gold-case templates (tychora-gold@1 without IDs).

Input entries carry `id`, `query`, `expected_files` (each with `path`, `required` and
`max_rank`), `expected_symbols` (each with `path` and `name`) and `tags`, the schema
of the earlier code-search project's golden_queries.yaml. Output lines keep paths
and symbol names; tests/helpers/code_gold.py resolves them to the workspace-specific
file and definition identities after ingestion.

Run: uv run --locked python -m tools.health.convert_gold QUERIES OUTPUT
QUERIES may be JSON or, for the earlier project's file, YAML (PyYAML is in the
locked full environment).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

DEVELOPMENT_CASES = 10
PROVENANCE = (
    "Queries and labels written by the maintainer against src/tychora at "
    "700df656fb391aa5eaa41a75ce27eed432be442c and reviewed before commit"
)
QUERY_DEFAULTS: dict[str, Any] = {
    "modalities": ["lexical"],
    "limit": 10,
    "budgets": {"max_tokens": 30000, "tokenizer_id": "cl100k_base"},
}


def _stable(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def development_families(entries: list[dict[str, Any]]) -> set[str]:
    """Families, in stable hash order, until at least ten cases are for development."""
    sizes: dict[str, int] = {}
    for entry in entries:
        family = entry["tags"][0]
        sizes[family] = sizes.get(family, 0) + 1
    chosen: set[str] = set()
    count = 0
    for family in sorted(sizes, key=_stable):
        if count >= DEVELOPMENT_CASES:
            break
        chosen.add(family)
        count += sizes[family]
    return chosen


def convert(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One template per query; the first tag is the family, so families never cross splits."""
    development = development_families(entries)
    templates = []
    for entry in entries:
        family = entry["tags"][0]
        files = entry["expected_files"]
        templates.append(
            {
                "case_id": entry["id"],
                "family_key": family,
                "split": "development" if family in development else "test",
                "benchmark": "semantic_code",
                "label_provenance": PROVENANCE,
                "query": {"question": entry["query"], **QUERY_DEFAULTS},
                "required_paths": [f["path"] for f in files if f.get("required", True)],
                "optional_paths": [
                    f["path"] for f in files if not f.get("required", True)
                ],
                "max_rank": min(f.get("max_rank", 10) for f in files),
                "symbols": entry.get("expected_symbols", []),
            }
        )
    return sorted(templates, key=lambda template: template["case_id"])


def load_queries(path: Path) -> list[dict[str, Any]]:
    """Read the query list from JSON, or YAML when the file ends in .yaml or .yml."""
    if path.suffix in {".yaml", ".yml"}:
        return importlib.import_module("yaml").safe_load(path.read_text())
    return json.loads(path.read_text())


def main(argv: list[str]) -> int:
    """Convert QUERIES to OUTPUT (JSON Lines, one template per case)."""
    source, output = Path(argv[0]), Path(argv[1])
    entries = load_queries(source)
    lines = [json.dumps(template, sort_keys=True) for template in convert(entries)]
    output.write_text("\n".join(lines) + "\n")
    print(f"{len(lines)} cases written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 3: Export the pinned corpus from Git in the provisioning script**

In `packaging/provision_ci_assets.py` add `import io` and `import tarfile` to the imports, and after `SELECTIONS = ...` add:

```python
CODE_CORPUS_COMMIT = "700df656fb391aa5eaa41a75ce27eed432be442c"
CODE_CORPUS_TREE = "34fa1cac9248f208c5dfaa34347674a4f6d6d26b"


def provision_code_corpus(
    repository: Path,
    destination: Path,
    *,
    commit: str = CODE_CORPUS_COMMIT,
    tree: str = CODE_CORPUS_TREE,
) -> Path:
    """Export src/tychora at the pinned commit; Git verifies the bytes by tree id."""

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(repository), *args], capture_output=True, check=False
        )

    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        if git("fetch", "--quiet", "--depth", "1", "origin", commit).returncode != 0:
            raise SystemExit(f"cannot fetch the pinned code corpus commit {commit}")
    actual = git("rev-parse", f"{commit}:src/tychora").stdout.decode().strip()
    if actual != tree:
        raise SystemExit(f"code corpus tree {actual} differs from the pinned tree {tree}")
    archive = git("archive", "--format=tar", commit, "src/tychora")
    if archive.returncode != 0:
        raise SystemExit(f"cannot export the code corpus: {archive.stderr.decode()}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(destination, filter="data")
    return destination
```

In `main()`, add the argument after `--bundle`:

```python
    parser.add_argument(
        "--code-corpus",
        type=Path,
        help="Export the pinned code gold-set corpus (Tychora's own source) here",
    )
```

and before `with args.github_env.open("a") as stream:`:

```python
    if args.code_corpus:
        lines["TYCHORA_TEST_CODE_CORPUS"] = provision_code_corpus(
            Path(__file__).parents[1], args.code_corpus
        )
```

Run: `uv run --locked pytest tools/health/tests/test_convert_gold.py tests/release/test_ci_assets.py -q` — Expected: PASS.

- [ ] **Step 4: Write the queries**

Create `tests/fixtures/evaluation/code-gold/queries.json` (33 queries; every path and symbol exists at `700df656`; check with `git show 700df656fb391aa5eaa41a75ce27eed432be442c:<path>`):

```json
[
  {"id": "identity-source-id", "query": "how is a stable source identifier derived from a collection and a source key", "expected_files": [{"path": "src/tychora/contracts/identity.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/contracts/identity.py", "name": "source_id"}], "tags": ["identity"]},
  {"id": "identity-canonical-json", "query": "canonical JSON bytes used to hash identities", "expected_files": [{"path": "src/tychora/contracts/identity.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/contracts/identity.py", "name": "canonical_bytes"}, {"path": "src/tychora/contracts/identity.py", "name": "digest"}], "tags": ["identity"]},
  {"id": "identity-source-key", "query": "reject parent traversal and absolute paths in source keys", "expected_files": [{"path": "src/tychora/contracts/identity.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/contracts/identity.py", "name": "normalize_source_key"}], "tags": ["identity"]},
  {"id": "fusion-rrf", "query": "reciprocal rank fusion score of ranked candidates", "expected_files": [{"path": "src/tychora/retrieval/fusion.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/retrieval/fusion.py", "name": "rrf_score"}, {"path": "src/tychora/retrieval/fusion.py", "name": "fuse"}], "tags": ["fusion"]},
  {"id": "lexical-identifiers", "query": "extract exact identifiers from a query for lexical lookup", "expected_files": [{"path": "src/tychora/retrieval/lexical_query.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/retrieval/lexical_query.py", "name": "extract_identifiers"}], "tags": ["lexical"]},
  {"id": "lexical-expression", "query": "build the full text search expression for lexical retrieval", "expected_files": [{"path": "src/tychora/retrieval/lexical_query.py", "required": true, "max_rank": 3}, {"path": "src/tychora/adapters/sqlite/lexical.py", "required": false, "max_rank": 10}], "expected_symbols": [{"path": "src/tychora/retrieval/lexical_query.py", "name": "lexical_expression"}], "tags": ["lexical"]},
  {"id": "lexical-index", "query": "SQLite lexical index lookup of identifiers", "expected_files": [{"path": "src/tychora/adapters/sqlite/lexical.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/sqlite/lexical.py", "name": "LexicalIndex"}, {"path": "src/tychora/adapters/sqlite/lexical.py", "name": "lookup_identifiers"}], "tags": ["lexical"]},
  {"id": "local-schemas", "query": "validate JSON schema documents without retrieving remote references", "expected_files": [{"path": "src/tychora/configuration/schemas.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/configuration/schemas.py", "name": "LocalSchemas"}], "tags": ["configuration"]},
  {"id": "workspace-owner", "query": "single process owns a workspace with an operating system lock", "expected_files": [{"path": "src/tychora/application/owner.py", "required": true, "max_rank": 5}], "expected_symbols": [{"path": "src/tychora/application/owner.py", "name": "WorkspaceOwner"}], "tags": ["runtime"]},
  {"id": "chroma-index", "query": "Chroma persistent client vector family collection", "expected_files": [{"path": "src/tychora/adapters/chroma/index.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/chroma/index.py", "name": "ChromaVectorIndex"}], "tags": ["vectors"]},
  {"id": "voyage-rerank", "query": "build the Voyage rerank request body and map the response", "expected_files": [{"path": "src/tychora/adapters/voyage/rerank.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/voyage/rerank.py", "name": "build_rerank_body"}, {"path": "src/tychora/adapters/voyage/rerank.py", "name": "map_rerank_response"}], "tags": ["providers"]},
  {"id": "artifact-store", "query": "content addressed artifact storage with staged writes", "expected_files": [{"path": "src/tychora/adapters/files/cas.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/files/cas.py", "name": "LocalArtifacts"}], "tags": ["storage"]},
  {"id": "snapshot-publication", "query": "publish a snapshot and resolve the active snapshot", "expected_files": [{"path": "src/tychora/adapters/sqlite/publication.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/sqlite/publication.py", "name": "publish_snapshot"}, {"path": "src/tychora/adapters/sqlite/publication.py", "name": "resolve_snapshot"}], "tags": ["publication"]},
  {"id": "snapshot-manifest", "query": "snapshot publication manifest", "expected_files": [{"path": "src/tychora/adapters/sqlite/publication.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/sqlite/publication.py", "name": "publication_manifest"}], "tags": ["publication"]},
  {"id": "eligibility-facts", "query": "read eligibility facts and the policy epoch for a query", "expected_files": [{"path": "src/tychora/adapters/sqlite/eligibility.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/sqlite/eligibility.py", "name": "read_eligibility_facts"}, {"path": "src/tychora/adapters/sqlite/eligibility.py", "name": "policy_epoch"}], "tags": ["eligibility"]},
  {"id": "http-routes", "query": "register the FastAPI HTTP routes", "expected_files": [{"path": "src/tychora/interfaces/http/routes.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/interfaces/http/routes.py", "name": "register_routes"}], "tags": ["transports"]},
  {"id": "mcp-tools", "query": "register MCP tools for the evidence server", "expected_files": [{"path": "src/tychora/interfaces/mcp/tools.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/interfaces/mcp/tools.py", "name": "register_tools"}], "tags": ["transports"]},
  {"id": "cli-entry", "query": "command line entry point of the tychora program", "expected_files": [{"path": "src/tychora/interfaces/cli.py", "required": true, "max_rank": 5}], "expected_symbols": [{"path": "src/tychora/interfaces/cli.py", "name": "main"}], "tags": ["transports"]},
  {"id": "python-adapter", "query": "tree-sitter Python definitions and imports adapter", "expected_files": [{"path": "src/tychora/adapters/code/python.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/code/python.py", "name": "PythonAdapter"}], "tags": ["code"]},
  {"id": "typescript-adapter", "query": "TypeScript overloads and exports adapter", "expected_files": [{"path": "src/tychora/adapters/code/typescript.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/code/typescript.py", "name": "TypeScriptAdapter"}], "tags": ["code"]},
  {"id": "ranking-metrics", "query": "normalized discounted cumulative gain and mean reciprocal rank", "expected_files": [{"path": "src/tychora/evaluation/metrics.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/evaluation/metrics.py", "name": "ndcg"}, {"path": "src/tychora/evaluation/metrics.py", "name": "mrr"}], "tags": ["evaluation"]},
  {"id": "gold-dataset", "query": "load a gold dataset manifest and verify its version digest", "expected_files": [{"path": "src/tychora/evaluation/dataset.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/evaluation/dataset.py", "name": "load_dataset"}, {"path": "src/tychora/evaluation/dataset.py", "name": "GoldCase"}], "tags": ["evaluation"]},
  {"id": "job-repository", "query": "durable job repository with events and cancellation", "expected_files": [{"path": "src/tychora/adapters/sqlite/jobs.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/sqlite/jobs.py", "name": "JobRepository"}], "tags": ["jobs"]},
  {"id": "restore-backup", "query": "restore a workspace from a backup archive", "expected_files": [{"path": "src/tychora/application/restore.py", "required": true, "max_rank": 3}, {"path": "src/tychora/application/backup.py", "required": false, "max_rank": 10}], "expected_symbols": [{"path": "src/tychora/application/restore.py", "name": "RestoreService"}], "tags": ["operations"]},
  {"id": "backup-service", "query": "create a backup archive with a manifest and a receipt", "expected_files": [{"path": "src/tychora/application/backup.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/application/backup.py", "name": "BackupService"}, {"path": "src/tychora/application/backup.py", "name": "BackupManifest"}], "tags": ["operations"]},
  {"id": "response-assembly", "query": "assemble the query response within a token budget", "expected_files": [{"path": "src/tychora/retrieval/response.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/retrieval/response.py", "name": "ResponseAssembler"}], "tags": ["response"]},
  {"id": "segments-tokens", "query": "count output tokens and select evidence segments", "expected_files": [{"path": "src/tychora/retrieval/segments.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/retrieval/segments.py", "name": "OutputTokenCounter"}, {"path": "src/tychora/retrieval/segments.py", "name": "assemble_segments"}], "tags": ["response"]},
  {"id": "error-envelope", "query": "error envelope with a code, a message and a retryable flag", "expected_files": [{"path": "src/tychora/contracts/errors.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/contracts/errors.py", "name": "ErrorEnvelope"}, {"path": "src/tychora/contracts/errors.py", "name": "TychoraError"}], "tags": ["contracts"]},
  {"id": "url-acquisition", "query": "acquire a source from a URL with bounded downloads", "expected_files": [{"path": "src/tychora/adapters/files/url_acquisition.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/files/url_acquisition.py", "name": "UrlAcquisition"}], "tags": ["acquisition"]},
  {"id": "source-capture", "query": "capture a source into the artifact store", "expected_files": [{"path": "src/tychora/ingestion/acquire.py", "required": true, "max_rank": 5}], "expected_symbols": [{"path": "src/tychora/ingestion/acquire.py", "name": "Acquirer"}], "tags": ["acquisition"]},
  {"id": "graph-traversal", "query": "graph traversal policy for relation expansion", "expected_files": [{"path": "src/tychora/adapters/ladybug/traversal.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/adapters/ladybug/traversal.py", "name": "GraphTraversal"}, {"path": "src/tychora/adapters/ladybug/traversal.py", "name": "resolve_graph_traversal_policy"}], "tags": ["graph"]},
  {"id": "query-caches", "query": "query result caches with a monotonic clock", "expected_files": [{"path": "src/tychora/retrieval/cache.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/retrieval/cache.py", "name": "QueryCaches"}], "tags": ["caches"]},
  {"id": "embedding-windows", "query": "split an evidence unit into embedding windows", "expected_files": [{"path": "src/tychora/indexing/windows.py", "required": true, "max_rank": 3}], "expected_symbols": [{"path": "src/tychora/indexing/windows.py", "name": "window_unit"}], "tags": ["indexing"]}
]
```

Generate the templates:

```bash
uv run --locked python -m tools.health.convert_gold tests/fixtures/evaluation/code-gold/queries.json tests/fixtures/evaluation/code-gold/cases.template.jsonl
grep -c '"split": "development"' tests/fixtures/evaluation/code-gold/cases.template.jsonl
```

Expected: `33 cases written`; at least 10 development cases.

- [ ] **Step 5: Write the resolver and the evaluation test**

Create `tests/helpers/code_gold.py`:

```python
"""Resolve code gold-case templates to the identities of one ingested repository."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from tychora.contracts.identity import canonical_bytes

TEMPLATES = Path(__file__).parents[1] / "fixtures/evaluation/code-gold/cases.template.jsonl"
DATASET_ID = "tychora-code-gold-v1"
TEMPLATE_ONLY = ("required_paths", "optional_paths", "max_rank", "symbols")


def load_templates(path: Path = TEMPLATES) -> list[dict[str, Any]]:
    """Every template line of the committed conversion."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def materialize(
    template: dict[str, Any],
    *,
    source_id: str,
    revision_id: str,
    definitions: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    """A tychora-gold@1 case: required files are recall targets, optional ones judged only."""

    def file_id(path: str) -> str:
        return f"{source_id}:{revision_id}:{path}"

    case = {key: value for key, value in template.items() if key not in TEMPLATE_ONLY}
    required = [file_id(path) for path in template["required_paths"]]
    optional = [file_id(path) for path in template["optional_paths"]]
    case["expected_source_ids"] = [source_id]
    case["expected_revision_ids"] = [revision_id]
    case["expected_file_ids"] = required
    case["file_relevance"] = {**{f: 1 for f in optional}, **{f: 3 for f in required}}
    case["expected_definition_ids"] = sorted(
        key
        for symbol in template["symbols"]
        for key in definitions[(symbol["path"], symbol["name"])]
    )
    return case


def write_dataset(directory: Path, cases: list[dict[str, Any]]) -> None:
    """Write manifest.json and cases.jsonl exactly as the loader verifies them."""
    payload = b"".join(canonical_bytes(case) + b"\n" for case in cases)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cases.jsonl").write_bytes(payload)
    (directory / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": DATASET_ID,
                "name": "Tychora source code retrieval",
                "version_sha": sha256(payload).hexdigest(),
                "case_count": len(cases),
            }
        )
    )


def mean_metrics(
    report: dict[str, Any], names: tuple[str, ...]
) -> dict[str, dict[str, float]]:
    """Mean of each metric per variant over the cases where it applies."""
    sums: dict[str, dict[str, list[float]]] = {}
    for entry in report["cases"].values():
        for run in entry["runs"]:
            values = {m["name"]: m["value"] for m in run["metrics"]["metrics"]}
            bucket = sums.setdefault(run["variant"], {name: [] for name in names})
            for name in names:
                if values.get(name) is not None:
                    bucket[name].append(values[name])
    return {
        variant: {
            name: round(sum(v) / len(v), 4) if v else 0.0 for name, v in metrics.items()
        }
        for variant, metrics in sorted(sums.items())
    }
```

Create `tests/evaluation/test_code_gold.py`:

```python
"""Code gold set: real queries over Tychora's own pinned source, lexical and structural."""

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.code.conftest import git
from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.assets import asset_path
from tests.helpers.code_gold import (
    DATASET_ID,
    load_templates,
    materialize,
    mean_metrics,
    write_dataset,
)
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.ports.execution import ExecutionContext

pytestmark = [pytest.mark.slow, pytest.mark.assets("TYCHORA_TEST_CODE_CORPUS")]

METRICS = ("file_recall", "definition_recall", "file_mrr", "file_ndcg")
VARIANTS = (
    {"name": "lexical", "profile": "test", "overrides": {"channels": ["lexical"], "rerank": False}},
    {
        "name": "lexical_structural",
        "profile": "test",
        "overrides": {"channels": ["lexical", "structural_graph"], "rerank": False},
    },
)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    shutil.copytree(asset_path("TYCHORA_TEST_CODE_CORPUS") / "src", repo / "src")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Gold Corpus")
    git(repo, "config", "user.email", "gold@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pinned corpus")
    return repo


def test_code_gold_set_runs_offline_with_valid_locators(tmp_path):
    repo = _repository(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n'
        "[configuration.limits]\nmax_tokens=30000\n"
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "workspace", config=config, token_counter=SyntheticCounter()
    ) as kb:
        handle = kb.sources.add(
            {
                "type": "repository",
                "path": str(repo),
                "source_key": "tychora",
                "collection_id": "default",
                "kind": "code_file@1",
                "metadata": {"title": "Tychora source"},
            }
        )
        status = kb.jobs.wait(handle.id, timeout=scaled(900))
        assert status.state == "succeeded", (status.error_code, status.message)
        owner = kb._workspace.owner

        def labels():
            return owner.db.execute(
                "SELECT s.source_id,r.revision_id,sr.data_json FROM structured_records sr "
                "JOIN evidence_units u USING(unit_id) JOIN extractions e USING(extraction_id) "
                "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
                "WHERE sr.record_type='code_symbol'"
            ).fetchall()

        rows = kb._workspace.bridge.call(owner.run_blocking(labels, ExecutionContext()))
        source_id, revision_id = rows[0][:2]
        definitions: dict[tuple[str, str], list[str]] = {}
        for _, _, data in rows:
            record = json.loads(data)
            key = (record["relative_path"], record["name"])
            definitions.setdefault(key, []).append(record["definition_key"])
        cases = [
            materialize(t, source_id=source_id, revision_id=revision_id, definitions=definitions)
            for t in load_templates()
        ]
        write_dataset(tmp_path / "gold", cases)
        kb.evaluations.register_dataset(tmp_path / "gold")
        snapshot = kb._workspace.bridge.call(
            owner.run_blocking(lambda: owner.store.resolve_snapshot(None), ExecutionContext())
        )
        run = kb.evaluations.run(
            {
                "dataset_id": DATASET_ID,
                "split": "test",
                "snapshot_id": snapshot.snapshot_id,
                "variants": list(VARIANTS),
                "seed": 0,
                "repetitions": 1,
                "offline": True,
            }
        )
        assert kb.jobs.wait(run.id, timeout=scaled(900)).state == "succeeded"
        report = kb.evaluations.get(run.id)

    test_cases = {case["case_id"] for case in cases if case["split"] == "test"}
    assert set(report["cases"]) == test_cases
    for entry in report["cases"].values():
        for result in entry["runs"]:
            values = {m["name"]: m["value"] for m in result["metrics"]["metrics"]}
            assert values["forbidden_leakage"] in (None, 0.0)
            assert values["locator_validity"] in (None, 1.0)
    means = mean_metrics(report, METRICS)
    assert set(means) == {"lexical", "lexical_structural"}
    destination = os.environ.get("TYCHORA_CODE_GOLD_REPORT")
    if destination:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(
            json.dumps({"dataset": DATASET_ID, "variants": means}, indent=2, sort_keys=True)
            + "\n"
        )
```

- [ ] **Step 6: Run the gold set and commit Tychora's baseline**

```bash
python packaging/provision_ci_assets.py --cache "$PWD/.cache/test-models" --workspace "$PWD/.cache/asset-setup" --github-env "$PWD/.cache/test-assets.env" --code-corpus "$PWD/.cache/code-corpus"
set -a; . .cache/test-assets.env; set +a
TYCHORA_CODE_GOLD_REPORT=tests/fixtures/evaluation/code-gold/baseline.json uv run --locked pytest tests/evaluation/test_code_gold.py -q
cat tests/fixtures/evaluation/code-gold/baseline.json
env -i PATH="$PATH" HOME="$HOME" uv run --locked pytest tests/evaluation/test_code_gold.py -q -rs
```

Expected: the test passes and writes per-variant means for `file_recall`, `definition_recall`, `file_mrr`, `file_ndcg`; without the variable the test is skipped with `requires TYCHORA_TEST_CODE_CORPUS`. If a symbol lookup raises `KeyError` in `materialize`, the code adapter names that definition differently: query `structured_records` for that path, correct the `name` in `queries.json`, regenerate the templates and rerun. A low recall is a finding to record, not a reason to edit queries.

- [ ] **Step 7: Commit**

```bash
git add tools/health/convert_gold.py tools/health/tests/test_convert_gold.py tests/fixtures/evaluation/code-gold tests/helpers/code_gold.py tests/evaluation/test_code_gold.py packaging/provision_ci_assets.py tests/release/test_ci_assets.py
git commit -m "test: code gold set over Tychora's pinned source with a committed baseline"
```

---

### Task 20: Nightly workflow: branch coverage, flakes, mutation, gold set, architecture report

Spec §4.1 (`health-nightly.yml`), §4.7, §4.11.4; programme resolved question 6.

**Files:**
- Create: `.github/workflows/health-nightly.yml`
- Modify: `quality/baseline.json` (nightly entries, from the CI artifact), `pyproject.toml` (`[tool.coverage.report] fail_under`)

**Interfaces:**
- Consumes: `--code-corpus` (Task 19), mutmut configuration (Task 17), `report --architecture` (Task 18), `flaky` (Task 15), nightly gates G16 (branches), G21, G22.
- Produces: artifacts `nightly-suite-reports`, `nightly-mutation-reports`, `architecture-report` (90 days), `nightly-baseline` (dispatch only).

- [ ] **Step 1: Write the workflow**

```yaml
name: Health nightly

on:
  schedule:
    - cron: "17 3 * * *"
  workflow_dispatch:
    inputs:
      baseline:
        description: Record the nightly metrics in quality/baseline.json and upload it
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  suite:
    name: Branch coverage, three seeds and the code gold set
    runs-on: ubuntu-22.04
    timeout-minutes: 150
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked stack
        run: uv sync --locked --all-extras --group dev --group test-fixtures
      - name: Provision pinned public test assets and the code corpus
        run: python packaging/provision_ci_assets.py --cache "$RUNNER_TEMP/tychora-public-models" --workspace "$RUNNER_TEMP/tychora-asset-setup" --github-env "$GITHUB_ENV" --code-corpus "$RUNNER_TEMP/tychora-code-corpus"
      - name: Seed 1 with branch coverage and the gold-set report
        env:
          TYCHORA_TEST_TIMEOUT_FACTOR: "3"
          TYCHORA_CODE_GOLD_REPORT: reports/code-gold.json
        run: >-
          uv run --locked pytest tests -q -n auto --randomly-seed=1
          -m 'not live and not qualification and not release'
          --cov --cov-branch --cov-report=json:reports/coverage-branch.json --cov-report=term
          --junitxml=reports/seed-1.xml
      - name: Seed 2
        if: always()
        env:
          TYCHORA_TEST_TIMEOUT_FACTOR: "3"
        run: uv run --locked pytest tests -q -n auto --randomly-seed=2 -m 'not live and not qualification and not release' --junitxml=reports/seed-2.xml
      - name: Seed 3
        if: always()
        env:
          TYCHORA_TEST_TIMEOUT_FACTOR: "3"
        run: uv run --locked pytest tests -q -n auto --randomly-seed=3 -m 'not live and not qualification and not release' --junitxml=reports/seed-3.xml
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        if: always()
        with:
          name: nightly-suite-reports
          path: reports/
          retention-days: 14

  mutation:
    name: Mutation testing of the core modules
    runs-on: ubuntu-22.04
    timeout-minutes: 300
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install the project to provision assets
        run: uv sync --locked --all-extras --group test-fixtures
      - name: Provision pinned public test assets
        run: python packaging/provision_ci_assets.py --cache "$RUNNER_TEMP/tychora-public-models" --workspace "$RUNNER_TEMP/tychora-asset-setup" --github-env "$GITHUB_ENV"
      - name: Reinstall without the project so mutants are imported
        run: uv sync --locked --no-install-project --all-extras --group dev --group mutation --group test-fixtures
      - name: Run mutmut
        run: uv run --no-sync mutmut run --max-children 4
      - name: Export results
        if: always()
        run: mkdir -p reports && uv run --no-sync mutmut results --all true > reports/mutmut-results.txt
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        if: always()
        with:
          name: nightly-mutation-reports
          path: reports/
          retention-days: 14

  report:
    name: Nightly gates, flakes and architecture report
    needs: [suite, mutation]
    if: always()
    runs-on: ubuntu-22.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
          fetch-depth: 0
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.9.28"
          enable-cache: true
      - name: Install locked checks
        run: uv sync --locked --all-extras --group dev
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: nightly-suite-reports
          path: reports
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: nightly-mutation-reports
          path: reports
      - name: Architecture report
        run: uv run --locked python -m tools.health report --architecture reports/architecture.md --previous docs/quality/2026-10-15-baseline.md
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: architecture-report
          path: reports/architecture.*
          retention-days: 90
      - name: Tests whose outcome depends on the seed
        if: always()
        run: uv run --locked python -m tools.health flaky reports/seed-1.xml reports/seed-2.xml reports/seed-3.xml
      - name: Record the nightly baseline (manual dispatch only)
        if: github.event_name == 'workflow_dispatch' && inputs.baseline
        run: uv run --locked python -m tools.health update --initial --group nightly
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        if: github.event_name == 'workflow_dispatch' && inputs.baseline
        with:
          name: nightly-baseline
          path: quality/baseline.json
          retention-days: 7
      - name: Nightly gates
        if: always()
        run: uv run --locked python -m tools.health check --group nightly
```

(`--previous` must point at the most recent committed report; each sub-project's final task updates it.)

- [ ] **Step 2: Validate and run it once by hand**

```bash
uv run --locked python -c "import yaml; yaml.safe_load(open('.github/workflows/health-nightly.yml')); print('ok')"
git add .github/workflows/health-nightly.yml && git commit -m "ci: nightly branch coverage, flaky detection, mutation testing, gold set and architecture report"
git push
gh workflow run health-nightly.yml --ref "$(git branch --show-current)" -f baseline=true
gh run watch <run-id>
gh run download <run-id> -n nightly-baseline -D quality
git diff --stat quality/baseline.json
```

Expected: `suite` and `mutation` succeed; `report` uploads `architecture-report` and `nightly-baseline`; `flaky` prints nothing; `git diff` shows new entries `coverage_branch`, `mutation_score` (per module; `tychora.contracts.identity` at 100.0) and `code_gold`. Note from the `suite` log the `TOTAL` line of the coverage terminal report (combined line and branch percentage).

- [ ] **Step 3: Set the nightly coverage floor and commit the baseline**

Add to `pyproject.toml`, using the combined `TOTAL` percentage from Step 2 rounded down to one decimal (spec §3.7 measured 81.65 locally as a lower bound):

```toml
[tool.coverage.report]
fail_under = 81.6
```

```bash
git add pyproject.toml quality/baseline.json
git commit -m "chore: record nightly coverage, mutation and gold-set baselines"
```

- [ ] **Step 4: Handle a flaky test if one appears**

If `flaky` lists a test: reproduce with `uv run --locked pytest <test id> -p randomly --randomly-seed=<seed>`; fix the shared state if it is in the test; otherwise add `@pytest.mark.quarantine(reason="<one line>", issue="<issue URL>")`, open the issue, and run `uv run --locked python -m tools.health update` only after a quarantine has been removed (G23 may only fall; adding one is a hand edit justified in the pull request).

---

### Task 21: Documentation accuracy audit

Spec §3.1 "Documentation accuracy", §4.8 "Documentation accuracy audit", §4.10 (worktree inventory).

**Files:**
- Modify: `docs/documentation.md` (audit checklist), `docs/architecture.md:5,231`, `docs/contributing.md:16`, `HANDOFF.md:11,59` (and the security paragraph), `tests/docs/test_examples.py`

**Interfaces:**
- Produces: `test_documented_commands_reference_real_entry_points[HANDOFF.md|docs/contributing.md]`.

- [ ] **Step 1: Write the failing command-block test**

Append to `tests/docs/test_examples.py` (add `import shlex` to its imports):

```python
DOCUMENTED_COMMANDS = ("HANDOFF.md", "docs/contributing.md")
TRACKED_ROOTS = ("packaging/", "tools/", "tests/", "docs/", "src/", "dashboard/", "examples/")
MARKERS = {"live", "qualification", "release", "assets", "slow", "quarantine"}
VALUE_FLAGS = {"--group", "--project", "--with", "--extra"}


def _documented_commands(document: str) -> list[list[str]]:
    text = (ROOT / document).read_text()
    commands = []
    for block in re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                commands.append(shlex.split(line))
    return commands


def _uv_run_target(words: list[str]) -> str | None:
    rest = iter(words[2:])
    for word in rest:
        if word in VALUE_FLAGS:
            next(rest, None)
        elif not word.startswith("-"):
            return word
    return None


@pytest.mark.parametrize("document", DOCUMENTED_COMMANDS)
def test_documented_commands_reference_real_entry_points(document: str) -> None:
    commands = _documented_commands(document)
    assert commands, f"{document} has no bash command block"
    for words in commands:
        for word in words:
            if word.startswith(TRACKED_ROOTS):
                assert (ROOT / word).exists(), f"{document}: {word} does not exist"
        if words[:2] == ["uv", "run"]:
            target = _uv_run_target(words)
            assert target, f"{document}: `{' '.join(words)}` names no command"
            if "--project" in words:
                project = ROOT / words[words.index("--project") + 1] / "pyproject.toml"
                installed = target in project.read_text()
            else:
                installed = (Path(sys.executable).parent / target).exists()
            assert installed, f"{document}: `{' '.join(words)}` runs a tool that is not installed"
        if "pytest" in words and "-m" in words:
            expression = words[words.index("-m") + 1]
            names = set(re.findall(r"[a-z_]+", expression)) - {"not", "and", "or"}
            assert names <= MARKERS, f"{document}: unknown markers {sorted(names - MARKERS)}"
```

Prove it bites: temporarily change `packaging/provision_ci_assets.py` to `packaging/provision.py` in `docs/contributing.md`, run `uv run --locked pytest tests/docs/test_examples.py -q -k documented_commands` (expected: FAIL naming `packaging/provision.py`), and revert.

- [ ] **Step 2: Add the audit checklist**

Append to `docs/documentation.md`:

```markdown
## Accuracy audit

Run this checklist once at the end of every sub-project, and whenever a page claims a number:

1. Every claim with a number (tests, sizes, versions, durations) is measured again or removed.
2. Every "not implemented", "missing" or "planned" statement is checked against the code.
3. Every file and line reference resolves.
4. Every command block runs. `tests/docs/test_examples.py` executes the README and example recipes and checks the command blocks of `HANDOFF.md` and `docs/contributing.md` statically (paths exist, tools are installed, markers are registered); run the blocks by hand as well.
5. A page that names the commit it describes is updated to the current commit, or the pin is removed.
```

- [ ] **Step 3: Correct the pages the audit found wrong**

- `docs/architecture.md:231`: replace "The dashboard has a specification but no implemented UI. Its intended views cover ingestion, corpus inspection, retrieval history and evaluation. Full traces and retention controls must be completed before presenting them as dashboard features." with "The built-in dashboard, served by `tychora serve`, covers collections, documents, PDF and OCR intake, search, evidence inspection, settings and evaluations; see the [dashboard guide](dashboard.md). Query traces and retention controls are not yet available in it."
- `docs/architecture.md:5`: replace "at `e7de140`" with "as of the current `main`" (and keep the link to the review assessment).
- `docs/contributing.md:16`: replace "Missing assets currently fail several tests; do not interpret those failures as retrieval regressions or silently suppress them." with "Tests that need provisioned assets declare them with `@pytest.mark.assets(...)` and are skipped with the variable names when those are unset; qualification and release tests fail instead."
- `HANDOFF.md:11`: replace "Current Task 6 evidence is recorded in the task report;" with "Current PDF intake evidence is recorded in its verification report;".
- `HANDOFF.md:59`: keep the dated observation and add after the list: "On 2026-10-15, 2,385 tests were collected; the default selection is now green on a clean machine (code-health baseline)."
- `HANDOFF.md`, "Security gate still blocked" paragraph: replace "No advisory has been ignored or waived." with "The four advisories are waived for at most 30 days in `quality/advisories.toml`, approved by the product owner and reviewed monthly; see the code-health design R1."

Re-run `git grep -nE "e7de140|no implemented UI|Task 6" -- docs ':!docs/superpowers' HANDOFF.md`. Expected: nothing.

- [ ] **Step 4: Record the local worktree inventory**

```bash
git worktree list
du -sh .worktrees .superpowers 2>/dev/null
git worktree prune --dry-run -v
```

Add a short "Local state" subsection to `HANDOFF.md` listing each worktree path with its branch and the sizes printed above, and the prunable entry reported by the dry run (`/private/tmp/tychora-engine-implementation` on 2026-10-15). Ask the product owner before running `git worktree prune`; HANDOFF says worktrees are not deleted routinely. Run it only after a "yes", then update the subsection.

- [ ] **Step 5: Verify and commit**

```bash
uv run --locked pytest tests/docs -q -n auto
uv run --project packaging/docs --locked mkdocs build --strict
git add docs HANDOFF.md tests/docs/test_examples.py
git commit -m "docs: audit accuracy, check documented commands and record local state"
```

---

### Task 22: Final verification: every gate green, baseline equal or better

Spec §6.3, §7. This is the template every later sub-project's last task follows.

**Files:**
- Modify: `quality/baseline.json` (only through `python -m tools.health update`)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run every gate locally with fresh reports**

```bash
rm -rf reports dashboard/coverage
COVERAGE_CORE=sysmon uv run --locked pytest tests -q -n auto -m 'not live and not qualification and not release and not slow and not quarantine' --cov --cov-config=quality/coverage-line.toml --cov-report=json:reports/coverage.json --junitxml=reports/junit.xml
env -i PATH="$PATH" HOME="$HOME" uv run --locked pytest tests -q -n auto -m 'not live and not qualification and not release' --junitxml=reports/clean-junit.xml
npm --prefix dashboard run coverage
uv run --locked pytest tools/health/tests -q
uv run --locked --group audit python -m tools.health check
```

Expected: every line `ok`. Coverage and runtime may differ from the CI-measured baseline on a laptop; a difference there is acceptable locally only if the CI `Code-health gates` job is green.

- [ ] **Step 2: Check the success criteria one by one**

| Criterion | Evidence to record in the final review |
| --- | --- |
| S1 | CI job `Code-health gates` green; `tools/health/tests/test_integration.py` passes |
| S2 | CI job `Clean machine and isolation`, step "Default suite without assets or variables": 0 failed, skip reasons name variables |
| S3 | same job, step "Each listed test file alone" green |
| S4 | `uv run --locked lint-imports`: 4 kept; `layering_exceptions` ≤ 90 |
| S5 | `plan_references` 0 |
| S6 | `quality/vulture_whitelist.py` lists `vector_record_id` and `new_family_nonce` with "callers added by engine core v2 §4.4"; `dead_code_dashboard` 0 |
| S7 | `coverage_line`, `coverage_branch`, `mutation_score` entries in `quality/baseline.json`; first nightly run linked |
| S8 | `tests/fixtures/evaluation/code-gold/baseline.json` committed; nightly `code_gold` entry |
| S9 | links to the six workflow runs (`check`, `qualification`, `dashboard`, `docs`, `build`, `binary`) on the delivery commit; `qualification` may stay red only on the recorded Linux Docling caption issue, the advisory register covers R1 |
| S10 | `docs/quality/2026-10-15-baseline.md` committed; G25–G28 in the `health` job; `docs/quality/README.md` template |

- [ ] **Step 3: Compare with the before/after table of spec §7**

```bash
uv run --locked python - <<'EOF'
import json
metrics = json.load(open("quality/baseline.json"))["metrics"]
for name in ("plan_references", "dead_code_whitelist", "integrity_raises_without_reason",
             "layering_exceptions", "cognitive_complexity", "modules_in_import_cycles",
             "mutation_score", "coverage_line", "coverage_branch"):
    print(name, metrics.get(name, {}).get("value"))
EOF
```

Expected: `integrity_raises_without_reason` 380, `layering_exceptions` ≤ 90 (61 + 2 direct edges listed once or more), `cognitive_complexity` ≤ 229, `modules_in_import_cycles` ≤ 76, `mutation_score` per module recorded with `tychora.contracts.identity` 100.0; `plan_references`, public API docstrings, knip findings, clean-machine failures and dashboard cycles are hard gates at 0.

- [ ] **Step 4: Record any improvement and commit**

```bash
uv run --locked --group audit python -m tools.health update
git diff --stat quality/baseline.json
git add quality/baseline.json
git commit -m "chore: record the code-health baseline at the end of sub-project 0"
```

(Skip the commit if `update` changed nothing.)
