# Command line

Place `--workspace`, `--profile`, `--offline`, `--format json|markdown|agent`, or `--json` before the command. The daily verbs are `init`, `add`, `search`, `inspect`, and `serve`; lifecycle work is under `manage`. JSON results go to stdout and progress/errors to stderr. Use `--help` on a verb to see its current flags.

`init` creates a local workspace without fetching assets. Its `--config` argument is an existing **workspace TOML** that registers profile roots, not an external pack's `profile.toml`. It installs those bytes at `WORKSPACE/workspace.toml`; relative pack paths must resolve both at initialization and after restart. When a config is supplied, the JSON response labels the installed provider choice `configured`. `inspect config --json` validates the file without opening a workspace. Other `inspect` subjects open the owner and can initialize a missing empty path; confirm the target before using them in read-only preparation.

The [README recipe](../README.md) gives a tested offline inline-source first run. To add a file, register an absolute acquisition root in `workspace.toml`, discover kinds and metadata schemas, then provide a valid kind and metadata file:

```bash
tychora --workspace ./knowledge --offline inspect kinds --json
tychora --workspace ./knowledge --offline add ./manual.md --kind manual@1 --metadata-file ./manual.metadata.json --json
tychora --workspace ./knowledge --offline inspect sources --json
tychora --workspace ./knowledge --offline search "verification" --json
```

`add --request-file source.json` accepts a complete `SourceInput` mapping, including inline text and selected vision requests. `search --request-file query.json` accepts a complete `QueryRequest` with filters, snapshot/edition choices, and budgets. `search --passage TEXT` and `search --image ASSET_ID` are separate input forms. Search returns structured evidence and coverage, not a generated answer. Use `--format markdown` or `--format agent` for a renderer output within the recorded budget.

`inspect kinds|facets|relations|profiles|settings|models|sources|runs --json` discovers the active registry and visible records. `inspect evidence UNIT_ID --json` reads exact authorized evidence; `inspect revisions REVISION_ID --original --json` requests a retained original if allowed. `inspect selections SOURCE_ID:REVISION_ID:EXTRACTION_ID --json` gives bounded selectors for an explicit image-region request. `inspect job JOB_ID --json` checks a durable job. Discovery and inspection reflect the current caller scope; unavailable or restricted records should not be inferred from missing IDs or counts.

`manage setup --asset output-tokenizer --bundle BUNDLE` verifies a local offline asset. `manage health`, `export`, `import`, `sync`, `remove`, `purge-originals`, `purge-cache`, `purge-history`, `provider-upload-consent`, and `evaluate` have their own typed inputs. See [operations](operations.md). `serve` owns loopback HTTP and MCP for one workspace; `serve --stdio` reserves stdout for MCP frames. A second local client delegates through the owner's protected endpoint record.
