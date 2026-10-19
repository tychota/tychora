# Command line

Place `--workspace`, `--profile`, `--offline`, `--format json|markdown|agent`, or `--json` before the command. The daily verbs are `init`, `add`, `search`, `inspect`, and `serve`; lifecycle work is under `manage`. JSON results go to stdout and progress/errors to stderr. Use `--help` on a verb to see its current flags.

`init` creates a local workspace without fetching assets. Its `--config` argument is an existing **workspace TOML** that registers profile roots, not an external pack's `profile.toml`. It installs those bytes at `WORKSPACE/workspace.toml`; relative pack paths must resolve both at initialization and after restart. When a config is supplied, the JSON response labels the installed provider choice `configured`. `inspect config --json` validates the file without opening a workspace. Other `inspect` subjects open the owner and can initialize a missing empty path; confirm the target before using them in read-only preparation.

The [getting-started recipe](getting-started.md) gives a tested offline inline-source first run. To add a file, register an absolute acquisition root in `workspace.toml`, discover kinds and metadata schemas, then provide a valid kind and metadata file:

```bash
tychora --workspace ./knowledge --offline inspect kinds --json
tychora --workspace ./knowledge --offline add ./manual.md --kind manual@1 --metadata-file ./manual.metadata.json --json
tychora --workspace ./knowledge --offline inspect sources --json
tychora --workspace ./knowledge --offline search "verification" --json
```

`add --request-file source.json` accepts a complete `SourceInput` mapping, including inline text and selected vision requests. `search --request-file query.json` accepts a complete `QueryRequest` with filters, snapshot/edition choices, and budgets. `search --passage TEXT` and `search --image ASSET_ID` are separate input forms. Search returns structured evidence and coverage, not a generated answer. Use `--format markdown` or `--format agent` for a renderer output within the recorded budget.

`inspect kinds|facets|relations|profiles|settings|models|sources|runs --json` discovers the active registry and visible records. `inspect evidence UNIT_ID --json` reads exact authorized evidence; `inspect revisions REVISION_ID --original --json` requests a retained original if allowed. `inspect selections SOURCE_ID:REVISION_ID:EXTRACTION_ID --json` gives bounded selectors for an explicit image-region request. `inspect job JOB_ID --json` checks a durable job. Discovery and inspection reflect the current caller scope; unavailable or restricted records should not be inferred from missing IDs or counts.

`manage rebuild native CHANNEL...` re-projects the named channels (`lexical`, `text`, `image`, `graph`) from retained rows without provider calls. `tychora --workspace NEW manage rebuild workspace --from OLD [--kind-map FILE]` recreates an older workspace in the current storage format. `NEW` must be absent or empty; `OLD` is opened read-only and must not be open in another process. Every retained original is re-added as a source job; the JSON report lists the rebuilt sources and every skipped item with its reason (`withdrawn`, `repository_source`, `unsupported_kind`, `original_removed`, `original_missing`, `job_failed`, `timed_out`, `review_target_missing`). `--kind-map` maps retired kind IDs to current ones. Replayed review decisions keep their original decision time and actor; the old event identifier is not carried over because the review table has no column for it. A rebuild writes a `.rebuild-incomplete` marker into `NEW` first and removes it only after the old access policy is restored; a workspace that still carries it fails to open with `rebuild.incomplete`, so delete it and rerun the rebuild.

`manage gc [--limit N] [--published-only]` deletes snapshots outside the retention rule and the pages, batches and payloads only they used; `--limit` caps the snapshots deleted in one run (default 1024) and `--published-only` keeps batches that no snapshot ever published. It needs `manage` and a grant covering every collection. See [operations](operations.md#snapshot-retention-and-garbage-collection).

`manage setup --asset output-tokenizer --bundle BUNDLE` verifies a local offline asset. `manage health`, `export`, `import`, `sync`, `remove`, `purge-originals`, `provider-upload-consent`, and `evaluate` have their own typed inputs. See [operations](operations.md). `serve` owns loopback HTTP and MCP for one workspace; `serve --stdio` reserves stdout for MCP frames. A second local client delegates through the owner's protected endpoint record.

The CLI currently advertises `purge-cache` and `purge-history`, but maintenance dispatch returns `CAPABILITY_MISSING`; these commands do not enforce retention. `inspect relations` discovers predicate definitions, not an assertion by ID. `inspect collections` is not implemented. A second client encountering a standalone Python owner can currently receive `INTEGRITY_ERROR` rather than `WORKSPACE_BUSY`. See the dated [review assessment](review-assessment.md).
