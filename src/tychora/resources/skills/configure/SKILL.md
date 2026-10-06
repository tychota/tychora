---
name: configure
description: Use when configuring a Tychora workspace, installing an external profile pack, or preparing offline assets and capabilities.
---

# Configure a workspace

Before initialization, inspect pack files, CLI help, and a workspace TOML statically. `inspect config --json` validates that TOML without opening the corpus, but `inspect kinds`, `inspect facets`, `inspect relations`, and `inspect settings` open the workspace and can create a missing one. For a read-only task, confirm the target workspace already exists and is authorized to open before those commands. If it is absent, prepare the wrapper and commands without executing owner-opening discovery. After an authorized `init`, discover the selected profile, registered kinds, filters, relations, and capabilities with those commands. A pack's `profile.toml` defines a pack; `init --config` accepts a **workspace TOML** that registers a pack root. The durable file is `WORKSPACE/workspace.toml`. Do not hand `profile.toml` to `init --config` or infer supported kinds, predicates, providers, or assets from a pack name.

For a data-only pack copied to `WORKSPACE/profiles/workbench`, a minimal `WORKSPACE/workspace.toml` is:

```toml
schema_version = 1
[profiles.workbench]
path = "profiles/workbench"
[defaults]
profile = "workbench"
```

Then run `tychora --workspace WORKSPACE --offline init --provider configured --config WORKSPACE/workspace.toml` and inspect the resolved configuration. A relative pack path is resolved from the workspace TOML; keep the pack there for restart. Treat any `init` response label as a summary and inspect the effective selected profile before relying on it.

For a clean offline cache, obtain a local `output-tokenizer` bundle through an authorized distribution path, verify it against the packaged manifest, and run `tychora --workspace WORKSPACE --offline manage setup --asset output-tokenizer --bundle BUNDLE --json`. Without a verified bundle, stop that setup step and report `CAPABILITY_MISSING` with the asset and supported setup command. Offline mode never downloads missing assets. Document, code, and visual paths require separate extras and assets. Provider configuration and credentials do not authorize uploads; inspect current capability and grant before any remote operation.

After configuration, validate the intended source or query against discovered kinds, schemas, filters, and scope. Inspect returned evidence, revision, citation, coverage, and original versus derived status. Do not raise scope, treat feedback as a rule, or invent a capability to satisfy the request.
