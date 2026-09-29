# Command line

Tychora has five daily commands: `init`, `add`, `search`, `inspect`, and `serve`.
Maintenance lives under `manage`. Put common options such as `--workspace`,
`--profile`, `--offline`, and `--format json|markdown|agent` before the command.
Use `--json` for canonical machine output. Status and errors go to stderr; JSON
results go to stdout.

```sh
tychora --workspace ./knowledge init --provider local
tychora --workspace ./knowledge inspect kinds --json
tychora --workspace ./knowledge inspect capabilities --json
```

`init --provider configured --config workspace.toml` uses an explicitly supplied
profile document. `init` never fetches model assets. The configuration must
register acquisition roots before `add PATH` can read local files. As of this
CLI release, runtime provider and index bindings still require explicit Python
composition; a configured provider setting alone does not activate one.

The kind registry defines required metadata. For example, `manual@1` requires
`title`, `publisher`, and `edition`. Supply actual values in a JSON file:

```json
{"title":"Synthetic handbook","publisher":"Example","edition":"1"}
```

```sh
tychora --workspace ./knowledge add ./handbook.txt --kind manual@1 --metadata-file handbook.metadata.json --json
```

For code or other source kinds, run `inspect kinds --json`, choose a registered
kind and its required metadata, then supply a complete `SourceInput` JSON with
`add --request-file code-source.json`. The same request file accepts text,
bytes, URL, folder, and repository variants. No metadata field is inferred from
the filename. Source jobs run the configured acquisition, extraction, lexical,
vector, and graph stages; their status is returned as a job record.

```sh
tychora --workspace ./knowledge search "Where is the exact term defined?" --json
tychora --workspace ./knowledge search --passage "synthetic passage" --json
tychora --workspace ./knowledge search --image IMAGE_ASSET_ID --json
tychora --workspace ./knowledge search --request-file exact-query.json --json
```

An exact lookup request file uses the public `QueryRequest` fields, for example:

```json
{"question":"Find the declared term","exact_identifiers":["rule:SYNTHETIC-42"],"exact_only":true}
```

The request file also carries filters, limits, budgets, and other advanced
options. `--format markdown` and `--format agent` render search evidence under
the recorded output budget. Search returns evidence rather than an answer.

```sh
tychora --workspace ./knowledge inspect sources --json
tychora --workspace ./knowledge inspect sources SOURCE_ID --json
tychora --workspace ./knowledge inspect evidence UNIT_ID --json
tychora --workspace ./knowledge inspect revisions REVISION_ID --original --json
tychora --workspace ./knowledge inspect job JOB_ID --json
```

`inspect config --json` resolves and redacts static settings without opening the
workspace database. Missing offline tokenizer assets produce a setup error before
query provider work. Explicitly select manifest assets and supply a verified
local bundle when staying offline:

```sh
tychora --workspace ./knowledge --offline manage setup --asset output-tokenizer --bundle ./model-bundle --json
```

`serve` hosts authenticated loopback HTTP and MCP through one workspace owner.
`serve --stdio` runs MCP on stdout with a private loopback control endpoint;
another CLI process delegates to that owner instead of opening a second one.
All diagnostics remain on stderr. Other maintenance verbs are discoverable with
`manage --help`; handlers still in development report `CAPABILITY_MISSING`.
`manage purge-originals` and its `--confirm` flag remain unavailable until the
owner-authorized preview and execution workflow is installed.

Provider configuration never grants upload consent. Collections deny uploads by
default; the public owner-authorized collection grant and revoke operation is
being added separately. Do not treat credentials or profile selection as a grant.
