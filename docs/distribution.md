# Installation and distribution

Tychora requires Python 3.12. `uv sync --locked --no-default-groups` installs the base lexical/local package; `uv sync --locked --all-extras` installs optional parser, native index, provider, and server dependencies from the lock. Individual extras are `documents`, `code`, `vector`, `graph`, `voyage`, `vision`, and `server`. An installed wheel provides Python code and bundled data-only profiles. It does not bundle tokenizer files, parser weights, provider credentials, or a standalone binary.

```bash
uv sync --locked --no-default-groups
uv run --locked tychora --help
# For a development environment with every optional dependency:
uv sync --locked --all-extras
```

Model assets use a packaged manifest with ID, path, size, SHA-256, license and source. `manage setup` verifies a local bundle or explicitly selected online source; ordinary `open`, `add`, and `search` do not download. Offline first use requires the `output-tokenizer` bundle documented in the [README](../README.md). Rich documents, local OCR, code grammars, vector and graph projections, and selected visual work have separate package and asset requirements; `inspect settings --json` exposes current capability status.

The existing native qualification covers an installed macOS arm64 environment and synthetic local fixtures; it does not qualify an installed wheel across every platform or a one-file executable. Linux/macOS release artifact, signing, model packaging, and live-provider correctness are separate release checks. SQLite must support FTS5 and JSON functions. Do not keep an externally supplied live Chroma client open across another process's writes to the same native path.
