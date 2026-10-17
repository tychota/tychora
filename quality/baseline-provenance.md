# Baseline provenance

`baseline.json` and `jscpd/*.json` were measured on the Ubuntu 22.04 reference runner by the `check.yml` workflow
dispatched with `baseline=true` (run 38996751848, commit 09f1a79), on 2026-10-17.

Values to remember when comparing with a laptop: `test_runtime_seconds` is 841.1 s on the runner (about 110 s on
Apple-silicon macOS) and `coverage_line` is 87.53. Branch coverage and mutation scores are nightly entries recorded by
the `health-nightly.yml` workflow dispatched with `baseline=true`; a `baseline=true` dispatch of `check.yml` rebuilds
this file from scratch, so record the nightly entries again afterwards.
