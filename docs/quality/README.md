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

## Baseline recording

`python -m tools.health update --initial` on an existing `quality/baseline.json` adds only the metrics that have no entry and never changes an existing entry; a plain `update` is the way to move a recorded value. The code gold set (G22) is excluded from the nightly checker until its report exists; see `tests/fixtures/evaluation/code-gold/README.md`.
