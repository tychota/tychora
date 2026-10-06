# Configuration and external packs

A workspace stores its durable selection in `workspace.toml`. `init --config` accepts that workspace document, not a pack's `profile.toml`. A workspace document registers names under `[profiles]`, selects one in `[defaults]`, and can override strict `[configuration]` values. Relative pack paths resolve from the workspace document. On ordinary reopen the durable selected profile is used. Existing workspaces keep the same filename; there is no automatic rename or migration.

The [generic workbench pack](../examples/profiles/workbench/README.md) demonstrates an external data-only profile. It extends the version-qualified bundled `documents@1` pack, which provides regulation, standard, template, and feedback schemas and distinct roles/lanes. It registers `clarifies@1`, an explicit historical lane mapping, a project facet bound to source metadata, and hierarchical output. To use it from this checkout:

```bash
mkdir -p ./knowledge/profiles
cp -R ./examples/profiles/workbench ./knowledge/profiles/workbench
cat > ./knowledge/workspace.toml <<EOF_WORKSPACE
schema_version = 1
[profiles.workbench]
path = "profiles/workbench"
[defaults]
profile = "workbench"
[configuration.acquisition]
roots = ["$PWD"]
EOF_WORKSPACE
tychora --workspace ./knowledge --offline init --provider configured --config ./knowledge/workspace.toml --json
tychora --workspace ./knowledge --offline inspect config --json
tychora --workspace ./knowledge --offline inspect kinds --json
tychora --workspace ./knowledge --offline inspect facets --json
tychora --workspace ./knowledge --offline inspect relations --json
```

The acquisition root must be absolute. It permits source paths beneath it; it does not grant access to another caller or collection. A path outside configured roots fails. The CLI's `--profile` selects an already registered name for an operation. Kind metadata is validated against the discovered schema; for example, built-in `manual@1` requires title, publisher, and edition. Do not guess a kind from a filename. Pack dependencies are version/digest pinned; unresolved dependencies, duplicate definitions with different contents, and escaping schema includes fail validation.

For a file add after the root above is configured:

```bash
printf '%s\n' 'Record verification outcomes.' > ./manual.md
printf '%s\n' '{"title":"Verification","publisher":"Example Team","edition":"1"}' > ./manual.metadata.json
tychora --workspace ./knowledge --offline add ./manual.md --kind manual@1 --metadata-file ./manual.metadata.json --json
```

Source metadata `scope.project` projects into the registered project facet. A missing source project value matches a project filter only if that source explicitly declares `scope.global_facets=["project"]`, the facet allows global matching, and the request sets `include_global`. Children can narrow source values. Global matching never widens collection, revision, date, review, or access eligibility. Dates are strict ISO calendar dates and effective periods are half-open `[start, end)`; missing dates remain unknown. Distinct editions can coexist, while an update of the same edition replaces that edition's active member. The latest explicit publication is the default current edition; `all`, `exact`, `as_of`, and cutoff selections remain distinct.

Settings `GET /v1/settings` returns redacted effective values. `PUT /v1/settings` takes exactly `{ "profile": "name", "workspace_toml": "..." }` and requires management scope. Activation can return `WORKSPACE_BUSY` while work is active. It changes the effective policy epoch without rebuilding indexes; an incompatible pinned representation may return `PROFILE_INCOMPATIBLE` until a compatible snapshot is published. Credentials are `env:NAME` or API-resolved `secret:NAME` references, never secret values in the TOML. The current config archive is a versioned field projection containing reference names; a non-null query default cannot be exported.
