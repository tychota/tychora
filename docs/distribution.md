# Installation and distribution

Tychora requires Python 3.12. `uv sync --locked --no-default-groups` installs the base lexical/local package; `uv sync --locked --all-extras` installs optional parser, native index, provider, and server dependencies from the lock. Individual extras are `documents`, `code`, `vector`, `graph`, `voyage`, `vision`, and `server`. An installed wheel provides Python code and bundled data-only profiles. It does not bundle tokenizer files, parser weights, provider credentials, or a standalone binary.

```bash
uv sync --locked --no-default-groups
uv run --locked tychora --help
# For a development environment with every optional dependency:
uv sync --locked --all-extras
```

Model assets use a packaged manifest with ID, path, size, SHA-256, license and source. `manage setup` verifies a local bundle or explicitly selected online source; ordinary `open`, `add`, and `search` do not download. Offline first use requires the `output-tokenizer` bundle documented in the [README](../README.md). Rich documents, local OCR, code grammars, vector and graph projections, and selected visual work have separate package and asset requirements; `inspect settings --json` exposes current capability status.

The release suite builds a wheel and sdist from a temporary public-source staging tree. It inspects every archive member and its content before installation, then installs the base and full locked stacks into independent temporary environments. Subprocess checks run with a working directory outside the checkout and without `PYTHONPATH`. The base gate covers import, CLI help, offline lexical search and a default-collection identity collision. The full gate covers native Chroma/Ladybug, Tree-sitter grammars, MCP, HTTP, backup/restore and model-backed PDF/DOCX fixtures. The sdist is rebuilt into a wheel and its installed resources are checked again. Run it with a verified local model bundle:

```bash
uv lock --check
uv run --locked pytest tests/release -q
uv build --out-dir dist
python packaging/check_artifact.py dist/*.whl dist/*.tar.gz
```

The wheel has an explicit `src/tychora` include boundary. The sdist contains public source, tests, examples, docs, lockfile and build scripts; the archive auditor rejects private reports, corpus databases, credentials, model weights, traversal, links and machine-specific source paths. Builds use Hatchling 1.32.4. The lock records the tested Python 3.12 dependency versions and distribution hashes. Base installation can prohibit dependency source builds; the full stack has a specific exception because locked `antlr4-python3-runtime==4.9.3` is sdist-only. Its build remains isolated and hash checked. Build isolation controls dependencies, not arbitrary build-code execution.

`packaging/build_manifest.py` writes the package version, source commit, lock hash, toolchain, feature constraints, installed base/full dependency versions and license metadata, and SHA-256 for both archives and every member. The wheel includes the project's MIT license; third-party code is installed from separate distributions with their own notices, inventoried from the installed metadata. The manifest and checksum file are generated for local review; no CI workflow publishes to an index. Before any public release, inspect the outgoing Git history with `packaging/check_artifact.py --history-base BASE` to detect private files even if a later commit deleted them.

The last full-stack advisory audit in Task 43 remained nonclean: six rows covering five unique Chroma/OAuthlib advisories. The supported local profile had a bounded reachability assessment, not a blanket waiver; `check.yml` reruns the resolved dependency audit as a failing gate. PyPI account 2FA and trusted-publisher configuration remain unverified, and no publishing workflow is configured. The current host is macOS 26.5.2 arm64, so it does not establish a macOS 15 baseline or Linux qualification. Native platform, signing, model packaging, one-file executable and live-provider correctness remain separate release checks. SQLite must support FTS5 and JSON functions. Do not keep an externally supplied live Chroma client open across another process's writes to the same native path.
