# Dependency qualification

This report records package and SDK qualification for the Python 3.12 native
dependency candidates. The probes use synthetic local data, make no provider
calls, and do not download model or tokenizer assets.

## Hosted verification update — 2026-10-14

The [installed distribution qualification](https://github.com/tychota/tychora/actions/runs/38475452978) passed on the hosted macOS runner after correcting SQLite import verification for Python builds without extension-loading controls. The updated lock uses Docling 2.132.0, Docling Core 2.101.1 and Docling Parse 7.22.3. The tables below preserve the original qualification versions and observations; they are not a description of every current locked dependency.

Linux native qualification currently has one unresolved quality failure: Docling retains a synthetic figure caption as page text but omits its picture-caption association, while macOS supplies the association. All 43 other native checks passed in the [Linux diagnostic run](https://github.com/tychota/tychora/actions/runs/38478547001/job/115575184906). Controlled caption spacing and typography variants did not resolve the platform difference. The exact caption assertion remains enabled; no heuristic association or test waiver was added. This is an extraction-quality gap, distinct from whether the package or executable builds.

The resolved dependency audit remains blocked by four unique Chroma advisories. See [distribution and release limits](distribution.md). Passing functional qualification does not waive that gate.

## Tested environment

The local qualification run used macOS 26.5.2 arm64, CPython 3.12.13, uv
0.9.28, and SQLite 3.53.2. SQLite executed both an FTS5 query and a JSON
extraction query successfully. The locked qualification suite completed with
14 passing tests.

The installed native wheel metadata was:

| Distribution | Version | Installed wheel tag |
| --- | --- | --- |
| `chromadb` | 1.5.9 | `cp39-abi3-macosx_11_0_arm64` |
| `ladybug` | 0.20.3 | `cp312-cp312-macosx_15_0_arm64` |
| `tree-sitter` | 0.26.0 | `cp312-cp312-macosx_11_0_arm64` |
| `tree-sitter-python` | 0.25.0 | `cp310-abi3-macosx_11_0_arm64` |
| `tree-sitter-typescript` | 0.23.2 | `cp39-abi3-macosx_11_0_arm64` |
| `tree-sitter-javascript` | 0.25.0 | `cp310-abi3-macosx_11_0_arm64` |
| `docling-parse` | 7.19.1 | `cp312-cp312-macosx_14_0_arm64` |
| `onnxruntime` | 1.30.0 | `cp312-cp312-macosx_14_0_arm64` |

The Ladybug wheel sets the effective native macOS floor to macOS 15. The
Linux x86_64 candidates are Chroma's
`cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64` wheel and Ladybug's
`cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64` wheel. The Linux
CI target remains Ubuntu 22.04 with glibc 2.35, which is above both wheel
floors.

## Native storage results

| Gate | Result | Evidence |
| --- | --- | --- |
| Chroma explicit ID restriction | Passed | A two-vector collection queried with `ids=["allowed"]` returned only `allowed`. |
| Chroma persistence | Passed | A provider-free collection using supplied vectors was closed and reopened from a temporary directory. |
| Ladybug import and ownership | Passed | The installed module is `ladybug`; two `ladybug.Connection` instances read through one `ladybug.Database`. |
| Ladybug transactions and reopen | Passed | A subprocess committed `kept`, rolled back `rolled_back`, closed the database, reopened it, and returned exactly `kept`. |
| Ladybug forced termination recovery | Passed | A writer subprocess opened a transaction and inserted `interrupted`; after SIGKILL, reopening returned only the earlier committed row. |
| SQLite minimum and extensions | Passed | SQLite 3.53.2 executed FTS5 and JSON functions. |
| Locked minimal base install | Passed | `uv sync --locked --no-default-groups` imported `tychora` while Chroma, Ladybug, MCP, Voyage, Docling, and grammar modules were absent. |
| Linux x86_64 runtime | Not run locally | The workflow defines an Ubuntu 22.04 native job; no Linux runner result is claimed here. |

The graph probe's public adapter-facing names are `ladybug.Database` and
`ladybug.Connection`. Its query results expose `get_all()`. The probe keeps
connections under one owning database object and performs recovery checks in
separate processes.

## SDK and parser results

| Capability | Locked version | Result and actual API |
| --- | --- | --- |
| MCP server | `mcp==2.2.0` | Passed: `from mcp.server import MCPServer`; `MCPServer.streamable_http_app()` creates the app and lazily creates `server.session_manager`; `session_manager.run()` works as an async context manager. |
| Voyage contextual embedding | `voyageai==0.5.0` | Passed surface check: `voyageai.Client.contextualized_embed(..., enable_auto_chunking=False, ...)` exposes the required keyword. |
| Voyage tokenizer | `voyageai==0.5.0` | Passed surface check: `Client.tokenizer(model)`, `Client.tokenize(texts, model)`, and `Client.count_tokens(texts, model)` are explicit APIs. Executing `tokenizer(model)` was not run because it calls `tokenizers.Tokenizer.from_pretrained` and may fetch an asset. |
| Docling document JSON | `docling==2.126.0`, `docling-core==2.96.0` | Passed: `docling_core.types.doc.DoclingDocument.export_to_dict()` retained a synthetic document and embedded image URI. |
| Docling image access | `docling-core==2.96.0` | Passed: `PictureItem.get_image(document)` recovered exact dimensions and pixels from an embedded `ImageRef`. |
| Python grammar | `tree-sitter-python==0.25.0` | Passed through `tree_sitter_python.language()` and `tree_sitter.Language`. |
| JavaScript grammar | `tree-sitter-javascript==0.25.0` | Passed through `tree_sitter_javascript.language()`. |
| TypeScript grammar | `tree-sitter-typescript==0.23.2` | Passed through `language_typescript()`. |
| TSX grammar | `tree-sitter-typescript==0.23.2` | Passed through `language_tsx()`. |

No provider client made a request. Live Voyage embedding, image embedding,
reranking, and tokenizer-model correctness gates were not run because no
credentials or preprovisioned tokenizer assets were supplied. Real Docling
PDF/DOCX conversion with model assets was also not run; the qualification here
covers the installed structured JSON and image APIs without triggering a
weight download.

## Reproduction and remaining release gates

Install and run the current package-level qualification suite with:

```console
uv sync --locked --all-extras
uv run --locked pytest -m qualification tests/qualification -q
```

The workflow records platform data, Python, SQLite, installed package
versions, and native wheel tags before running the same suite on native macOS
15 arm64 and Ubuntu 22.04 x86_64 runners. Those CI jobs have been defined but
have not been run as part of this local qualification.

This historical dependency probe did not qualify a standalone executable. The
separate [distribution guide](distribution.md) and executable qualification report
record later local binary evidence and its target limits. A current-host
development run does not establish the macOS 15 or Ubuntu 22.04 baseline,
and live provider correctness, signing and notarization remain separate gates.
