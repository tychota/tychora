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

Collections are logical partitions inside a workspace database. Source registrations and query scopes refer to collection IDs; authorization and retrieval check that an ID belongs to the opened workspace and is allowed by the caller's scope. Collections do not have separate database or artifact directories. The current public API does not provide a complete collection create/list/discovery workflow, and there is no global catalog that discovers workspace directories. The supported way to keep corpora independent today is to open separate workspace directories. Do not infer a collection creation command from the collection ID fields in request schemas.

The built-in local owner uses a fixed `default` collection. A service integration can carry collection IDs in its request scopes, but it must already have an authorized collection ID. The workspace boundary remains the physical directory plus its single owner process; collection scope narrows access within that boundary.
