# Code health baseline

Date: 2026-10-15. Status: design, awaiting review. Sub-project 0 of the Tychora v2 programme (`2026-10-15-tychora-v2-program-design.md`, "the programme"). Findings of the 2026-10-15 ingestion and query flow review are cited by identifier ("the flow review").

All numbers below were measured on `main` at `b91d808` on 2026-10-15 with read-only commands, using a throwaway environment synchronised from `uv.lock` (`uv sync --locked --all-extras`) and analysis tools installed outside the repository. Tool versions are those pinned in section 4.2.

## 1. Intent and success criteria

The product owner asked for a big clean-up: clean infrastructure, no duplicated code (measured, for example with jscpd), accurate and descriptive comments, and good tests. The programme turns that into one rule: a baseline of automated quality gates is installed before engine work, and every later sub-project leaves each metric equal or better (programme section 2, "Code health"; success criterion 5).

This sub-project delivers the gates, the measured baseline and the ratchet. It fixes only what is cheap and blocks the gates; the large refactorings belong to the sub-projects that rewrite the code anyway.

Success criteria, each checkable by a command in CI:

| # | Criterion | Proof |
| --- | --- | --- |
| S1 | One command, `uv run --locked python -m tools.health check`, runs every gate locally and in CI and exits non-zero if any metric is worse than `quality/baseline.json`. | CI job `health` green on the delivery commit; a deliberately injected regression (one new clone, one upward import, one 90-line function) makes it red in a test of the checker. |
| S2 | The default test suite is green on a clean machine: no model assets, no network, no environment variables. | `env -i PATH=$PATH HOME=$HOME uv run --locked pytest tests -m 'not live and not qualification and not release'` passes with asset-dependent tests reported as skipped, not failed. |
| S3 | Every test module passes when run alone. | CI step runs each file of a fixed sample set in isolation (section 6). |
| S4 | Layering contracts from the engine specification are encoded in import-linter and pass, with today's violations listed as explicit, shrinking exceptions. | `lint-imports` green; exception count in the baseline. |
| S5 | Zero plan-task references ("Task 7", "Task19", `task5.spec.ts`) in source, tests, dashboard code and fixture file names. | Text gate at zero, not ratcheted. |
| S6 | Known dead code triaged: `vector_record_id` and `new_family_nonce` stay, whitelisted with the reason "callers added by engine core v2 §4.4" (it uses both for family and vector record identity); dashboard knip findings at zero. | vulture and knip gates. |
| S7 | Coverage (line and branch) and mutation score on the core modules are recorded as baselines and enforced. | Coverage gate in `health`; nightly mutation job compares with the baseline. |
| S8 | A real code-retrieval gold set with a committed Tychora baseline exists before engine core changes retrieval (programme criterion 3 needs a "today" to compare with). | Evaluation test over the pinned corpus, lexical and exact channels, offline. |
| S9 | CI is green on `main` for reasons the owner has accepted, on Linux and macOS. | All six workflows green, or red only on a recorded, owner-approved advisory (section 8, R1). |
| S10 | Architecture quality is measured and reviewable: coupling, cycles, cohesion, cognitive complexity, depth, co-change and pass-through reports exist, four of them are gates, and the first report is committed. | `docs/quality/2026-10-15-baseline.md`; G25–G28 in the `health` job; the review template of 4.11.4. |

## 2. Scope and non-goals

In scope: quality tooling and its configuration, architecture-quality metrics, reports and a design rubric (SOLID and Ousterhout), the ratchet checker and baseline file, CI changes, test-suite hygiene (markers, fixtures, isolation), a ratchet gate on integrity errors without a reason (the reason taxonomy itself belongs to engine core v2), removal of confirmed dead code, removal of plan-task references, a documentation accuracy pass on the files a new maintainer reads first, and the code gold set.

Non-goals, with the owner of each:

- Moving the 237 raw SQL calls outside `adapters/` into a repository layer. Owner: engine core v2 (sub-project 1). It redesigns the snapshot manifest, eligibility and read connections (D1, D2, Q1, Q2, D4), which rewrites most of those statements; moving them first would be done twice. This sub-project adds the count to the ratchet so it cannot grow.
- Splitting the 300-plus-line functions (`_prepare`, `read_eligibility_facts`, `_compile`, `assemble`, `execute_accepted`). Owner: engine core v2, which rewrites them along the specification's stage boundaries. The ratchet forbids growth.
- Removing the 336 function-body imports. Most break cycles that the layering exceptions also list; they disappear as engine core fixes the cycles.
- Wiring the eligibility cache (Q1), purge commands (C5), the busy-owner error (C5). Owner: engine core v2.
- Adding ESLint to the dashboard. TypeScript `strict`, Prettier, knip and jscpd cover what this sub-project needs; the dashboard is rewritten in sub-project 6, which can choose its own lint rules.
- Parallel test execution (pytest-xdist). The suite spawns owner processes and parser children; parallelising it is a project of its own and is not needed to establish the baseline.

## 3. Current state

### 3.1 Infrastructure

| Area | Observation | Evidence |
| --- | --- | --- |
| CI workflows | Six workflows. `check.yml` (lint, format, ty, pytest, pip-audit) runs on Ubuntu only. The unit suite never runs on macOS in CI, although macOS is the main development platform. | `.github/workflows/check.yml:14` |
| CI status | `main` on GitHub (`700df65`) is red: "Locked checks" fails only on the pip-audit step; "Native qualification" fails on Ubuntu in `tests/qualification/test_docling.py::test_real_docling_retains_document_and_locators[pdf]`, the Linux Docling caption issue recorded in `HANDOFF.md`. Local `main` is 68 commits ahead of `origin/main`, so the dashboard workflow has not yet run on `main`. | `gh run list`, `git log origin/main..main` |
| Action pinning | Five workflows pin actions by SHA (`checkout` v4.2.2, `setup-uv` v9.0.0). `qualification.yml` uses floating tags `actions/checkout@v5` and `astral-sh/setup-uv@v7`, a different major version of each. `docs.yml` checks out without `persist-credentials: false`. | `qualification.yml:27-29`, `docs.yml:23` |
| Test runtime | The CI unit step took 30 min 54 s (2026-10-13, Ubuntu, assets provisioned). | Locked checks run on `700df65` |
| Lock and packaging | `uv.lock` is respected everywhere (`--locked`, `uv lock --check`). Dev tools are unpinned in `pyproject.toml` (`ruff`, `ty`, `pytest`) and pinned only through the lock. Hatchling builds an sdist from an explicit include list. | `pyproject.toml:51-55,61-79` |
| Ruff configuration | Only `target-version`; default rule set (E4, E7, E9, F). No complexity, docstring or import-placement rules. `ruff check` and `ruff format --check` pass (482 files). | `pyproject.toml:81-82` |
| Packages | Eleven package directories are implicit namespace packages (no `__init__.py`): `adapters` and four of its children (`docling`, `sqlite`, `vision`, `voyage`), `application`, `configuration`, `formatting`, `ingestion`, `interfaces`, `ports`. import-linter cannot analyse namespace packages, so no layering contract can run today. | `find src/tychora -type d` |
| Pre-commit | None. No local command runs the same checks as CI. | repository root |
| Node toolchain | No `.nvmrc` or `engines`; CI uses Node 22, the development machine runs Node 24. `prettier` is a dev dependency with no script; the code is already Prettier-clean. No coverage tool. | `dashboard/package.json` |
| Advisories | pip-audit over the exported lock: 5 rows, 4 unique advisories, all `chromadb 1.5.9`, no fixed version (PYSEC-2026-311, -3813, -3814, -3815). `npm audit`: 0. | `pip-audit -r`, `npm ci` |
| Stray artefacts | No stray tracked files. `.worktrees/` (9.8 GB, seven worktrees) and `.superpowers/` (945 MB) are ignored only through the maintainer's `.git/info/exclude`, not `.gitignore` (which ignores only `.superpowers/verification/`). `site/` is ignored. One worktree entry (`/private/tmp/tychora-engine-implementation`) is prunable: its directory is gone. | `git check-ignore -v`, `git worktree list` |
| Documentation accuracy | `docs/architecture.md:231` says "The dashboard has a specification but no implemented UI"; the dashboard is implemented. The page is pinned to `e7de140`. `HANDOFF.md` quotes 1,922 passing tests; 2,385 are collected today. `docs/contributing.md` says missing assets fail several tests, which is the defect S2 removes. `HANDOFF.md` refers to "Task 6 evidence". | files cited |

### 3.2 Duplication (jscpd 5.4.1, minimum 50 tokens)

| Scope | Files | Lines | Clones | Duplicated lines |
| --- | ---: | ---: | ---: | ---: |
| `src/tychora` (Python, exact) | 190 | 64,412 | 71 | 851 (1.32%) |
| `src/tychora` (Python, identifiers and literals ignored) | 190 | 64,412 | 444 | 5,202 (8.08%) |
| `tests` (Python, exact) | 200 | 68,807 | 254 | 3,258 (4.74%) |
| `dashboard/src` without tests (TS/TSX) | 51 | 9,729 | 3 | 27 (0.28%) |
| `dashboard/src` and `e2e` with tests | 62 | 16,479 | 73 | 673 (4.08%) |

Exact duplication in production code is low. The flow review's repeated snippets (the four-column `ArtifactRef(sha256=row[0], ...)` rebuild, the decision watermark query, now found in 6 places) are shorter than 50 tokens and show up in the renamed-clone (Type-2) figure instead. Largest production clones: `ingestion/documents.py:275` and `ingestion/normalize.py:162` (31 lines); `application/owner.py:523` repeated at 631 and 676 (28 lines, three copies); `adapters/voyage/query.py:24` and `:80` (25 lines); `adapters/code/python.py:632` and `adapters/code/typescript.py:907` (21 lines); `indexing/coordinator.py:442` and `:599` (21 lines); `ports/query.py:19` and `:45` (19 lines). Largest test clones: `tests/indexing/test_sources.py:690/769` (51 lines), `tests/providers/test_reranker.py:543/650` (45), `tests/api/test_configured_runtime.py:648/764` (41).

### 3.3 Layering

The engine specification fixes the direction ("Transport adapters depend on application services. Services depend on domain contracts and storage/provider interfaces. Concrete adapters implement those interfaces. Domain contracts do not import database clients, HTTP frameworks, CLI libraries, or model SDKs", `2026-09-10-tychora-engine-design.md:60`). `docs/architecture.md` lists the packages but not the direction. Measured direct import edges (module-level and function-body) that break the contracts of section 4.4: 17 upward layer edges, 29 adapter-to-upper-layer edges from 17 adapter modules (the flow review counted 14 modules against fewer target layers), and 15 edges from contracts, ports or pipeline packages into `adapters`. Examples: `contracts/extraction.py:217` imports `adapters.docling.ocr_evidence`; `ports/query.py:14` imports `application.owner`; `application/owner.py:56` imports `evaluation.runner`; `retrieval/rerank.py:9,40` imports `application.reranking` and `application.owner`; `indexing/coordinator.py:13-16` imports three SQLite adapters and `application.collection_policy`; `clients/sync.py:352` imports `interfaces.http.client`. There are 336 function-body import statements in 73 files (ruff PLC0415 agrees: 336); the flow review's 180 counted fewer forms.

### 3.4 Size and complexity

2,110 functions in `src/tychora`. Lengths in physical lines, nested functions counted inside their parent:

| Threshold | Functions |
| --- | ---: |
| > 50 lines | 304 |
| > 80 lines | 158 |
| > 180 lines | 32 |
| > 300 lines | 10 |

Largest: `interfaces/http/routes.py:117 register_routes` (638, a closure registering every route), `adapters/code/normalize.py:238 _build` (471), `ingestion/composition.py:424 build_composed_document` (427), `adapters/sqlite/validation.py:207 validate_bundle` (427), `adapters/sqlite/eligibility.py:216 read_eligibility_facts` (390), `adapters/docling/pdf_batches.py:159 run_pdf_batches` (383), `retrieval/response.py:117 assemble` (372), `application/query.py:681 _prepare` (354). The flow review's "25 functions over 180 lines, largest 453" was measured on `700df65` with a different counting rule; the numbers above are the baseline.

Ruff at default thresholds on `src/tychora`: C901 (complexity > 10) 164; PLR0912 (branches > 12) 93; PLR0915 (statements > 50) 70; PLR0913 (arguments > 5) 84; PLR0911 (returns > 6) 9.

### 3.5 Dead code

vulture 2.16 on `src/tychora` alone (so a function used only by tests counts as dead), confidence 60, decorators of Pydantic validators, FastAPI/Typer/MCP registrations, properties and context managers ignored: 51 unused functions, methods, classes and attributes, plus 210 "unused variables" that are almost all Pydantic fields and are not tracked. Confirmed from the flow review: `contracts/identity.py:111 vector_record_id` and `:122 new_family_nonce` are referenced only by `tests/contracts/test_identity.py`. Correction to the flow review: `retrieval/eligibility.py:8 eligible_cache_key` gained a caller on 2026-10-14 (`application/retained_content.py:114`, content-cursor binding in `c8ecfdd`); it is no longer dead, but Q1 still stands because `compile_attempt` never uses it as a cache key. Other notable entries: `adapters/sqlite/history.py:219 purge` and `retrieval/cache.py:117,125` (the dead purge path of C5), `application/recovery.py:390 prune_native` and `:507 rebuild_native`, `evaluation/metrics.py:19 recall` and `:41 span_coverage`.

knip 6.41.0 on the dashboard: 1 unused file (`src/pages/Sources.tsx`), 2 unused dev dependencies (`@testing-library/user-event`, `prettier`), 3 unused exports, 17 unused exported types (`src/api/contracts.ts`, `src/api/operations.ts`).

### 3.6 Comments and documentation

Plan-task references: 6 lines in 6 source files (`adapters/sqlite/publication.py:90` "Exact Task 7 typed retention header", `application/reranking.py:406` "Task19's canonical ownership", `retrieval/visuals.py:255` "Task 30's bounded retained-facts loader", `application/removal.py:509`, `contracts/composition.py:328`, `ingestion/document_parts.py:19`), 6 lines in 2 test files, 62 lines in 10 dashboard files, plus file names: `dashboard/e2e/task5.spec.ts`, `task6.spec.ts`, `task7.spec.ts`, `dashboard/src/test/task5.test.tsx` and 32 fixtures `dashboard/contracts/task2-*.json` to `task6-*.json`.

Docstrings (ruff pydocstyle rules): module docstrings are present everywhere except `__main__.py` (D100: 1). Missing on public classes 371 (D101), public methods 548 (D102), public functions 135 (D103). Coverage of public, non-nested definitions by package (interrogate 1.7.0, measurement only): `clients` 9%, `ports` 19%, `contracts` 20%, `interfaces` 22%, `adapters` 24%, `application` 27%, `api.py` 27%, `retrieval` 47%; whole package 25.4%. The public Python API (`api.py`, `clients/`) is the least documented code in the repository.

Style: the flow review's "contract-speak" observation (declarative fragments that say what is forbidden rather than what the function does) cannot be measured by a tool and is handled by the audit procedure of section 4.8.

### 3.7 Tests

2,385 tests collected; 2,285 in the default selection (`not live and not qualification and not release`). Asset-dependent tests read six environment variables (`TYCHORA_TEST_MODEL_CACHE` in 42 places, `TYCHORA_TEST_DOCLING_MODELS` 15, `TYCHORA_TEST_TOKENIZER_ASSETS` 10, `TYCHORA_TEST_OCR_MODELS` 6, `TYCHORA_TEST_CODE` 1, `TYCHORA_TEST_PDF_RESPONSES` 1); 18 sites index `os.environ[...]` directly and raise `KeyError`, and fixtures call `pytest.fail` instead of skipping. Only one test (`tests/providers/test_reranker.py:538`) skips with a message.

Clean-machine run of the default selection (no `TYCHORA_TEST_*` variable, network denied by `tests/conftest.py`, branch coverage on, 57 min 1 s locally): **2,172 passed, 46 failed, 57 errors, 10 skipped**. Of the 103 non-passes, 101 are missing assets (`KeyError: 'TYCHORA_TEST_MODEL_CACHE'`, "Set TYCHORA_TEST_DOCLING_MODELS ...", "Set TYCHORA_TEST_TOKENIZER_ASSETS ..."); `tests/test_import.py` needs the `tychora` console script on `PATH`, which only `uv run` provides; `tests/transports/test_dashboard_distribution.py` needs Hatchling from the `build` group, which `--group dev` does not install. The flow review's focused subset saw 18 of these.

Fixture coupling: `tests/ingestion/test_document_records.py:8` imports its fixtures from the qualification module `tests/qualification/test_docling_records.py`, whose fixture fails without the Docling bundle; that single import causes all 57 errors and 8 of the failures. `tests/ingestion/test_docling_mapping.py:129,137-138` does the same with `tests/qualification/test_docling.py` (it passes alone in a fully installed environment, 54 passed, but inherits the qualification module's imports).

Coverage of `src/tychora` under that run (a lower bound, since 103 tests did not pass): **line 85.25% (24,773 / 29,058), branch 71.42% (7,301 / 10,222), combined 81.65%**. By package (combined): `interfaces` 60.5%, `clients` 73.5%, `ingestion` 76.9%, `formatting` 80.6%, `application` 81.3%, `adapters` 82.3%, `retrieval` 83.5%, `indexing` 84.0%, `contracts` 89.9%, `configuration` 90.2%. Core modules (line / branch): `contracts/identity.py` 100 / 100, `contracts/publication.py` 92.5 / 50.0, `adapters/sqlite/publication.py` 80.8 / 70.7, `retrieval/eligibility.py` 91.2 / 77.7, `adapters/sqlite/eligibility.py` 92.3 / 84.4, `retrieval/lexical_query.py` 95.8 / 76.7, `adapters/sqlite/lexical.py` 94.3 / 79.2, `retrieval/fusion.py` 95.8 / 81.8. Dashboard unit coverage with Vitest and V8 (161 tests): statements 81.6%, branches 77.0%, functions 73.6%, lines 83.5%.

Slowest tests (same run): `tests/evaluation/test_evaluation_http.py::test_http_evaluation_list_and_detail_serialize_real_runs_and_deny_reader` 50.0 s, `tests/ingestion/test_ocr_lineage.py::test_real_sdk_emission_binds_exact_cells_and_fields[text]` 49.9 s, `tests/evaluation/test_provenance.py::test_selected_bindings_are_bounded_and_reused_without_snapshot_rehydration` 47.5 s; 20 tests exceed 18 s.

Mutation testing trial (mutmut 3.8.0, tests from `tests/contracts` and `tests/retrieval` minus the asset-dependent `tests/retrieval/test_query.py`, 557 mutants, 4 min 42 s on 6 workers). `contracts/identity.py` has 100% line and branch coverage yet a quarter of its mutants survive, which is why coverage alone is not the test-quality gate:

| Module | Mutants | Killed | Survived | Not covered | Score |
| --- | ---: | ---: | ---: | ---: | ---: |
| `contracts/identity.py` | 180 | 134 | 36 | 10 | 74.4% |
| `retrieval/fusion.py` | 149 | 115 | 34 | 0 | 77.2% |
| `retrieval/lexical_query.py` | 228 | 143 | 85 | 0 | 62.7% |

The trial also showed two preconditions: mutmut must import the mutated copy, which the editable install defeats (the job installs with `--no-install-project` and puts `src` on `pythonpath`); and a single asset-dependent failure aborts the whole run (S2 is a prerequisite).

Evaluation: the only code evaluation cases are synthetic (`docs/evaluation.md`, "neither set is a semantic-model quality ... or real-corpus benchmark").

### 3.8 Errors and raw SQL

The string `INTEGRITY_ERROR` appears 382 times in `src/tychora`: 380 `TychoraError("INTEGRITY_ERROR", ...)` constructions, its registry entry in `contracts/errors.py:13` and one comparison, up from the flow review's 335. `TychoraError(code, message, retryable)` has no sub-code (`contracts/errors.py:38-51`); `ErrorEnvelope` carries `code`, `message`, `retryable` (`contracts/errors.py:29-35`). The dashboard matches on the bare code (`dashboard/src/pages/sources/PdfIntakePanel.tsx:313`).

`.execute(` calls outside `adapters/`: `application` 210, `indexing` 17, `evaluation` 7, `interfaces` 2, `retrieval` 1; total 237 (the flow review counted 189 in `application`).

### 3.9 Architecture metrics

Measured with small AST scripts (the same code becomes part of `tools/health`), complexipy 8.0.1, radon 5.1.0 and madge 8.0.0. A "component" is a top-level package, with `adapters.*` and `interfaces.*` split by child.

**Coupling (Martin metrics, all imports including function-body ones).** Ca = components that import this one, Ce = components it imports, I = Ce/(Ca+Ce), A = share of `Protocol`/`ABC` classes, D = |A + I − 1|.

| Component | Modules | Ca | Ce | I | A | D |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `contracts` | 30 | 22 | 2 | 0.08 | 0.00 | 0.92 |
| `configuration` | 8 | 16 | 1 | 0.06 | 0.00 | 0.94 |
| `ports` | 15 | 19 | 4 | 0.17 | 0.54 | 0.29 |
| `application` | 31 | 15 | 18 | 0.55 | 0.02 | 0.43 |
| `retrieval` | 13 | 5 | 9 | 0.64 | 0.07 | 0.29 |
| `ingestion` | 13 | 3 | 7 | 0.70 | 0.00 | 0.30 |
| `indexing` | 6 | 5 | 5 | 0.50 | 0.00 | 0.50 |
| `adapters.sqlite` | 13 | 3 | 9 | 0.75 | 0.00 | 0.25 |
| `adapters.files` | 6 | 4 | 4 | 0.50 | 0.00 | 0.50 |
| `interfaces.http` | 9 | 3 | 8 | 0.73 | 0.00 | 0.27 |

`contracts` and `configuration` sit in the "zone of pain" (stable and concrete), which is expected for Pydantic data contracts and is not a defect by itself; what matters is that they stay free of upward imports. `application` has Ca 15: lower layers import it, which a composition root should never experience. `adapters.sqlite` has Ce 9, importing four layers above it.

**Cycles.** With module-level imports only, the module graph is acyclic. With all imports, 76 modules form 6 strongly connected components, the largest of 63 modules; at component level 18 of 25 components form one cycle (everything except `clients`, `interfaces.*` and `__main__`). The 336 function-body imports are therefore not style: they are the mechanism that hides an architecture-wide cycle from the interpreter. Dashboard: one cycle, `src/api/contracts.ts` ↔ `src/api/operations.ts` (madge); 83 production modules, 240 edges; highest fan-out `pages/Search.tsx` (13 imports, 895 lines).

**Cognitive complexity** (complexipy, SonarSource definition): 1,842 functions; 229 above 15, 129 above 25, 59 above 50; total 13,906. Highest: `adapters/sqlite/validation.py validate_bundle` 240, `retrieval/response.py ResponseAssembler.assemble` 179, `adapters/sqlite/eligibility.py read_eligibility_facts` 177, `configuration/registry.py _validate_definitions` 161, `configuration/resolve.py _resolve_config` 133. Radon cyclomatic ranks: 132 blocks at D or worse (D 65, E 31, F 36) of 2,382.

**Maintainability index** (radon): 35 of 204 files rank C (MI < 10), including `application/owner.py`, `application/discovery.py`, `adapters/sqlite/publication.py` and `adapters/sqlite/validation.py`; median 34.6.

**Class cohesion** (LCOM4: connected components of a class's methods linked by shared `self` attributes or calls; protocols excluded): 83 classes have three or more methods; 15 have LCOM4 > 1, two have 4 or more (`adapters/docling/ocr_lineage_capture.py:107 ReadingOrder` 8, `adapters/sqlite/corpus.py:46 CorpusStore` 5 over 18 methods and 593 lines). `application/owner.py:119 WorkspaceOwner` has 29 methods and 739 lines in two unrelated clusters. The code base is mostly functions, so class cohesion covers a minority of the code; module size and fan-out matter more.

**Module depth** (Ousterhout's deep versus shallow modules; proxy = non-comment lines divided by public symbols plus their parameters): median 12.4 lines per interface element over 189 modules. Shallow by design: the protocol modules in `ports/` (0.9–2.8). Deep but monolithic: `interfaces/http/routes.py` (674 lines behind one public function, `register_routes`, a 638-line closure), `indexing/representations.py` (920 lines, two public symbols).

**Change amplification** (co-change over the 143 commits that touch `src/tychora`): median 4 files and 2 packages per commit; 43% of commits touch three or more packages. Strongest cross-package pairs: `clients/async_.py` and `clients/sync.py` each change with `interfaces/http/routes.py` in 82% of the commits that touch either, `contracts/operations.py` with `interfaces/http/routes.py` in 67%, `api.py` with `application/owner.py` in 57%. Adding one public operation touches `contracts/operations.py`, an application service, `api.py`, `clients/sync.py`, `clients/async_.py`, `interfaces/http/routes.py`, `interfaces/http/client.py` and usually `interfaces/mcp/tools.py`: eight files for one concept.

**Pass-through layers.** Every method of `clients/sync.py` and `clients/async_.py` follows the pattern `ws.bridge.call(ws.services.<service>.<op>(_parsed(Request, request), ws.scope, _context()))` (for example `clients/sync.py:181-188`), and `interfaces/http/client.py` mirrors both (`traverse` at `:249` and `:641`). The three files total 1,851 lines of forwarding.

## 4. Design

### 4.1 Components

| Component | Responsibility | Location |
| --- | --- | --- |
| Tool configuration | Rule selection and thresholds for each tool | `pyproject.toml` (`[tool.ruff]`, `[tool.coverage]`, `[tool.importlinter]`, `[tool.vulture]`, `[tool.mutmut]`, `[tool.pytest.ini_options]`), `.jscpd.json`, `dashboard/knip.json`, `dashboard/vite.config.ts` (coverage block) |
| Baseline file | The committed metric values the ratchet compares with | `quality/baseline.json` |
| Allow-lists | Named, reviewed exceptions: today's layering edges, the dead-code whitelist, the advisory register | `[tool.importlinter]` `ignore_imports`, `quality/vulture_whitelist.py`, `quality/advisories.toml` |
| Health checker | Runs each tool, reduces output to metrics, compares with the baseline, prints a table, exits non-zero on regression or on a stale baseline | `tools/health/` (a package outside `src/`, excluded from wheel and sdist) |
| Architecture report | Coupling, cycles, cohesion, complexity, depth, co-change and pass-through report with deltas | `tools/health/architecture.py`; output in CI artifacts and `docs/quality/` |
| CI job `health` | Runs the checker on every push and pull request | `.github/workflows/check.yml` |
| Nightly job `health-nightly` | Mutation testing and flaky detection | `.github/workflows/health-nightly.yml` |

The checker is about 400 lines of Python using only the standard library plus the tools it invokes. Each metric is one small function `measure_<name>() -> Metric` where `Metric = {name, value, direction, per_file: dict[str, int] | None}`. A registry lists them; `check` runs them all, `update` rewrites the baseline, `report` prints current values without failing. No plugin system.

### 4.2 Tools and pinned versions

Python tools go into the `dev` dependency group with exact pins (so `pyproject.toml` states the version, not only the lock); mutation testing gets its own group so ordinary installs stay small.

| Tool | Version | Group | Purpose |
| --- | --- | --- | --- |
| ruff | 0.16.7 | dev | lint, format, complexity, docstrings, import placement |
| ty | 0.0.80 | dev | type check (unchanged) |
| pytest | 9.1.1 | dev | tests |
| pytest-asyncio | 1.4.0 | dev | unchanged |
| pytest-cov / coverage | 7.1.0 / 7.16.2 | dev | line and branch coverage |
| pytest-randomly | 5.0.0 | dev | random test order with a printed seed |
| import-linter | 2.15 | dev | layering contracts |
| vulture | 2.16 | dev | Python dead code |
| pip-audit | 2.10.1 | audit | unchanged |
| mutmut | 3.8.0 | mutation | mutation testing |
| jscpd | 5.4.1 | dashboard devDependency | duplication, Python and TypeScript |
| knip | 6.41.0 | dashboard devDependency | TypeScript dead code |
| @vitest/coverage-v8 | 4.1.11 | dashboard devDependency | dashboard coverage (matches vitest 4.1.11) |
| prettier | 3.8.5 | dashboard devDependency (existing) | formatting check |
| complexipy | 8.0.1 | dev | cognitive complexity |
| radon | 5.1.0 | dev | maintainability index (report only) |
| madge | 8.0.0 | dashboard devDependency | dashboard import cycles |

jscpd is installed once in `dashboard/` and invoked from the root (`npm --prefix dashboard exec jscpd -- ...`) so Node remains build-only and there is one lock for Node tools. Node is pinned to 22 by `dashboard/.nvmrc` and `"engines": {"node": ">=22"}`, matching CI.

Interrogate is not adopted: ruff's D1 rules give the same presence check without another tool.

### 4.3 Gate list

Hard gates fail on any finding. Ratchet gates fail when the value is worse than the baseline. Every gate runs in the `health` job on Ubuntu; the pytest gate also runs on macOS.

| # | Gate | Kind | Command (inside the checker) | Baseline today |
| --- | --- | --- | --- | --- |
| G1 | Lint, default rules | hard | `ruff check .` | 0 |
| G2 | Format | hard | `ruff format --check .` and `prettier --check src e2e` in `dashboard/` | 0 |
| G3 | Types | hard | `ty check src tests`; `tsc --noEmit` | 0 |
| G4 | Size rules | ratchet, per file and rule | `ruff check --select PLR0911,PLR0913,PLR0915 --output-format json src/tychora` (C901 164 and PLR0912 93 are reported, not gated; see 4.11.1) | 9 / 84 / 70 |
| G5 | Function length | ratchet, per function | AST walk: functions over 80 physical lines, keyed by `path::qualified_name` with their length | 158 functions; new functions over 80 lines forbidden; listed ones may not grow |
| G6 | Function-body imports | ratchet, per file | `ruff check --select PLC0415` | 336 |
| G7 | Layering | hard with listed exceptions | `lint-imports` | 61 exception edges (17 + 29 + 15) |
| G8 | Raw SQL outside adapters | ratchet, per file | count of `.execute(`, `.executemany(`, `.executescript(` calls via AST outside `tychora/adapters` | 237 |
| G9 | Duplication, Python | ratchet plus no new clone | `jscpd --config .jscpd.json --baseline quality/jscpd-baseline.json --fail-on-new-clones 0 --threshold <baseline%>` on `src/tychora` | 1.32%, 71 clones |
| G10 | Duplication, TypeScript | same | same on `dashboard/src` | 4.08%, 73 clones (0.28% without tests) |
| G11 | Duplication, tests | ratchet on percentage only | jscpd on `tests` | 4.74% |
| G12 | Dead code, Python | ratchet via whitelist | `vulture src/tychora quality/vulture_whitelist.py --min-confidence 60` filtered to function, method, class, property and attribute findings | 51 before this sub-project's removals |
| G13 | Dead code, TypeScript | hard | `knip` | 23 findings today, 0 after this sub-project |
| G14 | Docstring presence | hard for modules and the public API, ratchet elsewhere | `ruff check --select D100,D101,D102,D103,D104,D419`; hard on `src/tychora/api.py` and `src/tychora/clients/` | 1 / 371 / 548 / 135 / 0 |
| G15 | Plan references | hard | regex `\b[Tt]ask ?[0-9]+\b` over source, tests and dashboard text and over tracked file names | 74 lines, 36 file names; 0 after |
| G16 | Coverage | ratchet, total and per core module | `pytest --cov=tychora --cov-branch`, `coverage json`; Vitest `--coverage` | line 85.25%, branch 71.42%; dashboard statements 81.6%, branches 77.0% |
| G17 | Default suite on a clean machine | hard | pytest with all `TYCHORA_TEST_*` variables unset | 0 failures required |
| G18 | Test runtime | ratchet with tolerance | total wall time of G17 and the slowest 20 tests from `--durations`; fail when total exceeds the baseline by more than 15% | 30 min 54 s on CI Ubuntu without coverage; 57 min 1 s locally with branch coverage |
| G19 | Advisories | hard with register | `pip-audit --local` with every ignored ID listed in `quality/advisories.toml`; `npm audit --audit-level=low` | 4 chromadb advisories; npm 0 |
| G20 | Lock freshness | hard | `uv lock --check`; `npm ci` | passes |
| G21 | Mutation score | ratchet, nightly | `mutmut run` on core modules; score per module | trial values in 3.7; full core baseline recorded in week one |
| G22 | Code gold set | ratchet | evaluation over the pinned code corpus (section 4.9) | recorded in week one |
| G23 | Flaky tests | hard on quarantine growth | nightly: three runs with different `pytest-randomly` seeds; quarantine list may only shrink | 0 known |
| G24 | Integrity errors without a reason | ratchet, per file | AST search for `TychoraError("INTEGRITY_ERROR", ...)` without `reason=` | 380 |
| G25 | Cognitive complexity | ratchet, per function | `complexipy src/tychora --output-format json` | 229 functions over 15 |
| G26 | Module-level import cycles | hard | AST import graph, top-level imports only | 0 |
| G27 | Modules in import cycles, all imports | ratchet | AST import graph including function-body imports | 76 |
| G28 | Dashboard import cycles | hard | `madge --circular --extensions ts,tsx src` | 1 today, 0 after |

Thresholds for new code are ruff's defaults (50 statements, 5 arguments, 6 returns), cognitive complexity 15 (complexipy's and SonarSource's default) and 80 physical lines per function. They are the conventional defaults and the measured baseline already uses them, so no number needs a separate justification.

### 4.4 Layering contracts

`[tool.importlinter]` in `pyproject.toml`, `root_package = "tychora"`, `include_external_packages = true`. Prerequisite: add an `__init__.py` (holding only a one-line module docstring) to the eleven namespace packages so grimp can see them; this also makes package discovery explicit for Hatchling and PyInstaller.

1. **Layers** (higher may import lower; `|` siblings independent, `:` siblings may import each other):
   `tychora.interfaces | tychora.clients` > `tychora.api` > `tychora.evaluation` > `tychora.application` > `tychora.retrieval : tychora.indexing : tychora.ingestion : tychora.formatting` > `tychora.configuration` > `tychora.ports` > `tychora.contracts`.
   Evaluation sits above application because it drives the production query path; `application/owner.py:56` importing `evaluation.runner` is therefore an exception to remove (inject the runner from `services`).
2. **Adapters implement ports**: `tychora.adapters` is forbidden to import `application`, `retrieval`, `indexing`, `ingestion`, `evaluation`, `formatting`, `interfaces`, `clients`, `api`. Application and `api` remain the composition root and may import adapters.
3. **Storage through ports**: `contracts`, `ports`, `configuration`, `ingestion`, `indexing`, `retrieval`, `formatting` are forbidden to import `tychora.adapters`.
4. **Pure contracts**: `tychora.contracts` is forbidden to import `sqlite3`, `fastapi`, `typer`, `rich`, `chromadb`, `ladybug`, `voyageai`, `anthropic`, `docling`, `mcp`, `httpx` (engine specification line 60). Today it holds only indirectly, through contract 3's exceptions.

Each of today's 61 violating direct edges is listed in `ignore_imports` of the contract it breaks, one line each with the file and line in a trailing comment. The checker counts `ignore_imports` entries and treats the count as a ratchet metric: entries may be removed, never added. A trial run with these four contracts on a copy with the packages made explicit reported all four broken, as expected.

`docs/architecture.md` gains a "Dependency direction" subsection that states the four contracts in prose and names `pyproject.toml` as their source of truth.

### 4.5 The ratchet

`quality/baseline.json` holds, per metric: `value`, `direction` (`lower` or `higher` is better), optional `per_key` map (file, file and rule, or function), and `measured_at` (commit). Rules applied by `tools.health check`:

1. **No global regression.** A `lower` metric fails if `current > baseline`; a `higher` metric fails if `current < baseline`. Coverage and mutation scores compare with a tolerance of 0.1 percentage point to absorb branch-counting noise; test runtime uses 15%.
2. **No local regression.** For per-key metrics, every key present in both fails if its value got worse; a key absent from the baseline (a new file or function) must have value zero, except coverage, which is global. Renames are handled by `update`, which is allowed only when the global value does not get worse.
3. **Stale baseline fails.** If any metric is strictly better than the baseline, `check` fails with "run `python -m tools.health update` and commit `quality/baseline.json`". This locks every improvement in the same pull request that made it, which is what makes it a ratchet rather than a threshold.
4. **Updates are reviewable.** `update` rewrites only improved values and keys, sorts keys, and prints a diff summary. A worse value can only enter the baseline by hand-editing the file, which shows up in review; the pull-request template asks for a one-line justification for any hand edit.
5. **jscpd, import-linter and vulture keep their own allow-lists** (`quality/jscpd-baseline.json` fingerprints, `ignore_imports`, the vulture whitelist); the checker adds their sizes to `baseline.json` so the same three rules apply.

Each later sub-project's final verification includes `python -m tools.health report` before and after, and its plan's last task is "baseline equal or better, updated and committed".

### 4.6 Error reasons: gate only

Decision: the error reason taxonomy (registry, `ErrorEnvelope.reason`, schema version, which sites get which reason) is owned by engine core v2 (its section 4.13), because the programme assigns shared contracts to sub-project 1 and engine core rewrites most of the raising code (Q3 removes many read-time checks outright). Defining a second, smaller registry here would be replaced within weeks.

This sub-project owns only the gate. G24 counts `TychoraError("INTEGRITY_ERROR", ...)` constructions without a `reason=` keyword, per file, by AST (baseline 380; engine core's text counts 382 occurrences of the string, which includes non-raising uses). Before engine core lands, no call can pass a reason, so G24 simply cannot grow. Once engine core adds the `reason` parameter, ratchet rule 2 makes it mandatory in new files and forbids reason-less raises being added to existing ones, and every site engine core classifies lowers the baseline. The checker also reports the count by package so the final review of each sub-project shows where reason-less raises remain.

### 4.7 Test hygiene

- **Asset markers.** A new marker `assets(*names)` declares which `TYCHORA_TEST_*` variables a test needs. `tests/conftest.py` gains one hook, `pytest_runtest_setup`, that skips with "requires TYCHORA_TEST_X (provision with packaging/provision_ci_assets.py)" when any named variable is unset. A small helper `tests/helpers/assets.py: asset_path(name) -> Path` replaces the 18 direct `os.environ[...]` reads and is only callable from a marked test (it raises a clear error otherwise). CI keeps provisioning assets, so the same tests still run there; the `health` job additionally runs the suite with variables unset to prove S2.
- **Shared Docling fixtures.** The fixtures that `tests/ingestion/test_docling_mapping.py` and `tests/ingestion/test_document_records.py` borrow from `tests/qualification/test_docling.py` and `tests/qualification/test_docling_records.py` move to `tests/fixtures/docling.py`, registered through `tests/conftest.py` (`pytest_plugins`) and marked `assets("TYCHORA_TEST_DOCLING_MODELS")`, so the unit files skip their asset-dependent cases instead of erroring.
- **Environment assumptions.** `tests/test_import.py` resolves the console script from the active interpreter's `bin` directory instead of `PATH`; `tests/transports/test_dashboard_distribution.py` gets `@pytest.mark.release` (it builds the wheel, which is what the release marker means) and keeps running in `build.yml`.
- **Isolation check.** The `health` job runs a fixed list of ten test files singly (the two Docling files, and the eight largest files by test count), proving that fixtures are not inherited by accident. The list lives in `tools/health/isolation.txt`.
- **Random order.** pytest-randomly runs in every CI pytest invocation; the seed is printed and a failure is reproducible with `-p randomly --randomly-seed=<seed>`. The nightly job runs the default suite three times with different seeds; any test whose outcome differs is marked `@pytest.mark.quarantine(reason, issue)` and excluded from the blocking run but kept in the nightly run. The quarantine count is G23 and may only fall.
- **Runtime budget.** G18 as above; a test slower than 30 s locally must carry `@pytest.mark.slow` (still in the default selection). The checker prints the 20 slowest tests on every run so the cost is visible.
- **Coverage.** `[tool.coverage.run] branch = true, source = ["tychora"]`, `[tool.coverage.report] fail_under` set to the baseline. The core modules have their own floors in the baseline: `contracts/identity.py`, `contracts/publication.py`, `adapters/sqlite/publication.py`, `retrieval/eligibility.py`, `adapters/sqlite/eligibility.py`, `retrieval/lexical_query.py`, `adapters/sqlite/lexical.py`, `retrieval/fusion.py`. Dashboard coverage thresholds go into `vite.config.ts` from the measured values.
- **Suite speed.** The 57-minute figure is a serial run with the default branch tracer (about 3.7× overhead on Python 3.12, where `COVERAGE_CORE=sysmon` cannot measure branches). Measured on a 12-core development machine: serial without coverage 20 min 42 s; `pytest -n auto` without coverage 3 min 49 s; `-n auto` with `COVERAGE_CORE=sysmon` line coverage 3 min 42 s (85% line, matching the baseline); `-n auto` with branch coverage 10 min 20 s. Design: `pytest-xdist` and `pytest-cov` join the `dev` group; every pull request runs `-n auto` with sysmon line coverage (target under 5 minutes); the nightly job runs branch coverage, mutation testing and the three random seeds. Branch coverage moves to sysmon when the project adopts Python 3.14. The parallel run showed no shared ports or state; it exposed timing flakes, so the 35 fixed `timeout=10` waits in tests scale by `TYCHORA_TEST_TIMEOUT_FACTOR` (default 1, CI coverage runs 3).
- **Fixture and product hot spots.** Every `LocalSchemas` construction re-runs `Draft202012Validator.check_schema` over about 22 schemas (`configuration/schemas.py:146`, reached from `configuration/registry.py:88,136,676` and `configuration/compatibility.py:74,91`); it was 55% of a workspace-open-plus-four-adds run. Validated schemas are cached by schema digest (a product fix that also speeds production; measured −29% on the evaluation, application and HTTP tests). `tests/evaluation/conftest.py` `executable_integrity_corpus` and `tests/application/test_inspection.py` `published` become module-scoped for read-only tests. The 10,000-file acquisition test, the cache-state subprocess benchmark, the crash matrix and the large evaluation scenarios carry `@pytest.mark.slow` and run nightly only.
- **Mutation testing.** Nightly on the same eight core modules with mutmut 3.8.0. Configuration in `[tool.mutmut]`: `source_paths = ["src"]`, `do_not_mutate` listing every other module (generated by the checker), test selection `tests/contracts tests/retrieval tests/storage tests/indexing`. The job installs with `uv sync --locked --no-install-project --group dev --group mutation` and `[tool.pytest.ini_options] pythonpath` becomes `[".", "src"]` so mutants are imported, which the trial showed is required. Results are exported with `mutmut export-cicd-stats`; the score per module is G21. Survivors in `contracts/identity.py` are fixed in this sub-project (36 today) because identities are the one contract the programme forbids breaking; the other modules are rewritten by engine core and only ratchet.

### 4.8 Comments and documentation

- **Rule.** Code comments and docstrings state what the code does and why, in present tense, for a reader who has not seen the plan. They never cite plan tasks, plan file names or review identifiers. The rule is written into `docs/contributing.md` with two examples rewritten from the current code (the `publication.py:90` and `reranking.py:406` docstrings). G15 enforces the mechanical part.
- **This sub-project rewrites** the 12 Python lines, renames `dashboard/e2e/task5.spec.ts`, `task6.spec.ts`, `task7.spec.ts` and `dashboard/src/test/task5.test.tsx` after the journeys they test (`ocr-intake.spec.ts`, `pdf-intake.spec.ts`, `request-and-restore.spec.ts`, `ocr-intake.test.tsx`), and renames the 32 contract fixtures by subject (`collections-*.json`, `models-*.json`, `settings-*.json`, `metadata-*.json`, `ocr-*.json`, `pdf-*.json`), updating `dashboard/scripts/generate_contracts.py` and every reference in one commit.
- **Docstrings.** Every public function, class and method of `tychora.api` and `tychora.clients` gets a docstring (81 today), because `docs/api.md` documents that surface and IDEs show it. Elsewhere G14 ratchets.
- **Documentation accuracy audit.** A checklist in `docs/documentation.md`, run at the end of every sub-project and once now: (1) every claim with a number (tests, sizes, versions) is re-measured or removed; (2) every "not implemented" or "missing" statement is checked against the code; (3) every file and line reference resolves; (4) every command block runs (`tests/docs/test_examples.py` already executes the examples; extend it to the command blocks of `HANDOFF.md` and `docs/contributing.md`); (5) the commit a page claims to describe is updated or the pin removed. This run corrects `docs/architecture.md:231` and its `e7de140` pin, the HANDOFF test counts and "Task 6" wording, and the contributing guide's asset paragraph.

### 4.9 Code gold set

Decision: owned here, as a test-quality gap, not by engine core's benchmark harness. Programme criterion 3 compares retrieval quality with "today"; for code, today has no real measurement, and it must exist before engine core v2 and chunking v2 change windows and eligibility. The benchmark harness of engine core measures latency and throughput on a large corpus and consumes this gold set for its embedding and rerank variants later.

- **Source.** The owner's earlier code-search project has 48 golden queries (`eval/golden_queries.yaml`: query, expected files with `required` and `max_rank`, expected symbols, tags) and a committed baseline produced by that project's engine (`eval/baseline.yaml`: file and symbol recall at 5, 10, 20, first ranks, segment coverage). Its corpus is its own source tree at a pinned commit (about 10,000 lines of Python, MIT licence).
- **Conversion.** A one-off script, kept in `tools/health/convert_gold.py`, converts each query to a `tychora-gold@1` case: expected files become file labels (`required: true` ones are mandatory recall targets, the others enter `file_relevance` as judged-but-optional), expected symbols become definition labels, `max_rank` becomes a per-case threshold kept in the Tychora baseline, tags become `family_key`. All cases go to the `test` split except ten for `development`, chosen by stable hash of the case ID.
- **Corpus.** Fetched by `packaging/provision_ci_assets.py` as a pinned archive verified by SHA-256, like the model assets, into `TYCHORA_TEST_CODE_CORPUS`; the evaluation test carries `@pytest.mark.assets("TYCHORA_TEST_CODE_CORPUS")`. The public repository therefore contains queries and labels, never the corpus bytes.
- **Baseline.** Tychora's own result over the `code` profile, offline, with variants `lexical` and `lexical + structural_graph`, is committed as `tests/fixtures/evaluation/code-gold/baseline.json` and is G22. The earlier project's baseline is kept beside it for comparison only; it measures a different engine.

### 4.10 Infrastructure fixes

- Pin `qualification.yml` actions to the same SHAs as the other workflows; add `persist-credentials: false` to `docs.yml`.
- Add the `health` job to `check.yml`; run the pytest step on a matrix of `ubuntu-22.04` and `macos-15`.
- Add `.worktrees/` and `.superpowers/` to `.gitignore` (replacing `.superpowers/verification/`) so a fresh clone behaves like the maintainer's checkout.
- Add `dashboard/.nvmrc`, `engines`, `format:check`, `knip`, `coverage` scripts.
- No pre-commit framework: `python -m tools.health check --fast` (G1–G3, G7, G15 on changed files, under ten seconds) is the local entry point and is documented; one command for humans and CI avoids drift between hook and CI configurations.
- Inventory of local worktrees and `.superpowers/` is written to `HANDOFF.md`; the only removal is `git worktree prune` for the entry whose directory no longer exists, after owner confirmation, in line with HANDOFF's instruction not to delete worktrees routinely.

### 4.11 Architecture quality

The product owner asked for coupling, cohesion and design-principle tracking, with SOLID and Ousterhout's *A Philosophy of Software Design* as references. Principles are used as a review rubric backed by numbers, not as rules to satisfy mechanically; YAGNI still applies, and no abstraction is added only to raise a score.

#### 4.11.1 Metrics: gates or reports

A metric becomes a gate when it is cheap, stable across runners, hard to game and points at a specific line. Otherwise it is a report that a reviewer reads.

| Metric | Tool | Baseline | Kind | Rationale |
| --- | --- | --- | --- | --- |
| Layering contracts | import-linter 2.15 | 61 exceptions | gate G7 | encodes the specification's direction |
| Module-level import cycles | AST graph in `tools/health` | 0 | hard gate G26 | already true; must stay true |
| Modules in import cycles (all imports) | same | 76 (6 SCCs, largest 63) | ratchet G27 | measures the cycle the 336 body imports hide |
| Dashboard import cycles | madge 8.0.0 | 1 | hard gate G28, fixed here by moving the shared types of `api/contracts.ts` and `api/operations.ts` into `api/types.ts` | one cycle, cheap to remove |
| Cognitive complexity per function | complexipy 8.0.1 | 229 functions > 15 | ratchet G25 (new functions ≤ 15; listed ones may not grow) | closer to "how hard is this to read" than cyclomatic complexity |
| Function length, statements, arguments, returns | AST, ruff | section 3.4 | ratchet G4, G5 | unchanged |
| Martin metrics Ca, Ce, I, A, D per component | AST graph | table in 3.9 | report | D is misleading for Pydantic contracts; trends matter, not thresholds |
| LCOM4 per class | AST, about 40 lines | 15 classes > 1 | report | a minority of the code is classes |
| Maintainability index | radon 5.1.0 | 35 files rank C | report | composite of the other metrics; useful only as a trend |
| Module depth | AST | median 12.4 | report | proxy; needs judgement |
| Co-change across packages | `git log --name-only` | 43% of commits touch ≥ 3 packages | report | history-dependent, noisy |
| Pass-through functions | AST: body is one call forwarding exactly the parameters | 14, mostly Pydantic validators | report | catches only exact forwarding, so it misses the client layers |

With cognitive complexity as G25, ruff C901 and PLR0912 leave G4, which keeps PLR0911, PLR0913 and PLR0915: the two cyclomatic rules measure the same branches and would double-count every refactoring.

Rejected tools: wily (its trend store duplicates the per-commit history of `quality/baseline.json` and the nightly report archive, and it writes a cache into the working tree); lizard (overlaps ruff and complexipy); the `cohesion` package (a dependency for a 40-line computation that must exclude `Protocol` classes anyway); xenon (a threshold wrapper around radon, replaced by the ratchet); dependency-cruiser (madge answers the only dashboard question asked, cycles; sub-project 6 may adopt dependency-cruiser for its own layering rules).

#### 4.11.2 Ousterhout rubric

Each question has a measurable proxy and a current example. The reviewer answers every question in each sub-project's final review (4.11.4).

| Principle | Question for the reviewer | Proxy | Current example |
| --- | --- | --- | --- |
| Deep modules | Does each new module hide more than it exposes? | module depth; public symbols per module | deep: `adapters/files/cas.py` (staged writes, verification, pins behind put/open); shallow-and-wide: the three client layers |
| Information leakage | Does knowledge of one representation live in one module? | raw SQL outside adapters (G8); co-change pairs | SQLite schema known by 237 calls outside adapters; configuration dumped to JSON for lane lookup in `retrieval/segments.py:56-79` (D7) |
| Pass-through methods and variables | Does each layer add behaviour? | pass-through count; parameters forwarded unchanged | `clients/sync.py`, `clients/async_.py`, `interfaces/http/client.py` |
| Change amplification | How many files does one conceptual change touch? | co-change report | a new public operation touches eight files (3.9) |
| Cognitive load | Can a reader hold the function in their head? | G25, G5 | `validate_bundle` cognitive complexity 240 |
| Define errors out of existence | Could the error be made impossible instead of checked? | G24 (reason-less integrity raises); read-path `INTEGRITY_ERROR` count | 380 integrity raises, many re-proving at read time what publication proved (Q3) |
| Comments say what the code cannot | Does each comment add intent, units, invariants or reasons? | G14, G15 and the audit of 4.8 | "Task19's canonical ownership" (`application/reranking.py:406`) says nothing to a new reader |
| Pull complexity downwards | Does the callee absorb configuration instead of every caller? | constructor parameter count, PLR0913 | `QueryService.__init__` takes 16 parameters (`application/query.py:215`) and `build_services` (`application/services.py:338`) reconciles shared caches for it |

"Define errors out of existence" has a concrete rule for later sub-projects: validate at the write boundary (`commit_extraction`, `publish_snapshot`), trust canonical state on read paths, and move whole-store audits into `manage health`. Each removed read-path check lowers G24.

#### 4.11.3 SOLID applied to this code base

| Principle | Violation today (evidence) | Removed by |
| --- | --- | --- |
| Single responsibility | `application/query.py:681 _prepare` (354 lines, every query stage); `application/sources.py:587 execute_accepted` (254 lines) and `:1321 _registration` (280 lines of SQL and checks); `application/owner.py:119 WorkspaceOwner` (29 methods, two cohesion clusters); dashboard `pages/Search.tsx` (895 lines, 13 imports) | engine core v2 (query stages, ingestion pipeline, owner split into writer and read connections, D4); dashboard v2 (pages) |
| Open/closed | adding a code language means a hand-written adapter class (`adapters/code/python.py:27 PythonAdapter` 775 lines, `typescript.py:48` 900 lines); adding a public operation edits eight files | languages: chunking v2 (declarative language packs and one generic extractor, chunking v2 §4.8); operations: engine core v2, which adds trace and usage operations and should introduce a single operation registry that generates the sync client, async client, HTTP route and MCP tool bindings, only if it adds two or more operations |
| Liskov substitution | the `CorpusStore` port declares five methods (`ports/corpus.py:11-16`) while services call 18 methods of the concrete class (`adapters/sqlite/corpus.py:46`); no second implementation could be substituted | engine core v2 (repository layer, which also absorbs G8) |
| Interface segregation | `ports/jobs.py JobRepository` has 16 methods with no shared state; `TextQueryProvider` and `VisualQueryProvider` (`ports/query.py:18,44`) are seven-method near-copies (a jscpd clone); `QueryService` receives six optional providers | engine core v2 (`QueryBindings` value object, flow review 4.4) |
| Dependency inversion | 29 adapter-to-upper-layer edges (for example `adapters/vision/local.py:12` imports `application.owner`); `ports/query.py:11,14` imports `retrieval.cache` and `application.owner`; `indexing/coordinator.py:13-16` imports SQLite adapters; `contracts/extraction.py:193,217` and `contracts/documents.py:304` import Docling adapters | engine core v2 for storage, ports and indexing; Docling enrichment (sub-project 4) for the three Docling edges in `contracts`, which it rewrites anyway |

This sub-project removes none of them beyond the dashboard cycle; it makes each one a listed exception or a ratcheted number so the owners above can show the removal.

#### 4.11.4 Reports and their use

- `python -m tools.health report --architecture` writes a Markdown report: the coupling table, cycle list, top 20 cognitive-complexity functions, LCOM4 > 1 classes, maintainability C files, shallowest modules, top 20 co-change pairs and pass-through list, each with the delta against the previous report.
- The nightly job runs it and uploads it as a CI artifact (retained 90 days). It is not committed nightly, to avoid churn.
- At the end of each sub-project, the final task commits the report as `docs/quality/<date>-<sub-project>.md` (excluded from the MkDocs site navigation, like the plans), so the history of architecture quality lives in Git next to `quality/baseline.json`.
- The final review of each sub-project includes a section "Architecture quality" that: (1) links the before and after reports; (2) answers the eight rubric questions of 4.11.2 for the code it touched, citing numbers; (3) lists which SOLID rows of 4.11.3 it was expected to remove and shows the evidence (removed `ignore_imports` lines, lower G24, smaller co-change sets); (4) explains any report metric that got worse. A worse report metric does not block the merge; an unexplained one blocks the review.
- This sub-project commits the first report, `docs/quality/2026-10-15-baseline.md`, with the values of section 3.9.

### 4.12 Security and consent

No runtime behaviour changes. The code corpus is downloaded only by the provisioning script, never by tests (the network guard in `tests/conftest.py` stays). The advisory register makes any ignored advisory explicit, dated and owner-approved (R1).

## 5. Migration and compatibility

No stored format and no public contract changes. Renamed dashboard fixtures and tests are internal. Adding `__init__.py` files does not change import paths. No dual code paths are introduced.

## 6. Testing and verification

### 6.1 Baseline measurement run

Before the first gate is enabled, `python -m tools.health update` produces `quality/baseline.json` on the CI Ubuntu runner (coverage, runtime and mutation values are machine-sensitive, so the reference machine is CI, not a laptop). The values in section 3 are the local measurement that sizes the work; the committed file is the CI measurement.

### 6.2 Tests of the checker

`tools/health/tests/` holds unit tests that feed recorded tool output (small JSON fixtures) to each `measure_*` function and to the comparison rules: global regression, per-key regression, new key, stale baseline, tolerance, and a hand-edited worse value. One integration test builds a throwaway package with a 90-line function, an upward import and a duplicated block and checks that `check` fails with three named regressions.

### 6.3 Proof of each criterion

| Criterion | Verification |
| --- | --- |
| S1 | `health` job green; checker tests above |
| S2 | `health` job step with variables unset: 0 failures; skip reasons listed |
| S3 | isolation step |
| S4 | `lint-imports` green; G7 count 61 or lower |
| S5 | G15 at zero |
| S6 | vulture whitelist lists both identity functions with their engine-core reason; knip prints nothing |
| S7 | coverage gate active; first nightly mutation report committed |
| S8 | code gold evaluation test passes with assets; baseline file committed |
| S9 | six workflow runs on the delivery commit, linked in the final report |

## 7. Code-health ratchet

This sub-project defines the ratchet; its own obligations are that it leaves every metric equal or better and some strictly better:

| Metric | Before | After this sub-project |
| --- | ---: | ---: |
| Plan references (lines / file names) | 74 / 36 | 0 / 0 |
| knip findings | 23 | 0 |
| vulture tracked findings | 51 | 51 or fewer (the two identity functions whitelisted for engine core; each other entry triaged as remove, keep with reason, or owned by engine core) |
| Reason-less integrity raises | 380 | 380 (gate installed; engine core lowers it) |
| Public API docstrings missing (`api`, `clients`) | 81 | 0 |
| Surviving mutants in `contracts/identity.py` | 36 | 0 or each documented as equivalent |
| Clean-machine failures and errors | 103 | 0 |
| Layering exceptions | 61 (first measured) | 61 or fewer |
| Dashboard import cycles | 1 | 0 |
| Functions over cognitive complexity 15 | 229 | 229 or fewer |
| Modules in import cycles (all imports) | 76 | 76 or fewer |
| All other metrics | baseline | not worse |

## 8. Risks and open questions

- **R1. Advisory gate.** pip-audit fails on four `chromadb 1.5.9` advisories with no fix; HANDOFF records that none was waived. A permanently red gate hides new advisories. Default: `quality/advisories.toml` lists each ID with package, reason, reviewer and an expiry 30 days out; the gate fails on any unlisted advisory or expired entry, and the owner approves the first entries in review. If the owner prefers to keep the gate red, the `health` job still passes and the pip-audit step stays separate and red. Decided: the register is used (programme, resolved questions). Status on 2026-10-15: `chromadb 1.6.0` is merged upstream but not released on PyPI; its code fixes the two collection-configuration code-injection advisories and leaves the two tenant-authorization advisories open until upstream PR #7602 ships. None of the four is reachable from Tychora's embedded `PersistentClient` without the HTTP server. Upgrade when 1.6.0 is published (no API change for the calls in `adapters/chroma/index.py`; verify the exact `configuration` equality check in that adapter), then keep the remaining two in the register with that reachability reason.
- **R2. Publishing the code corpus.** The earlier code-search project has no public remote. Default: the owner publishes a pinned archive of it (it is MIT and the owner's own code) for the provisioning script to fetch. If the owner declines, the gold set is rewritten against Tychora's own `src/tychora` at a pinned commit with the same query intents, and S8 still holds.
- **R3. Coverage run time.** The local clean-machine run with branch coverage took 57 minutes; CI without coverage takes 31. Resolved by §4.7 "Suite speed": parallel execution with sysmon line coverage per pull request (measured 3 min 42 s), branch coverage nightly.
- **R4. Baseline noise.** Coverage and runtime vary between runners. Default: tolerances of 0.1 point and 15%; revisit after two weeks of nightly data.
- **R5. Scope of one and a half weeks.** Planned order: gates, checker and baseline (days 1–2); test hygiene and S2 (day 3); dead code, plan references and docstrings (day 4); the error-reason gate and the documentation audit (day 5); architecture metrics, report and review template (days 6–7); code gold set (day 8). If time runs short the gold set slips first (nothing depends on it until chunking v2), then the report's co-change and depth sections, which can follow as report-only additions without touching any gate.

## 9. Flow-review findings closed and deferred

Closed or partially closed here:

- Maintainability, section 3.4: "import-linter rule would fail today" (contracts encoded and enforced with listed exceptions); dead `vector_record_id` and `new_family_nonce` (whitelisted here; engine core v2 §4.4 gives them callers; `eligible_cache_key` is no longer dead, see 3.5); task-number docstrings (removed and gated); "`INTEGRITY_ERROR` ... no sub-code" (partially: the gate G24 is installed here; the taxonomy and site migration are engine core v2 section 4.13).
- Section 5, 18 asset failures in the focused run (fixed by asset markers).

SOLID and Ousterhout findings of 4.11.3 are assigned there to engine core v2, Docling enrichment and dashboard v2.

Deferred to engine core v2 with the ratchet preventing growth: raw SQL in the application layer and the `CorpusStore` port (3.4; engine core section 4.13 moves the 176 statements of the modules it rewrites, and G8 holds the rest); the repeated `ArtifactRef` rebuild, decision-watermark and policy-epoch reads (3.4); function-body imports that break cycles (3.4); the 300-plus-line functions (3.4, 4.4); Q1–Q6, D1–D8 and C1–C5, which are behaviour and design findings outside this sub-project.
