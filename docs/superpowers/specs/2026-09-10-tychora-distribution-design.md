# Tychora — wheel and standalone distribution

Date: 2026-09-10
Status: release design; no wheel or standalone application has been built
Related: [Engineering](2026-09-10-tychora-engineering-design.md), [Configuration packs](2026-09-10-tychora-configuration-design.md), [Dashboard](2026-09-10-tychora-dashboard-design.md)

## 1. Release artifacts and their promises

Ship one Python distribution, `tychora`, containing the importable API, CLI, schemas, generic packs, migration resources and companion skill resources. Also target a standalone command-line executable for each qualified OS/architecture. Both use the same source and application contracts; the binary is not an alternate implementation.

| Artifact | Intended use | What it contains / requires |
| --- | --- | --- |
| `tychora-VERSION-py3-none-any.whl` | Python applications and isolated CLI installations | Tychora Python code/resources; compatible Python and installed dependency wheels required |
| `tychora-VERSION.tar.gz` | Rebuilding the Python package | Allowlisted source/build metadata and public tests/resources |
| `tychora-VERSION-OS-ARCH` | CLI, MCP and local HTTP without installing Python | Frozen Python runtime and selected Python/native dependencies; compatible OS libraries required |
| Checksums, dependency/license inventory and build provenance | Verify exact release composition | Bind version, commit, lock/build inputs, target and feature manifest |

A wheel is one package artifact, not a self-contained executable with every dependency vendored. Standard wheel installation resolves declared dependencies. The standalone executable bundles that runtime stack. Neither artifact includes user corpora, credentials, local packs, model weights or workspace databases. [Wheel format](https://packaging.python.org/en/latest/specifications/binary-distribution-format/).

## 2. Python package and extras

Use Hatchling with explicit include lists and `uv build`; install the built artifact in tests rather than importing from the checkout. The main wheel has no native extensions of its own, so a universal Python wheel is appropriate while native dependencies retain their own platform tags. If native code is later added to Tychora, change its wheel tags and build matrix instead of preserving a false universal tag. [uv packaging](https://docs.astral.sh/uv/guides/package/).

The base distribution contains validated contracts, configuration, SQLite/FTS, native Markdown/text ingestion, formatting and the CLI. Extras are fixed and documented:

| Extra | Capabilities |
| --- | --- |
| `documents` | Docling, supported local PDF/office extraction and image handling |
| `code` | Tree-sitter language wheels and Git scanning helpers |
| `vector` | Embedded Chroma adapter |
| `graph` | Embedded Ladybug adapter |
| `voyage` | Text/image embeddings and reranking provider |
| `vision` | Optional ingestion-only remote vision provider |
| `server` | HTTP and official MCP transports |
| `full` | Union of the supported extras above; no experimental adapters or external agent frameworks |

Recommended Python installation: `uv pip install 'tychora[full]'`; isolated CLI installation: `uv tool install 'tychora[full]'`. Select extras to reduce footprint, never to change evidence semantics. Optional imports are lazy; missing required capabilities return actionable diagnostics. An Agno adapter remains a separate optional integration and is not required by `full`.

Commit `uv.lock` for development/build/release resolution. Package metadata declares tested dependency ranges; release qualification uses the locked versions and publishes a constraints/feature manifest. Test both locked full installation and minimal base installation. Development groups hold Ruff, ty, pytest and packaging tools, never runtime dependencies. Build/install without access to private directories and inspect wheel/sdist contents against allowlists.

When the dashboard is implemented, build static assets in CI and include them as package resources in the same wheel/binary. No Node runtime is required on the user's machine. Until then, discovery reports `dashboard=false`; backend release does not advertise an implemented UI.

## 3. Standalone executable

Select **PyInstaller one-file mode** as the initial freezing approach, pinned with build tools during qualification. First qualify its one-directory output for dependency diagnosis, then produce and test one-file releases. PyInstaller bundles the interpreter and dependencies, produces platform-specific output and extracts support files at startup. It requires explicit treatment of imports/resources it cannot discover statically. [PyInstaller operation](https://pyinstaller.org/en/stable/operating-mode.html).

Maintain a reviewed `packaging/tychora.spec` and hooks. Build from the installed release wheel plus the locked `full` environment. Explicitly collect Chroma/Ladybug native libraries, Tree-sitter grammars, Docling/Torch resources, tokenizer resources, distribution metadata, schemas and generic packs. Do not collect entire developer environments or model caches. Runtime resources resolve through a package-resource abstraction; writable workspaces/caches use platformdirs or the configured path, never the executable extraction directory.

The executable exposes the same CLI entry point and can run MCP/HTTP. It does not require Python, uv, Node or a compiler at runtime. **Repository capture still requires a compatible system Git executable**: discover and validate it explicitly; ordinary document ingestion/search does not. Full offline repository use therefore requires Git provisioned separately. Do not claim the binary bundles Git. Arbitrary external Python plugins cannot be installed into a frozen executable; declarative local packs work unchanged, while custom executable adapters use the wheel installation or a rebuilt binary.

Use a frozen entry point with early `multiprocessing.freeze_support()`, before CLI parsing/heavy imports. Parser workers use the qualified standard multiprocessing path; database handles remain owner-local. Sanitize the library environment for system Git subprocesses independently of the application's native libraries. Test worker shutdown, cancellation and crash recovery from the frozen artifact. [PyInstaller multiprocessing and subprocess constraints](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html).

One-file startup writes temporary runtime files and incurs extraction overhead. Test the supported temporary filesystem and permissions, including executable mappings/symlinks. Report compressed size, peak extracted disk usage, cold start and warm service latency. A one-directory diagnostic archive is not counted as successful one-file delivery. If an adapter cannot be frozen or the artifact is unusable, report that failed gate and revise the distribution decision; never silently remove a promised capability.

## 4. Models, setup and offline operation

Code/runtime dependencies and model weights are separate. Explicit setup is a typed maintenance operation reachable through `tychora manage setup` and the typed maintenance API, not a new daily command. It lists required model assets, versions/checksums, sizes, licenses and destination, then acquires only the selected assets. It must not make arbitrary package changes. Support an explicitly supplied local model bundle for offline machines, validated against the same manifest.

Docling's selected extraction/OCR model set and tokenizers must be available before ingestion. `--help`, discovery, plain import and search never trigger downloads. No API keys are embedded. Voyage/vision still require authorized network access when configured; a standalone binary does not make remote models local. Offline requests use installed local capabilities and return an actionable unavailable-channel result when a required model is missing.

Package upgrades and workspace/schema/index migrations are separate. A new executable opens a supported workspace read-only until compatibility is checked; required migrations use the explicit maintenance/backup flow. Replacing a binary never deletes a corpus or silently rebuilds embeddings. Do not auto-update or self-modify the executable.

## 5. Qualification matrix and release gates

Initial candidate targets are Python 3.12 wheels plus standalone macOS arm64 (macOS 15+) and Linux x86_64 (glibc 2.35+, qualified on Ubuntu 22.04). The macOS minimum follows the selected Ladybug 0.20.3 arm64 wheel tag; older macOS would require a separately qualified dependency/source build. [Selected graph wheels](https://pypi.org/project/ladybug/0.20.3/#files). These are targets, not tested support claims. Qualify exact dependency wheel tags, native library minimums and freeze behavior on each; missing compatible wheels block that target. Windows, Linux arm64 and macOS x86_64 require their own build/install/recovery matrix before advertisement. No cross-platform universal executable is promised.

Build natively on each target family using controlled runner images and locked inputs. Test the lowest advertised OS/runtime baseline and a current supported runner. Record PyInstaller, Python, compiler/system-library and dependency versions. Dependency reproducibility does not imply byte-identical signed executables; distinguish rebuilt-input equivalence from artifact hash identity. macOS public binaries require signing/notarization; a missing release credential blocks that signed artifact, not local development builds. Publish hashes and third-party license notices for every artifact, including native/model components where redistributed.

Required gates on the **actual installed wheel and final one-file executable**:

1. Clean-machine startup and help with networking disabled; no hidden interpreter/developer-path dependence. Verify missing Git only affects repository operations.
2. Real Markdown/PDF/DOCX extraction, image provenance and supported code parsing with preprovisioned model assets; no development fixture masquerades as a real adapter test.
3. Chroma/Ladybug create, reopen, query, transaction failure and forced-termination recovery; SQLite feature checks and migrations/backups.
4. Real stdio MCP framing, HTTP lifespan/ownership and delegation; logs never corrupt protocol stdout.
5. Load an external generic configuration pack, register a custom kind/relation and compare canonical and hierarchical output between wheel and executable.
6. Provider contract checks, explicit offline failures, parser multiprocessing/cancellation, paths with spaces and read-only install locations.
7. Runtime/asset manifests, license inventory, artifact-content audit, import/dependency checks and measured size/startup/resource use.

Semantic retrieval quality and live-provider correctness retain their separate evaluation gates. This distribution specification does not assert that the large native/Docling stack already freezes successfully; proving that on the final artifact is required before a binary release claim.
