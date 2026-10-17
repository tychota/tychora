# Workspaces and collections

A workspace is a local directory that owns one corpus and its runtime. Choose a different directory for each independent RAG workspace and open that same directory on later runs. The CLI uses the current directory when `--workspace` is omitted; it does not silently create a workspace under the home directory. See [configuration](configuration.md) and the [API](api.md).

For two independent corpora, these commands create separate workspaces without downloading models:

```bash
tychora --workspace ./knowledge/standards --offline init --json
tychora --workspace ./knowledge/product-code --offline init --json
```

Both initially use the document defaults. Select a configured code profile for repository ingestion; naming a directory `product-code` does not select its parser. Provision tokenizer/parser assets as described in [getting started](getting-started.md) before ingesting or searching.

You may choose `$HOME/knowledge/standards` instead. The physical path is the reference passed to the CLI, Python client or one-workspace server. There is no implicit cloud storage or cross-workspace search. A caller wishing to query several independent workspaces must query each and retain their separate provenance when combining results.

A workspace typically contains:

- `workspace.toml`: the selected profile and its configuration.
- `corpus.sqlite3`: canonical source, revision, extraction, evidence, relationship, policy, and indexing records.
- `artifacts/`: content-addressed source and derived payloads.
- `indexes/text/` and `indexes/image/`: configured local vector indexes; `indexes/graph/`: the configured graph index. These directories appear when their backends are enabled.
- `cache/syntax-v1/`: a discardable cache of parsed repository syntax facts.
- `models/`: verified tokenizer, parser, or model assets by default.
- `staging/` and private runtime coordination files.

`TYCHORA_MODEL_CACHE` can move model and parser assets to another directory. It does not move the corpus, artifacts, or native indexes. The CLI workspace path and the model-cache path are independent choices. For details on profile registration, see [configuration](configuration.md); for the current storage boundaries, see [architecture](architecture.md).

A profile describes extraction, validation, indexing, and retrieval policy for a workspace. Selecting another profile does not create another corpus or isolate access to the existing one. Some changes can be incompatible with already published index representations and require a compatible snapshot before activation.

Collections are named logical partitions inside a workspace database. The HTTP API supports create, list, edit, archive and reactivate operations; UUIDs remain stable when names or lifecycle change. The built-in `default` UUID and slug are reserved, but its title can be edited. Collections share the workspace database, artifacts and indexes. There is no global workspace-directory catalog or cross-workspace search.

Only an explicitly managed installation owner can create a collection. The loopback server binds one managed credential at composition; an otherwise identical static token cannot create collections or expand its grant. Managed future calls derive all accessible canonical IDs, including archived collections, and current provider upload authority. Explicit caller scopes and accepted-job scopes remain immutable. Reopening a managed workspace repeats this derivation.

Archiving blocks new ingestion, PDF preparation/receipt processing, provider work and publication in that collection, and removes its evidence from ordinary query and inventory eligibility. It retains identity, original data, authorized exact inspection and job history. Reactivation changes lifecycle without rewriting source/revision/unit IDs. Archive is distinct from withdrawal and purge.

New collections start active at access floor zero with no provider upload consent. The existing `provider-upload-consent` maintenance operation remains the consent editor; revocation takes effect against current canonical policy. Consent mutations preserve lifecycle and default kind. An optional registered `default_kind_id` guides new intake only; omitted edit values preserve it, explicit null restores inheritance. Inheritance offers `document@1` only where registered; legacy profiles have no title-only default. Metadata forms come from the selected kind's registered schema.
