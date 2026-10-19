# Baseline provenance

`baseline.json` and `jscpd/*.json` were measured on the Ubuntu 22.04 reference runner by the `check.yml` workflow
dispatched with `baseline=true` (run 39230447546, commit 7558693), on 2026-10-19, in the sharded layout: three test
shards and two clean-machine shards, merged into `reports/junit.xml`, `reports/clean-junit.xml` and
`reports/coverage.json` before the gates ran. The files are the run's `quality-baseline` artifact, copied unchanged
except for one removed entry, `test_runtime_seconds`.

`test_runtime_seconds` (G18) is a nightly entry: on commit 7558693 the dispatch measured 689.6 s and a pull-request run
measured 973.1 s, a 40% runner spread that a 15% per-pull-request ratchet cannot absorb. Pull requests print the value
without comparing it. The first nightly dispatched with `baseline=true` records it, with `update --initial --group
nightly --gates G16,G18,G21`, from the seed-2 run of the nightly suite (no coverage, the nightly selection); until then
the nightly gate reports the missing entry.

Values to remember when comparing with a laptop: `coverage_line` is 87.54 on the runner. Branch coverage, mutation
scores and the test runtime are nightly entries recorded by the `health-nightly.yml` workflow dispatched with
`baseline=true`; a `baseline=true` dispatch of `check.yml` rebuilds this file from scratch, so record the nightly
entries again afterwards.
