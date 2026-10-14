# Configuration and external packs

New workspaces use `general-documents@1`, an additive extension of the unchanged
`documents@1` pack. Its `document@1` kind accepts a title alone and preserves
optional metadata validation. It uses the existing structured-section normalizer
and evidence lane; existing specialized kinds retain their required metadata.

Existing workspaces keep their registered profile. To upgrade explicitly, change
that profile's `builtin = "documents@1"` to `builtin = "general-documents@1"`
through settings activation, preserving the profile name and other settings.
Activation proves unchanged old kind/schema/reference meanings, policies,
parser, embedding and index settings before permitting the added kind. Retained
extractions, citations and configuration artifacts keep their original identities;
re-adding an unchanged original uses its verified retained configuration.
Representation changes still require the existing rebuild/migration workflow.

Authorized readers can fetch
`GET /v1/discovery/kinds/{kind_id}/metadata-schema` for a registered kind's typed
required/optional fields, exact schema digests and local reference closure.
Responses are bounded to 64 schema nodes and 256 KiB. Fields marked `advanced`
need a metadata editor that retains the supplied schema constraints; they must
not be silently omitted. The endpoint does not accept arbitrary schema paths or
fetch remote references.


A workspace stores its durable selection in `workspace.toml`. `init --config` accepts that workspace document, not a pack's `profile.toml`. A workspace document registers names under `[profiles]`, selects one in `[defaults]`, and can override strict `[configuration]` values. Relative pack paths resolve from the workspace document. On ordinary reopen the durable selected profile is used. Existing workspaces keep the same filename; there is no automatic rename or migration.

The generic workbench pack in `examples/profiles/workbench/` demonstrates an external data-only profile. It extends the version-qualified bundled `documents@1` pack, which provides regulation, standard, template, and feedback schemas and distinct roles/lanes. It registers `clarifies@1`, an explicit historical lane mapping, a project facet bound to source metadata, and hierarchical output. To use it from this checkout, provide the same checksum-verifiable local `output-tokenizer` bundle described in the [getting-started guide](getting-started.md). Set `TYCHORA_MODEL_BUNDLE` to its directory; a clean offline workspace cannot add/search before this asset is provisioned:

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
: "${TYCHORA_MODEL_BUNDLE:?Set this to a verified local bundle directory}"
export TYCHORA_MODEL_CACHE="$PWD/model-cache"
tychora --workspace ./knowledge --offline manage setup --asset output-tokenizer --bundle "$TYCHORA_MODEL_BUNDLE" --json
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

## Guided workspace edits

Full workspace managers can inspect `GET /v1/settings/fields`, preview a
`SettingsPatch` at `POST /v1/settings/preview`, and resubmit it with
`PATCH /v1/settings`. The server supplies `base_workspace_sha256` for the exact
current `workspace.toml` bytes. A patch contains 1–32 unique leaf changes with
`action: "set"` and a typed non-null `value`, or `action: "inherit"` without a
value. Inherit removes the workspace override and restores profile resolution.
Comments, registered profiles, defaults and unrelated configuration survive AST
editing. Neither profile switching nor arbitrary nested JSON is supported here.

The fields response supplies actual value schemas, origins, override values and
qualified choices. Guided settings cover provider/model/reference configuration,
qualified embedding limits, parser selection, existing retrieval options,
bounded graph/query inspection limits, the strict `offline` boolean and the
`index.text.backend` / `index.image.backend` choices (`disabled` or `chroma`).
Reranking uses `providers.rerank`; there is no `retrieval.rerank` setting. Fresh
workspaces can preview/apply online Voyage text/rerank settings and a Chroma text
index together before ingestion. Setting `offline=false` permits
network operation but does not grant collection upload consent. Preview and
activation do not resolve credential references, contact providers, or download
models; explicit setup and collection consent remain separate actions. Existing
indexed representations still reject incompatible backend/embedding changes.
PDF OCR remains an explicit intake choice, not a global configuration switch.

Credential references use the existing `env:NAME` or `secret:NAME` syntax; names
must match `[A-Za-z_][A-Za-z0-9_.-]*`. Set the named environment variable in the
server process or supply the embedded secret resolver. The manager-only fields
response exposes reference strings, never secret values. Ordinary settings and
preview changes redact references. Credential readiness is `not_checked`:
inspection never resolves secrets or probes a remote provider.

Preview does not activate, provision or rebuild anything. It reports candidate
and base digests, redacted changes and compatibility blockers. Populated index
generations retain their representation identity and snapshot configuration.
Query defaults, limits and network policy edits can also be incompatible with
that retained configuration. Preview reports `PROFILE_INCOMPATIBLE`, with no
rebuild queued; guided and expert activation reject the change without altering
the current settings or index. Such changes require a fresh workspace or a
separately qualified publication/migration path. Configure a fresh remote-provider
workspace before indexing; ordinary per-query controls still respect its current
limits. Credential/reranker changes, monotonic policy tightening and qualified
additive pack upgrades remain available when retained compatibility permits them.
Apply recomputes the draft under exclusive admission and the existing atomic
activation path. Refresh on
`IDEMPOTENCY_CONFLICT`; retry `WORKSPACE_BUSY` after accepted work settles.
Runtime preparation may still fail after a successful preview.
