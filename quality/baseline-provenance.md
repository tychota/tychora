# Baseline provenance

`baseline.json` and `jscpd/*.json` were measured locally on 2026-10-16 (macOS, Python 3.12, `COVERAGE_CORE=sysmon`),
pending a re-measure on the Ubuntu reference runner.

`test_runtime_seconds` (110.8 s, Apple-silicon macOS) and `coverage_line` (85.13) are local values. The re-measure
(`workflow_dispatch` with `baseline=true`, artifact `quality-baseline`) must be run and its artifact committed before
`feat/code-health` merges; until then the first CI `check` may fail the runtime ratchet. Replace this note afterwards.

Comparison with the design's initial figures: duplication_python 1.33 (1.32), duplication_tests 4.76 (4.74),
modules_in_import_cycles 47 (76; type-only `TYPE_CHECKING` imports are excluded by design), missing_docstrings 1031
(down from 1055), coverage_line 85.13.
