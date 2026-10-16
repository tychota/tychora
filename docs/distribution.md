# Installation and distribution

Tychora requires Python 3.12. `uv sync --locked --no-default-groups` installs the base lexical/local package; `uv sync --locked --all-extras` installs optional parser, native index, provider, and server dependencies from the lock. Individual extras are `documents`, `code`, `vector`, `graph`, `voyage`, `vision`, and `server`. An installed wheel provides Python code and bundled data-only profiles. It does not bundle tokenizer files, parser weights, provider credentials, or a standalone binary.

```bash
uv sync --locked --no-default-groups
uv run --locked tychora --help
# For a development environment with every optional dependency:
uv sync --locked --all-extras
```

Model assets use a packaged manifest with ID, path, size, SHA-256, license and source. `manage setup` verifies a local bundle or explicitly selected online source; ordinary `open`, `add`, and `search` do not download. Offline first use requires the `output-tokenizer` bundle documented in the [getting-started guide](getting-started.md). Rich documents, local OCR, code grammars, vector and graph projections, and selected visual work have separate package and asset requirements; `inspect settings --json` exposes current capability status.

The release suite builds a wheel and sdist from a temporary public-source staging tree. On macOS, its build subprocess runs under a `sandbox-exec` profile that denies reads from the original checkout, its Git common directory, and a separate synthetic private fixture. The subprocess proves those paths unreadable and the staged public README readable before invoking `uv build --offline` under the same profile. This is path-scoped read denial during the build, not whole-host or write isolation; staging happens before the boundary. It inspects every archive member and its content before installation, then installs the base and full locked stacks into independent temporary environments. Subprocess checks run with a working directory outside the checkout and without `PYTHONPATH`. The base gate covers import, CLI help, offline lexical search and a default-collection identity collision. The full gate covers native Chroma/Ladybug, Tree-sitter grammars, MCP, HTTP, backup/restore and model-backed PDF/DOCX fixtures. The sdist is rebuilt into a wheel and its installed resources are checked again. Run it on macOS with a verified local model bundle and a UV cache containing the pinned Hatchling build backend:

```bash
uv lock --check
uv run --locked pytest tests/release -q
RELEASE_TMP=$(mktemp -d)
python packaging/build_public_stage.py --repository . --source "$RELEASE_TMP/public-stage" --dist "$RELEASE_TMP/built" --proof "$RELEASE_TMP/proof.json" --private "$RELEASE_TMP/private-fixture" --uv "$(command -v uv)"
mkdir -p dist && cp "$RELEASE_TMP/built"/*.whl "$RELEASE_TMP/built"/*.tar.gz dist/
python packaging/check_artifact.py dist/*.whl dist/*.tar.gz
```

The wheel has an explicit `src/tychora` include boundary. The sdist contains public source, tests, examples, docs, lockfile and build scripts; the archive auditor rejects private reports, corpus databases, credentials, model weights, traversal, links and machine-specific source paths. Builds use Hatchling 1.32.4. The lock records the tested Python 3.12 dependency versions and distribution hashes. Base installation can prohibit dependency source builds; the full stack has a specific exception because locked `antlr4-python3-runtime==4.9.3` is sdist-only. Its build remains isolated and hash checked. Build isolation controls dependencies, not arbitrary build-code execution.

`packaging/build_manifest.py` writes the package version, source commit, lock hash, toolchain, feature constraints, installed base/full dependency versions and license metadata, and SHA-256 for both archives and every member. The wheel includes the project's MIT license; third-party code is installed from separate distributions with their own notices, inventoried from the installed metadata. The manifest and checksum file are generated for local review; no CI workflow publishes to an index. Before any public release, inspect the outgoing Git history with `packaging/check_artifact.py --history-base BASE` to detect private files even if a later commit deleted them.

The inventory verifies that its supplied commit is repository HEAD and that every non-generated sdist member equals the staged source input. It records a source-input tree hash and whether included bytes differ from that commit; unrelated generated `dist/` files do not mark source inputs dirty. Hosted executable jobs explicitly fetch the `documents`, `ocr`, `output-tokenizer`, and `voyage-context-4` selections through `manage setup`, then independently check their sizes and SHA-256 values against the packaged manifest before exposing test paths. The last selection supplies a verified local tokenizer for the synthetic, provider-free native recovery fixture. These downloads are a CI setup action, never an implicit runtime action. Hosted workflow definitions are present, but their executions are separate evidence.

The full-stack advisory audit on 2026-10-14 remains nonclean after upgrading OAuthlib to 4.0.0, Docling to 2.132.0 and multidict to 6.9.1. It reports five rows covering four unique Chroma 1.5.9 advisories, with no fixed versions reported: [CVE-2026-45829](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c), [CVE-2026-45833](https://github.com/advisories/GHSA-36p7-vc44-83pf), [CVE-2026-45831](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) and [CVE-2026-45830](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr). The duplicate row is CVE-2026-45829. The supported local profile had a bounded reachability assessment, not a blanket waiver; `check.yml` continues to run the resolved dependency audit as a failing gate. Passing package or executable checks does not close this security release blocker. PyPI account 2FA and trusted-publisher configuration remain unverified, and no publishing workflow is configured. The current host is macOS 26.5.2 arm64, so it does not establish a macOS 15 baseline or Linux qualification. Native target runs, signing, model packaging and live-provider correctness remain separate release checks. SQLite must support FTS5 and JSON functions. Do not keep an externally supplied live Chroma client open across another process's writes to the same native path.

## Standalone development executable

`packaging/tychora.spec` freezes the installed release wheel with the locked full runtime. `TYCHORA_BUILD_MODE=onedir` creates `dist/tychora-onedir/tychora` for dependency diagnosis; `TYCHORA_BUILD_MODE=onefile` (the default) creates `dist/tychora`. The build refuses an editable checkout import. Its entry point calls `multiprocessing.freeze_support()` before the CLI. Packaged profiles, schemas, migrations, skills, native extensions, converter resources and actual installed third-party notice texts are collected; model weights and user caches are external. In particular, `packaging/build_notices.py` copies installed license texts and adds the exact upstream Apache 2.0 license for `tokenizers==0.23.2`, whose installed wheel contains no notice path. The notice manifest records SHA-256 for each included text.

Build after obtaining an audited wheel and its release inventory. Set `UV_NO_SYNC=1` for `uv run` after installing the wheel so uv does not replace it with an editable project. The feature file is generated from that inventory, not hand-edited:

```bash
uv sync --locked --all-extras --group build --no-default-groups --no-install-project
uv pip install --python .venv/bin/python --no-deps dist/tychora-*.whl
UV_NO_SYNC=1 uv run --locked python packaging/build_release_features.py --manifest dist/release-manifest.json --output packaging/release-features.json
UV_NO_SYNC=1 uv run --locked python packaging/build_notices.py --output dist/tychora-notices
TYCHORA_BUILD_MODE=onedir UV_NO_SYNC=1 uv run --locked pyinstaller packaging/tychora.spec --noconfirm --clean
TYCHORA_BUILD_MODE=onefile UV_NO_SYNC=1 uv run --locked pyinstaller packaging/tychora.spec --noconfirm --clean
UV_NO_SYNC=1 uv run --locked python packaging/test_frozen.py --artifact dist/tychora --features packaging/release-features.json
UV_NO_SYNC=1 uv run --locked python packaging/audit_frozen.py --artifact dist/tychora --notices dist/tychora-notices
```

The test uses only the executable's public CLI, HTTP and MCP interfaces with synthetic inputs. It requires `TYCHORA_TEST_MODEL_CACHE` to point to a preverified public model bundle; runtime parser/tokenizer assets remain external, with no implicit download. System Git is needed for repository capture. Direct `add` waits for a terminal job for at most 180 seconds by default; `--wait-seconds` accepts a positive finite caller wait up to 3600 seconds and does not change parser execution limits. Native macOS 15 arm64 and Ubuntu 22.04 x86_64 builds are independently defined in `binary.yml`; a macOS 26 development result does not qualify either baseline. The workflow records unsigned local artifacts and hashes but does not publish or upload them. Public macOS signing and notarization require configured release credentials and a separate authorized release action.
