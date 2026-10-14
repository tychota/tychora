# Retrieve your first evidence

This tutorial creates a local workspace, adds a synthetic manual and searches it. It needs no provider credential and makes no embedding or LLM request. The output contains source text and citations for your application or agent to inspect.

## Install from a checkout

Tychora currently targets Python 3.12 with SQLite FTS5/JSON support. From a checkout:

```bash
uv sync --locked --no-default-groups
. .venv/bin/activate
tychora --help
```

This installs the base package. Documents, code, graph/vector indexes and server interfaces have separate extras; see [installation and distribution](distribution.md). These instructions do not assume a package or release has already been published to PyPI.

## Prepare the output tokenizer

Even lexical-only output uses a verified tokenizer to enforce response budgets. If you already have a bundle, point `TYCHORA_MODEL_BUNDLE` at its root. It must contain `tiktoken/0.14.0/cl100k_base.tiktoken`.

Otherwise, this explicit download obtains only the public tokenizer file; it sends no corpus documents. Run it once while online:

```bash
mkdir -p ./model-bundle/tiktoken/0.14.0
curl --fail --location \
  https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken \
  --output ./model-bundle/tiktoken/0.14.0/cl100k_base.tiktoken
export TYCHORA_MODEL_BUNDLE="$PWD/model-bundle"
```

The next step's `manage setup` verifies the packaged size and SHA-256, so changed bytes fail instead of being trusted. In an offline environment, transfer the bundle from a connected machine first.

## Create, add and search

Run the following from a writable directory. All commands below are offline and use a synthetic source.

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

The result should contain `Record verification outcomes.` together with evidence IDs, citations and coverage. Copy a returned unit ID and inspect it:

```bash
tychora --workspace ./knowledge --offline inspect evidence UNIT_ID --json
```

`UNIT_ID` is the actual ID returned by search, not the literal placeholder. Original text and derived descriptions have different provenance. Read [the architecture](architecture.md) for the result model.

## Next steps

- Add local files with explicit acquisition roots using [configuration](configuration.md).
- Keep independent corpora in [separate workspaces](workspaces.md).
- Inspect evidence and follow jobs in the [local dashboard](dashboard.md).
- Call the engine from the [Python API](api.md) or an [MCP agent](mcp.md).
- Review the current [implementation limits](review-assessment.md) before relying on strict retrieval, history retention or traces.
