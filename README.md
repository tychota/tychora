# Tychora

<img src="docs/assets/logo.svg" alt="Tychora source and evidence mark" width="64" height="64">

![Documents and code connected to inspectable evidence](docs/assets/hero.png)

**Local evidence for applications and agents.**

Tychora is a local evidence workspace. It registers versioned sources, retrieves scoped evidence, and returns citations and coverage for inspection. Search does not write an answer for you. The Python API, CLI, loopback HTTP, and MCP transport expose the same workspace policy.

**Status: alpha.** The [current review assessment](docs/review-assessment.md) records known retrieval, retention and trace limitations. The dashboard is specified but not implemented.

Start with the [tutorial](docs/getting-started.md), [architecture walkthrough](docs/architecture.md), or [workspace guide](docs/workspaces.md). Build the searchable documentation site with the [documentation instructions](docs/documentation.md).

Continuing development? Read [HANDOFF.md](HANDOFF.md) for the current state, open fixes, architecture decisions, and dashboard planning priorities.

Python 3.12 and SQLite with FTS5 and JSON functions are required. Install the base package with `uv sync --locked --no-default-groups`. To enable every optional parser, native index, provider adapter, and server package in a development environment, use `uv sync --locked --all-extras`. An installed wheel is a Python package; it does not include model weights, tokenizer files, or a qualified standalone executable. See [distribution](docs/distribution.md).

An offline first run needs a separately obtained, checksum-verifiable `output-tokenizer` bundle. Set `TYCHORA_MODEL_BUNDLE` to a local directory containing `tiktoken/0.14.0/cl100k_base.tiktoken` with the bytes declared in the packaged asset manifest. `manage setup` verifies its size and SHA-256. A missing or changed file fails; offline mode never downloads it. The following commands use only synthetic text and a clean cache.

<!-- quickstart:start -->
```bash
: "${TYCHORA_MODEL_BUNDLE:?Set this to a verified local bundle directory}"
export TYCHORA_MODEL_CACHE="$PWD/model-cache"
tychora --workspace ./knowledge --offline init --json
tychora --workspace ./knowledge --offline manage setup --asset output-tokenizer --bundle "$TYCHORA_MODEL_BUNDLE" --json
cat > source.json <<'JSON'
{"type":"text","source_key":"verification-manual","collection_id":"default","kind":"manual@1","format":"text","metadata":{"title":"Verification","publisher":"Example Team","edition":"1"},"text":"Record verification outcomes."}
JSON
tychora --workspace ./knowledge --offline inspect kinds --json
tychora --workspace ./knowledge --offline add --request-file source.json --json
tychora --workspace ./knowledge --offline search "verification" --json
```
<!-- quickstart:end -->

The JSON search result contains source, revision, unit, citation, and coverage data. Use `tychora --workspace ./knowledge --offline inspect evidence UNIT_ID --json` with a returned unit ID to read exact authorized evidence. Run `inspect sources --json` for source lineage and `manage health` for workspace health. The workspace configuration file is `knowledge/workspace.toml`; reopening uses its durable selected profile. Adding a file path requires an absolute acquisition root in that configuration; see [configuration](docs/configuration.md). Document, code, and visual workflows need their respective extras and provisioned assets.

A Python example using the same synthetic sentence is [quickstart.py](examples/quickstart.py). [knowledge_adapter.py](examples/knowledge_adapter.py) shows a framework-neutral conversion that keeps provenance IDs inside metadata as well as at the top level. There is no installed Agno integration or generated-answer API.

- [Python API](docs/api.md) · [CLI](docs/cli.md) · [HTTP](docs/http.md) · [MCP](docs/mcp.md)
- [Configuration and packs](docs/configuration.md) · [Operations](docs/operations.md) · [Security](docs/security.md)
- [Evaluation](docs/evaluation.md) · [Performance](docs/performance.md) · [Distribution](docs/distribution.md)

- [Extension boundaries and effort](docs/extensions.md) · [Identity, chunking and providers](docs/design-choices.md) · [Ingestion improvements](docs/ingestion-roadmap.md)
- [Contributing](docs/contributing.md) · [MIT license](LICENSE)
