# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Continue the user's already selected execution method. Do not ask for it again.

**Goal:** Deliver simple document indexing and evidence search, plus real administration of one workspace and multiple named collections.

**Architecture:** Extend the existing scoped services for collection administration and safe configuration workflows. Reorganize the React application around Knowledge and Administration with shared list/detail components. Retain the existing durable ingestion/query/inspection contracts. Integrate the separately planned streamed PDF and local OCR backend into the single final ingestion workflow.

**Spec:** `docs/superpowers/specs/2026-10-14-dashboard-redesign-design.md`.

**Related required specifications:** `2026-10-14-indexed-content-inspection-design.md` and `2026-10-14-pdf-intake-design.md` in the same specs directory.

**Execution environment:** use the existing Python 3.12 environment from this worktree: `PYTHONPATH=/Users/tychota/Documents/Code/openSource/tychora/.worktrees/dashboard/src ../ci-fixes/.venv/bin/python -m pytest ...`. The absolute import path keeps subprocesses in temporary working directories on this checkout. Commands below written as `uv run pytest` describe test selection; do not install or resolve dependencies merely to run them. Use the existing environment for Ruff/typing. The only planned new direct dependency is already-locked TOMLKit for safe configuration editing; declare it in the relevant extras and update the lock without changing unrelated resolutions. Check actual test paths before invocation.

## Global Constraints

- One workspace, multiple named collections; preserve stable collection/source/revision/extraction identities.
- Existing custom/restricted grants never broaden. Workspace-wide operations require explicit complete owner authority. Collection changes abort stale requests and clear incompatible UI state.
- Every control invokes a real supported service and displays its actual result. Unknown counts are not zero; stored chunks are not proof of active snapshot membership; OCR is derived and review-required.
- Ordinary JSON stays capped at 1 MiB. Streamed PDF intake, bounded local OCR and their provenance/resource limits follow the separate PDF specification.
- Preserve current native text ingestion, durable job retry/recovery, citations, original inspection, relation semantics, evaluation and history behavior.
- Retain warm neutral surfaces and the forest-green accent. Use readable text, compact operational tables, clear navigation groups and progressive technical details.
- Credentials, queries and imported content remain memory-only in the browser. No live remote providers, model downloads or user-document content in committed fixtures.
- Preserve all worktrees, verification records and the running preview's current corpus. Switch preview processes only after a fresh verified backup and identity comparison.

## Cross-plan execution

The exact-content API is implemented and reviewed through `c8ecfdd`; consume its actual DTOs and fixtures. This plan's document/search tasks fulfill Task 2 of the indexed-content inspection plan, avoiding duplicate UI work.

PDF backend Tasks 1 and 2 execute under `docs/superpowers/plans/2026-10-14-pdf-intake.md`. They must be reviewed before this plan's PDF integration task. Its acceptance criteria also fulfill PDF Task 3. The controller may interleave independent backend and frontend tasks sequentially, but never run competing implementation agents. Root design/ledger work and read-only reviews may proceed independently.

## Review Focus

- Newly created collections work immediately and after server reopen for the managed owner; existing restricted credentials retain their original authority.
- Archive and grant changes affect engine behavior and invalidate policy/cache state, not just navigation labels.
- Named scope selection cannot leak stale content, and administration does not accidentally use a narrowed query client for whole-workspace maintenance.
- Readable returned passages, retained chunks and full units remain distinct, including clipped anchors, Unicode, derived OCR and denied dependencies.
- Guided model settings preserve credential references and configuration semantics; activation cannot silently reinterpret existing index generations.
- The Add documents flow uses the same final component for text and streamed PDFs, and errors/reset/retry/confirmation are tied to the current input and destination.

### Task 1: Title-only document kind, metadata forms and qualified additive registry compatibility

**Files:** create `src/tychora/configuration/additive_compatibility.py`, `src/tychora/profiles/general-documents/profile.toml`, `src/tychora/profiles/general-documents/kinds/document.toml`, `src/tychora/profiles/general-documents/schemas/document-metadata.json`, `tests/configuration/test_additive_registry.py`, `tests/acceptance/test_document_kind_compatibility.py`. Modify `profiles/manifest.json`, `configuration/indexed_representation.py`, `configuration/registry.py`, `configuration/resolve.py` only where proof plumbing is required, `application/settings.py`, `application/query.py`, retained replay/reuse in `application/sources.py` and `application/inspection.py` where required, `indexing/coordinator.py`, `application/discovery.py`, `interfaces/http/routes.py`, `contracts/operations.py`, `api.py` default configuration, and native/Docling qualification tests. Preserve `profiles/documents/` legacy files unchanged.

**Pack version recommendation:** retain `documents@1` and its manifest digest; add `general-documents@1` extending `builtin:documents@1`, with only the new kind/schema and explicit compatible evidence-lane membership. Existing kinds, metadata schemas and their envelope/digests stay byte-identical. Add `document@1` to evidence lane with role supporting_evidence; preserve every old lane property, citation prefix, relative kind membership and mode restriction; default compose already admits evidence. `general-documents@1` must project old evidence-lane membership plus document; inheritance merge replaces lists, so enumerate old three evidence kinds plus document explicitly. Manifest gives new pack its own digest. New workspace defaults use general-documents@1. An existing workspace explicitly activates its registered local profile root from documents@1 to general-documents@1 through the proven additive compatibility path; do not silently rewrite its registered root. No rebuild/relabel of retained content. This versioning avoids silently replacing a checksum-qualified documents@1 pack; The current builtin inheritance loader permits this additive extension pack; no duplication of the 15 legacy kind/schema definitions is needed.

**Kind/schema content:** new kind key document, human label Document, `normalizer="structured_sections@1"`, `metadata_schema="schemas/document-metadata.json"`, `role="supporting_evidence"`, `default_lane="evidence"`, allowed uses compose/audit/lookup. New schema ID `document-metadata@1`, copy only existing supported metadata properties/bounds from metadata@1, `required=["title"]`, `additionalProperties=false`; omit no real restrictions on supplied optional fields. Existing schema IDs/files remain untouched. Native text/Markdown adapter and rich Docling PDF already dispatch structured_sections@1; qualify both rather than claiming adapter existence proves correctness. Scanned PDF/OCR proof uses concurrent PDF task contracts, never invented OCR settings.

**Produced interfaces:**

```python
def qualified_additive_registry_extension(retained: ResolvedConfig, current: ResolvedConfig) -> bool: ...
async def kind_metadata_schema(kind_id: str, scope: RequestScope, ctx: ExecutionContext) -> KindMetadataForm: ...
```

The compatibility function is a predicate, not a fingerprint replacement. It first retains existing credential rotation and monotonic offline/root tightening rules, then proves all remaining representation configuration values exactly equal: extraction/parser/normalizer options, text/image provider/model/dimensions/chunking/accounting, graph/index semantics and all nonpermitted values. Existing kinds/schemas/relations and transitive definition/schema-reference bindings have identical canonical meanings. Added kinds must use the already-qualified normalizer and schemas validated by LocalSchemas. Existing lane/role/mode/facet/output meanings stay identical; the sole lane change permitted is insertion of newly added kind IDs into a preexisting lane admitted under unchanged modes. Existing kind→lane mapping/order/authority/review/use policy must stay fixed. Removed IDs, edited references, rebindings, changed schemas, provider/parser model changes and arbitrary new normalizer semantics return false. Pack/origin/derived digest differences are acceptable only after this source-level proof, never ignored upfront.

Current `resolve._resolved_hashes` hashes **all** kinds/schemas/relations/bindings into extraction_fingerprint. Preserve historical `_resolved_hashes` algorithms and all old recorded fingerprints; do not recompute old artifacts using the new registry. Extend activation/pinned-runtime compatibility narrowly with the proof. Retained parser/extraction config and artifacts remain authoritative for replay/reuse; new document extractions use the new resolved config. Current config_sha may differ, but old snapshots/report identities stay unchanged and owner checks must still validate recorded config bindings. Cases in query.py and coordinator.py using pinned_runtime_compatible must receive the same proof, with negative tests against changes in every representation field. Never relax general SettingsService indexed_representation_identity rejection to `old registry subset new` alone.

Concrete reuse risk: SourceService's native reuse check hashes the entire current `registry.schema_digests` into the normalizer fingerprint, and `_retained_bundle` normalizes through the current registry. Merely allowing activation will therefore not preserve old normalization/replay identity after adding a schema. Resolve retained extraction through its validated retained registry/config binding, then apply the additive proof for current admission; do not normalize an old artifact under the expanded registry or rewrite its IDs. Include retained inspection, original sync/reuse and backup/restore in this check. Historical native parser fingerprint itself uses extraction values and must remain unchanged. Resolve the retained semantic config SHA through exact capture lineage: jobs.captured_inputs_sha matching the retained capture manifest yields effective_config_sha CAS candidates; an exact extraction-to-snapshot-membership join may supply a bounded legacy fallback. Verify CAS and retained derived config digests, require matching semantic config_sha, then prove additive compatibility. Do not scan every historical config. Cap at 64 distinct candidates (+1 exhaustion detector) and 16 MiB aggregate config payload bytes unless a stricter existing bound applies. Budget exhaustion is BUDGET_TOO_SMALL; absent/malformed exact lineage is INTEGRITY_ERROR. Never silently use the current registry when this proof fails.

**Metadata schema HTTP:** GET `/v1/discovery/kinds/{kind_id}/metadata-schema`, read, from current authorized registered kind and exact Registry.kind_envelopes transitive schema closure. KindMetadataForm returns kind/schema IDs, schema digests, required/optional typed field descriptions and bounded schema+local-reference bindings; max 256 KiB response, max 64 schema nodes. No filesystem schema-path reading, remote $ref fetching or arbitrary schema ID access. Scope must have some authorized collection and applicable allowed use; use the same current kind discovery authority rather than treating a route ID as permission. For known legacy kinds, manual requires title/publisher/edition, evidence title/author/provenance; form derives this from schema, not hardcoded UI assumptions. Unsupported field constructs remain an explicit advanced metadata editor, not silently dropped validation.

- [ ] **Step 1 — Write failing compatibility and metadata tests.** Snapshot old 15-kind pack digests and old kind envelopes from a real legacy-resolved fixture. Assert additive candidate yields 16 kinds, old envelope digests unchanged and title-only metadata passes document while failing manual when required fields absent. Assert metadata schema endpoint returns manual/evidence requirements and rejects unknown/unauthorized schema traversal. Negative compatibility tests independently change old schema property, relation meaning, lane binding, normalizer option, parser, embedding model/dimensions, graph/index settings and reference binding; every case rejects.
- [ ] **Step 2 — Add versioned pack and bounded schema endpoint.** Use new pack inheritance/manifest and actual qualified normalizer; validate profile registry and schema binding closure. Add schema form DTO/route with byte/node budgets and current kind authority. Do not edit existing metadata schemas, manifest entry documents@1 or legacy kind files. Use generic document default only after its qualification tests pass.
- [ ] **Step 3 — Implement bounded compatibility proof and pinned reads.** Compare canonical semantic projections described above, preserve old hashes/artifacts, and insert proof only into existing strict compatible runtime/activation branches. Retained mode/output behavior must remain governed by pinned config where the engine already pins it. New current registry admission must not retroactively add document kind to historical snapshots or reinterpret old source metadata.
- [ ] **Step 4 — Run mixed legacy/current real journeys.** Open/index a legacy documents@1 15-kind corpus, retain source/revision/extraction/unit/window IDs, activate the 16-kind candidate, search/inspect old manual and exact chunks, replay retained request if configured, then add title-only document text beside old manual and query both with exact citations. Assert no rebuild and old IDs/fingerprints remain unchanged. Native and rich synthetic PDF document cases retain text/locators and validate metadata; scoped channels/review states remain intact. Perform backup/restore and pinned-config/native-reuse cases. Live providers remain mocked.
- [ ] **Step 5 — Verify.** Run `uv run pytest tests/configuration/test_additive_registry.py tests/acceptance/test_document_kind_compatibility.py tests/application/test_discovery.py tests/indexing/test_publication.py tests/operations/test_backup.py tests/operations/test_restore.py -q`, plus existing native/document qualification tests relevant to structured_sections. Update docs for new pack/default and explicit additive upgrade. If the proof cannot preserve pinned read/replay behavior, keep legacy runtime operational and report blocker; do not weaken guard or ship a fake title-only form.

---

### Task 2: Named collections, managed grants, consent and document inventory

**Files to create:**

- `src/tychora/contracts/collections.py`
- `src/tychora/application/collection_policy.py`
- `src/tychora/application/collections.py`
- `src/tychora/application/workspace_authority.py`
- `tests/application/test_collections.py`
- `tests/transports/test_collection_http.py`
- `tests/security/test_collection_authority.py`
- `tests/indexing/test_collection_lifecycle.py`

**Files to modify:**

- `api.py` managed-default-owner scope construction; `application/owner.py` registration/upload/publication policy; `application/services.py` service composition.
- `application/pdf_intake.py` current lifecycle checks for new prepare/receive/analyse/submit while retained inspect/abort remains authorized.
- `application/consent.py` v1/v2 projection and lifecycle-preserving updates; `application/discovery.py` source visibility/summary batching; `application/inspection.py` distinguish retained authorized inspection from ordinary inventory.
- `adapters/sqlite/corpus.py` canonical collection handling within `commit_extraction`; `adapters/sqlite/eligibility.py`, `retrieval/eligibility.py` lifecycle facts; `adapters/sqlite/publication.py`, `indexing/coordinator.py` current-status publication checks.
- `interfaces/http/auth.py`, `app.py`, `server.py`, `routes.py`; `clients/sync.py`, `clients/async_.py`, `interfaces/http/client.py` shared public surface if the unified plan exposes new collection methods through Python clients.
- `dashboard/src/api/contracts.ts`, `api/client.ts`, `session.tsx`; HTTP contract fixtures under `dashboard/contracts/`.
- `tests/security/test_upload_consent.py`, `tests/runtime/test_upload_policy.py`, `tests/operations/test_backup.py`, `tests/operations/test_restore.py`, `tests/operations/test_migrations.py`, `tests/application/test_discovery.py`, `tests/retrieval/test_policy_cache.py`, `tests/indexing/test_publication.py` and dashboard session/contract tests.
- `docs/dashboard.md`, `docs/http.md`, `docs/workspaces.md`, `HANDOFF.md` after verification.

**Produced interfaces (names are the integration contract):**

```python
# contracts/collections.py: strict Contract DTOs; use existing Identifier,
# NonEmptyStr, AwareDatetime, Cursor and bounded page limits.
class CollectionPolicyV2(Contract):
    schema_version: Literal[2] = 2
    upload_providers: tuple[NonEmptyStr, ...] = ()
    owner_upload_providers: tuple[NonEmptyStr, ...] = ()
    archived_at: AwareDatetime | None = None
    default_kind_id: Identifier | None = None  # null inherits qualified document@1

class CollectionCreate(Contract):
    default_kind_id: Identifier | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]

class CollectionEdit(Contract):
    default_kind_id: Identifier | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]

# Shared parser upgrades v1 projection in memory; rejects duplicate keys,
# unregistered providers (unless existing injected-provider mode), unknown keys,
# malformed timestamps and unknown versions. Serializing a mutation emits v2.
def parse_collection_policy(raw: str, *, allow_injected_provider_ids=False) -> CollectionPolicyV2: ...
def collection_is_active(raw: str) -> bool: ...

# CollectionService(owner) async methods perform canonical work on owner lane.
async def list(scope: RequestScope, ctx: ExecutionContext, *, cursor=None, limit=50, include_archived=False) -> CollectionPage: ...
async def get(collection_id: str, scope: RequestScope, ctx: ExecutionContext) -> CollectionSummary: ...
async def create(request: CollectionCreate, scope: RequestScope, ctx: ExecutionContext, *, managed_owner_authorized: bool) -> CollectionSummary: ...
async def edit(collection_id: str, request: CollectionEdit, scope: RequestScope, ctx: ExecutionContext) -> CollectionSummary: ...
async def set_archived(collection_id: str, archived: bool, scope: RequestScope, ctx: ExecutionContext) -> CollectionSummary: ...
async def consent(collection_id: str, scope: RequestScope, ctx: ExecutionContext) -> CollectionConsentView: ...
async def documents(scope: RequestScope, ctx: ExecutionContext, *, cursor=None, limit=50) -> DocumentPage: ...
```

CollectionSummary contains UUID/workspace/slug/title, archived_at, access_floor, policy_epoch and bounded summary fields. Page limits 1–100; opaque cursors bind to scope, subject and policy epoch. Return only granted readable rows. Name sorting/filtering can initially be done over the current bounded frontend page; do not claim global sorting if cursor order remains UUID.

CollectionConsentView contains collection/owner/current caller provider sets, enabled provider sets, policy epoch, and whether managed grant refresh is available; it contains no secrets. `/v1/handshake` adds `managed_workspace_owner: bool` and `policy_epoch`, preserving existing fields. This boolean is display metadata, never authorization input.

Consumes Task 1 kind metadata form/qualified document kind. Add an explicit optional `default_kind_id` to collection create/edit, validated against current registered kind authority and normalizer capability; it does not mutate existing source kinds. v1 policy projection uses null default, strict v2 has canonical optional default_kind_id and the parser/consent mutation must preserve it. Default collection legacy policy can inherit document@1 only when current runtime actually qualifies that kind; legacy 15-kind runtime returns no easy default rather than pretending manual is title-only. Display schema-derived required fields when a collection selects manual/evidence/other domain kind.

For edits, omitted default_kind_id preserves the current setting; explicit null restores inheritance. Use field-presence semantics instead of confusing an omitted value with a requested reset.

**HTTP:** GET `/v1/collections`, POST `/v1/collections` (201), GET/PATCH `/v1/collections/{id}`, POST `/v1/collections/{id}/archive`, POST `/v1/collections/{id}/reactivate`, GET `/v1/collections/{id}/consent`, GET `/v1/documents`. Creation takes title/slug and optional registered default_kind_id only: generated UUID, floor 0, strict v2 with empty provider sets/active lifecycle. Existing consent changes keep POST `/v1/jobs` operation `provider-upload-consent`; no duplicate endpoint. GET handshake remains the grant refresh response.

**Managed LocalAuth:** add explicit constructor parameter `managed_owner_token: str | None = None`, used only by `server.serve` and managed tests. Trusted server/embedded composition explicitly binds exactly one managed slot; verify owner.managed_default_owner is true and its initial scope equals the exact current owner scope. Do not read or compare a hidden runtime-file credential: constructor composition is the authority boundary and supports synthetic preview/e2e credentials. Reject invalid or duplicate managed binding at app composition. Store a distinct managed slot; never infer it from principal, capabilities or collection coverage. A private `refresh_managed_owner(scope)` replaces only that slot with a fresh immutable scope after an authorized owner-lane change; static `_grants` entries remain unchanged. RawBoundary places validated managed-identity status in request state from token authentication, never headers/body. Creation requires this status and exact full workspace scope, including capabilities/access/uses; narrowed `X-Tychora-Scope` cannot authorize workspace creation. Managed grant derivation enumerates all current canonical collections including archived, respects unchanged access ceiling/uses/capabilities and derives upload authority from canonical consent plus enabled providers. Reopen performs the same derivation; it never applies to an explicit caller scope. Update attached workspace/default future-call clients when owner scope changes; never mutate scopes captured for accepted jobs.

**DocumentPage:** one paginated authorized source batch, title fallback to source_key, collection title/ID, source type, visible selected revision/extraction IDs, known original media/size, known timestamps, publication/channel observations and nullable counts. Keep processing_state, published_content_state and channel readiness distinct. An indexing failure can coexist with previously published content. Existing source/job lineage is not cheaply indexed: use a single bounded recent authorized job scan (max 100 records, max 1 MiB decoded capture metadata total) shared across the page only when safely associable; otherwise `latest_job=null`, `processing_state="unknown"` and `job_observations_complete=false`. Do not decode entire corpus artifacts per document. Unit/window counts use batched queries over page IDs and exact eligibility; cap observations at 5,000 returned candidate records for the entire page and return null/complete=false when the cap or partial authorization prevents exact counts. SQL scan cardinality must be bounded before Python filtering. Do not count hidden revisions or label unavailable size zero. Report limits in DTOs. This conservative initial summary is preferable to inventing current status; a persistent job-subject index is a separately justified optimization, not required schema growth here.

If the existing exact eligibility evaluator requires unbounded snapshot/artifact materialization, leave unit/window counts null with counts_complete=false and an explicit bounded-observation reason, including for small documents. Do not create a second eligibility evaluator for this inventory. Exact counts are optional only when a shared existing authorization path can prove them within the stated SQL/byte bounds. Keep regression coverage for hidden rows, incomplete observations and the 5,000-candidate ceiling; the exact paginated content/chunk APIs remain the way to inspect actual records.

- [ ] **Step 1 — Pin policy, identity and security failures.** Add strict v1/v2 parser tests, reserved default slug tests, duplicate/blank title/slug tests, UUID-stability tests and managed-vs-static grant tests before service changes. Representative parser assertion:

```python
def test_v1_collection_policy_remains_active():
    policy = parse_collection_policy('{"schema_version":1,"upload_providers":[]}')
    assert policy.archived_at is None
    assert policy.upload_providers == ()
    assert policy.owner_upload_providers == ()

def test_unknown_collection_policy_field_fails_closed():
    with pytest.raises(TychoraError) as error:
        parse_collection_policy('{"schema_version":2,"upload_providers":[],"owner_upload_providers":[],"archived_at":null,"surprise":true}')
    assert error.value.code == "INTEGRITY_ERROR"
```

HTTP fixture: construct real Tychora with the existing synthetic Counter pattern, explicit synthetic managed credential with exact owner scope, one static token cloned from current owner grant, one read-only token. Test create via static (403), managed narrowed scope (403), managed full scope (201); refreshed handshake/new ingestion succeeds only for managed; static scope unchanged before/after reconnect. Reopen managed owner must retain every collection and UUID.

- [ ] **Step 2 — Implement parser/service/refresh minimally.** Owner transaction checks workspace, collection grant/access and manage; UUID4 create and unique slug conflict produce canonical errors without raw SQL detail. Reserve default slug/UUID against create/change but permit default title edits. Strip whitespace before validating title. Mutations are idempotent; epoch bumps only on actual state/policy change. Consent mutations copy parsed policy with updated provider sets so archived_at survives. New create defaults grant no upload consent. On consent grant, derive and refresh only managed future-call grant; restricted tokens remain capped. Revocation removes managed upload authority and current canonical checks block subsequent attempts for every caller. Return a fresh handshake when the client asks; no credential ever appears in response.

- [ ] **Step 3 — Enforce lifecycle at each load-bearing boundary.** Add `collection_archived: bool = False` to detached RevisionFact; propagate from parsed current policy into eligibility and reject it in ordinary query/graph/vector candidate policy. Gate source registration, sync/retry admission, provider dependency checks and final canonical publication. Also gate new PDF preparation, receipt receive/analysis and submission against current collection lifecycle; archived receipt inspection and abort remain allowed. Add an archive-between-analysis-and-submit regression so a retained grant ID cannot admit new PDF work. Check archive again inside `CorpusStore.commit_extraction` transaction and `PublicationCoordinator` transaction; add coordinator checks before projection/provider work. Policy epoch must invalidate existing query/graph caches. Authorized retained inspection/job authorization stays available; do not remove IDs from grants or blindly add active filtering to every collection join.

Collection commit rule: under existing `_write` transaction, fetch canonical collection UUID/workspace/access_floor/slug/title/policy. Verify incoming immutable UUID/workspace/source linkage; a changed/higher floor must pass current scope and current policy checks and cannot be lowered by captured bundle data. Use canonical mutable title/slug/consent/lifecycle for registration comparison; never overwrite them from a stale bundle. Reject archived even if capture observed active. Preserve existing monotonic floor safeguards; do not bypass `_register` globally for sources/revisions.

Snapshot publication retains unchanged prior members from archived collections. Their presence must neither block indexing into an active collection nor remove their coverage on a later reactivation. Inside the canonical publication transaction, check the actual source job target and newly introduced or changed archived membership. Bind the target through exact durable source authorization/capture; a broad granted collection set is not evidence of the job's target. Preserve the existing PublicationPlan and snapshot identities. Test archive A → publish B → query excludes A → reactivate A restores the same retained coverage, and archive a reuse/sync target after plan preparation → publication rejects.

- [ ] **Step 4 — Prove race/retention behavior.** Pause a synthetic source job between capture and publication with a deterministic Event: archive, resume, assert no new active snapshot publication and canonical scope failure. Repeat rename and consent change, assert successful publication retains new title/slug/consent rather than stale bundle fields. Test previously cached query after archive returns no collection evidence; reactivate restores query without changing source/revision/unit IDs. Assert existing job status/events and stored metadata history remain readable while archived; purge/withdraw semantics stay distinct. Consent grant/revoke tests use only mock dispatch and assert no static grant expansion. Backup an archived v2 collection plus active v1 collection, restore, verify lifecycle/provider policy and identity, then run normal packaged schema verification/same-version migration; pinned SQL checksum remains unchanged.

- [ ] **Step 5 — Add bounded summaries and session refresh.** Batch source lineage and counts for at most one page; assert hidden first rows do not leak counts/title/media, malformed/stale cursors reject, empty source has unknown extraction stats, and more than 5,000 candidate units yields incomplete/null count rather than misleading zero. Instrument DB statement count to verify no per-source unbounded scan; oversized job captures produce unknown status without reading full payload. Retain full-grant ApiClient in session for admin operations; selected client stays narrowed. After create/consent, obtain handshake using full-grant request **without the stale X-Tychora-Scope header**, dispose old clients, update grant, select created active ID and increment epoch. Preserve selection if still valid; fall back to active/default only when appropriate. Static session refresh never gains IDs. Abort old requests/SSE and clear stale detail state. Never persist token outside session memory.

- [ ] **Step 6 — Verify and document.** Run `uv run pytest tests/application/test_collections.py tests/transports/test_collection_http.py tests/security/test_collection_authority.py tests/indexing/test_collection_lifecycle.py tests/security/test_upload_consent.py tests/runtime/test_upload_policy.py tests/operations/test_backup.py tests/operations/test_restore.py tests/operations/test_migrations.py tests/retrieval/test_policy_cache.py tests/indexing/test_publication.py tests/application/test_discovery.py -q`. Run scoped dashboard contract/session tests after mirrored DTO changes. Update docs with owner/static distinction, archive and bounded summaries. No success claim until these commands pass; commit only if separately assigned by root.

---

### Task 3: Guided configuration, qualified model inventory and workspace operations authority

**Consumes Tasks 1–2:** qualified additive compatibility predicate; managed request-identity marker and managed grant refresh; collection consent view and policy parser. Task 2's `workspace_authority.py` currently provides refresh only; extract the canonical full-workspace proof from `MaintenanceService._authorize_workspace` into the shared module as part of this task. Collection creation separately requires the exact managed owner grant and retains that stricter condition. Existing `SettingsService.activate(SettingsActivation)` continues to own file/runtime commit. Existing `SetupService.plan/apply` and POST jobs setup own provisioning; MaintenanceService remains the operation dispatcher.

**Files to create:**

- `src/tychora/contracts/administration.py`
- `src/tychora/application/settings_draft.py`
- `src/tychora/application/model_inventory.py`
- `tests/application/test_settings_draft.py`
- `tests/transports/test_admin_models_http.py`
- `tests/security/test_workspace_admin.py`

**Files to modify:**

- `pyproject.toml`, `uv.lock`; `application/settings.py` extract shared noncommitting preflight, `application/setup.py` safe bounded verifier facade, `application/maintenance.py` workspace-mutating full-scope enforcement, `application/workspace_authority.py` shared checks.
- `interfaces/http/routes.py`, `app.py`, `server.py`; `configuration/models.py` only if needed to expose qualified supported field types; do not change provider support claims.
- `dashboard/src/api/contracts.ts` and captured HTTP fixtures; frontend Models/Settings forms belong to root UI task.
- Existing settings activation HTTP tests in `tests/transports/test_http.py`, setup/backup/recovery tests in `tests/operations/`, packaging dependency/resource checks, `docs/configuration.md`, `docs/operations.md`, `docs/http.md`, `docs/dashboard.md`.

**Produced interfaces:**

```python
# All base digests are server-issued SHA256 of exact workspace.toml bytes;
# limits cap encoded request to ordinary 1 MiB HTTP boundary.
class SettingsFieldChange(Contract):
    path: NonEmptyStr                 # dot path from the fixed allowlist below
    action: Literal["set", "inherit"]
    value: JsonValue | None = None    # inherit forbids value; set validates type

class SettingsPatch(Contract):
    base_workspace_sha256: Sha256
    changes: Annotated[tuple[SettingsFieldChange, ...], Field(min_length=1, max_length=32)]

class SettingsDraftService:
    async def fields(self, scope, ctx) -> SettingsFieldView: ...
    async def preview(self, patch: SettingsPatch, scope, ctx) -> SettingsPreview: ...
    async def apply(self, patch: SettingsPatch, scope, ctx) -> SettingsFieldView: ...

class ModelInventoryService:
    async def catalog(self, scope, ctx) -> ModelCatalog: ...
    async def setup_plan(self, selections: tuple[str, ...], scope, ctx) -> SetupPlanView: ...
    async def readiness(self, selections: tuple[str, ...], scope, ctx) -> ModelReadiness: ...
```

**HTTP:** GET `/v1/settings/fields`, POST `/v1/settings/preview`, PATCH `/v1/settings`; preserve GET/PUT `/v1/settings` for redacted inspection/expert activation. GET `/v1/models` gives qualified inventory; POST `/v1/models/setup-plan` with `{selections:[...]}`; GET `/v1/models/readiness?selection=...` for explicit selections. Setup execution stays POST `/v1/jobs` `{operation:"setup", assets:[...],offline,local_bundle}`. No alternate activate/download/rebuild/backup executor.

**Allowlist:** only `providers.text/image/rerank/vision` provider/model/credential_ref plus supported embedding dimensions/group/request limits; `extraction.parser`; PDF OCR remains an explicit per-intake policy, with readiness inspection rather than invented global OCR fields; `retrieval.rerank`, declared bounded retrieval options and existing graph inspection bounds. Implement from actual `ConfigurationValues` schema and qualified runtime capabilities, not free-form nested JSON. Reject paths to profiles/defaults/root bindings, pack definitions, global registries, arbitrary filesystem roots, credentials themselves or undeclared OCR controls. Allow `inherit` to remove a workspace override leaf so resolved profile value resumes; never convert null to TOML. No whole providers overwrite and no auto-profile switching. Include offline only if root explicitly decides a workspace network-policy form belongs in this task; current plan omits it from guided edits and keeps expert TOML route.

SettingsFieldView contains active profile, base digest, allowlisted effective/override values with origins, credential reference strings for authorized managers only, registered qualified choices and representation-compatibility blockers. Other GET settings stays redacted. Reference disclosure is safe for this full-workspace manager surface; secret values are never resolved/returned. Credential readiness is `not_checked` initially—no remote connectivity probe or secret resolution side effects. Qualified model choices come from actual runtime/asset support, not permissive settings literals: `rerank-3` may be parsed but remains unavailable if runtime tokenizer qualification only supports `rerank-2.5`.

Preview returns redacted effective change list, base/candidate digests, `activation_allowed`, concrete blocker codes and `rebuild_queued=false`; no TOML or secret/ref dump is returned. Apply resubmits patch+base digest, recomputes server draft and preflight, then calls existing activation under exclusive admission. No persistent draft cache needed. Reject stale base with `IDEMPOTENCY_CONFLICT`; incompatible generations with existing `PROFILE_INCOMPATIBLE`; busy scheduler with existing retryable `WORKSPACE_BUSY`. Preview is not an activation guarantee; apply rechecks everything.

**Setup plan/readiness:** reuse pinned `ModelAssetPlan` selection/manifest/hash/license notices. Report total declared bytes and per-selection installed/missing/corrupt/not_checked, checked bytes and complete flag. No arbitrary provider marketplace and no package-installed=assets-ready claim. Bounded verification request: at most 4 selections and 64 assets, read budget 256 MiB and execution deadline 5 seconds; stop at budget/deadline and mark unchecked assets `not_checked`. Use existing no-follow regular-file/size/hash verification. Budget bounds actual bytes read, not only reported declared sizes. Readiness calls never create cache directories or download assets. A missing file is missing; checksum mismatch is corrupt; interrupted verification is unknown. A single large asset exceeding the budget returns `needs_full_verification`, not missing/corrupt; Expose a previous full-verification/setup receipt with verification timestamp and matching manifest/hash identity as historical evidence; it must not promote a deferred current check to ready solely because current size/mtime matches. The returned verification state labels whether a full hash was measured in this operation or reported by the explicit durable setup result. Existing explicit setup is the full verification path: it already checks the complete file hash and avoids downloading a verified asset. Initial UI must offer full verification/setup deliberately, must not loop setup or suggest download for not_checked assets. If no applicable durable verified receipt exists, large-asset readiness stays deferred until that explicit path completes; no new unlimited read endpoint. Directories/symlinks fail safe and sanitized. Expose manifest URLs/notices only from trusted bundled inventory. Setup completion may return an already-completed durable job because current endpoint waits; do not fabricate queued progress. Do not change setup execution/cancellation model in this task.

**Workspace operation authorization:** align full-scope proof across PATCH/PUT settings, setup, export/import, rebuild and migration. Require manage plus canonical complete workspace collection/access/uses authority; creation additionally requires managed installation-owner marker. Fixed full-workspace manage grants may execute existing global operations if they satisfy canonical proof, but never autoexpand. Health without mutation remains read+whole-workspace proof; resume-deletions requires manage. Source withdrawal/purge and per-collection consent remain narrower operations. Settings route global mutation must not accept selected-collection scope accidentally. RawBoundary currently marks only PUT `/v1/settings` activation: include PATCH apply as exclusive admission, preview as nonmutation; internal direct API must use the same guard rather than relying only on HTTP middleware.

- [ ] **Step 1 — Add focused failing settings tests.** Build a TOML fixture with multiple profiles/root bindings, comments, custom preserved fields and inherited provider references. Apply changes only within `[configuration]`; assert profiles/defaults/unchanged credential references and comments survive. Set quoted Unicode reference/name values through AST, reject raw keys/nonreference secrets/duplicate patch paths, null misuse and root traversal. Preview must not alter file/runtime/epochs or invoke provider network/setup. Stale digest rejects before activation; failure leaves old bytes and active generation intact.

```python
def test_ast_patch_preserves_unrelated_structure():
    original = ('schema_version=1\n# keep this\n[profiles.local]\n'
                'builtin="documents@1"\n[defaults]\nprofile="local"\n'
                '[configuration.providers.text]\nprovider="disabled"\n'
                'credential_ref="env:UNCHANGED_REFERENCE"\n')
    document = tomlkit.parse(original)
    document["configuration"]["providers"]["text"]["model"] = "voyage-context-4"
    revised = tomlkit.dumps(document)
    assert '# keep this' in revised
    assert tomllib.loads(revised)["profiles"] == tomllib.loads(original)["profiles"]
    assert tomllib.loads(revised)["configuration"]["providers"]["text"]["credential_ref"] == "env:UNCHANGED_REFERENCE"
```

The product test calls SettingsDraftService; this library-level snippet illustrates the preservation contract rather than replacing integration coverage. Test inherited leaf removal restores resolved origin/value, and qualified model list excludes unsupported rerank runtime choices.

- [ ] **Step 2 — Implement AST changes and shared preflight.** Declare TOMLKit directly, validate SettingsPatch as strict DTO; read bounded owner workspace bytes and digest; parse existing TOML AST and edit only allowlisted configuration leaves. Reuse resolver/registry activation checks and indexed-representation identity compatibility. Preserve registered profile structure; compute redacted field diff rather than raw file diff. Extract noncommitting preflight from existing SettingsService without replacing atomic file swap/runtime preparation/cleanup. Hold apply admission before digest check and candidate construction. Add global authority checks to expert PUT too. Preserve existing errors and rollback semantics.

- [ ] **Step 3 — Pin model-plan/readiness failures and implement bounded reads.** Tests use small synthetic manifest-backed local assets and mock downloader. Assert correct size/hash marks verified, wrong same-size hash marks corrupt, missing cache stays absent, symlink cannot escape cache, repeated requests never provision, and budgets/deadlines report not_checked without claiming ready. Catalog distinguishes configured/qualified, optional package capability, verified asset cache and upload consent. Setup plan faithfully returns notices/bytes from SetupService.plan and rejects unknown/duplicate selections. Actual POST setup still uses existing service; verify durable result/receipt and no changes to provider authorization.

- [ ] **Step 4 — Prove whole-workspace authorization across existing endpoints.** Use two collections, a full static manager and narrower manage token. For settings PUT/PATCH, setup/export/import/rebuild/migrate, narrow token returns FORBIDDEN_SCOPE; read-only token cannot mutate; full manager reaches genuine operation behavior and restricted token gains nothing after managed create/consent. Health read remains permitted only with whole-workspace access/uses. Selected collection header cannot turn global settings into a local edit. Exercise actual ASGI RawBoundary admission while a source job is accepted: preview remains read-only, apply rejects busy and no lost update occurs. No live network is used.

- [ ] **Step 5 — Verify and document contracts.** Run `uv run pytest tests/application/test_settings_draft.py tests/transports/test_admin_models_http.py tests/security/test_workspace_admin.py tests/transports/test_http.py tests/operations/test_setup.py tests/operations/test_backup.py tests/operations/test_restore.py tests/operations/test_migrations.py -q`. Run package dependency checks and repository Ruff/type checks as required by root. Mirror HTTP DTO fixtures and run dashboard contract/type checks once UI wiring exists. Document credential reference setup, no remote readiness probe, incomplete verification, global scope requirements and populated-generation rejection; no fake rebuild plan. Commit only if separately assigned.

### Task 4: Application shell, collections and readable document workspace

**Files:** refactor `dashboard/src/App.tsx`, `session.tsx`, `pages/OperationsRouter.tsx`, `api/contracts.ts`, `api/operations.ts` and `styles.css`; create focused shell/navigation, collection and document modules; reuse/refactor `pages/sources/SourceForm.tsx`, `Coverage.tsx`, `components/EvidenceReader.tsx`, `Operations.tsx`. Add frontend behavior tests and update route fixtures. Build committed dashboard resources.

**Consumes:** Task 2 actual collection/inventory DTOs and auth refresh behavior; exact-content API report and fixtures under `.superpowers/sdd/2026-10-14-indexed-content-inspection/` and `dashboard/contracts/extraction-{units,windows}.json`. Existing source ingestion and reader services remain authoritative. Task 3's configuration API is consumed later by administration pages.

- [ ] Write failing behavior tests for named collection creation/selection/refresh, static restricted grants, archive/reactivate, collection switch cancellation, direct links and old route compatibility. Use real captured backend contracts, not invented counts/statuses.
- [ ] Build a shared shell with Knowledge (Collections, Documents, Search, Explore) and Administration (Jobs, Models, Evaluations, Activity, Settings). Keep workspace/connection context visible and replace UUID checkboxes with named scope selection. Provide a full-grant administration client separately from the selected-collection client; Activity retains the exact query scope it represents. Keep auth tokens out of storage and URLs.
- [ ] Support one/several/all-granted active collections for reading and querying, and exactly one explicitly selected ingestion destination. Workspace forms visibly use Entire workspace; Jobs uses full-grant visibility, Activity explains exact selected-query-scope history. Test these distinctions with a collection selected during health/backup, multi-collection query and scope-specific empty activity.
- [ ] Build Collections with a compact searchable table, create/edit inline form, real lifecycle controls and links to documents/search. Put IDs and consent/policy detail in secondary inspection. New collections use the server's safe defaults. Product archive confirmation explains retained data and search/ingestion effect; do not perform destructive collection deletion.
- [ ] Build Documents as a useful toolbar and inventory beside a selected-document pane. Display source name, collection, current processing state and only reported type/size/time/counts. Offer Overview, Content, Chunks, Relationships and History. Preserve revisions, extraction coverage and original download/inspection. Source withdrawal uses the existing manage operation and consequence confirmation; label it accurately, not physical purge.
- [ ] Implement extraction units/windows browsing with actual lineage URLs, source ordering, complete server text, locators and compact ID/bound/token metadata. Include pagination through empty scope-limited pages, cursor expiry restart, loading/error/empty/denied states and selection cancellation. Full unit deliberately opens the existing evidence reader; no JavaScript codepoint slicing or fabricated active status.
- [ ] Create the single Add documents route and shared staged form. Text is a simple editable title/content/destination flow. Render required metadata fields from actual registered schema or a qualified known schema; do not invent publisher/author/edition values or weaken validation. Advanced source kinds, raw metadata and registered paths remain available. Preserve the reviewed input-switch and idempotency fixes. PDF integration will extend this same workflow after its backend is reviewed.
- [ ] Use the real document@1 kind for the simple path, or an explicit collection default; render specialized required fields from registered schema. Add the prerequisite/setup-and-return journey using authoritative readiness, preserving in-memory drafts and unexpired receipts and invalidating stale analysis. Canonical collection/model editors are linked, never duplicated.
- [ ] Apply consistent buttons, fields, tabs, status labels, tables, toolbars, detail panels and useful empty states. Preserve existing local typography while improving normal-zoom readability and hierarchy. Desktop uses an approximately 208px sidebar and flexible list/detail layout; narrow screens use accessible navigation and list-to-detail transitions. No document-wide horizontal overflow, nested card grids or decorative metric panels.
- [ ] Use a readable 15–16px base, 13–14px dense table text and 16px or larger passage text; secondary labels remain legible and meet contrast requirements. Global inventory sorting/filtering uses backend support; otherwise label the loaded-row scope explicitly and show partial counts/continuation.
- [ ] Run covering frontend tests, full frontend suite, typecheck, format and production build. Add real-HTTP browser journeys for create collection → add text → document content/chunks, including mobile and keyboard navigation. Record desktop/mobile screenshots in ignored verification artifacts. Commit source/tests/bundle and report actual contracts and remaining integration obligations.

### Task 5: Simple search, bounded exploration and operable administration

**Files:** refactor `pages/Search.tsx`, `Jobs.tsx`, `Settings.tsx`, `Evaluations.tsx`, `Activity.tsx`; create focused `pages/Models.tsx`, `Explore.tsx`, `components/ReturnedPassage.tsx`, `StoredChunks.tsx` and shared administration forms as needed; integrate `components/Relations.tsx`; extend API modules, frontend tests, E2E and styles. Avoid giant page components by grouping independent responsibilities.

**Consumes:** Task 3 actual safe settings/setup DTOs, Task 4 shell/session/components, existing durable operations endpoints and exact-content API. The inspection plan's Task 2 search acceptance is binding here.

- [ ] Write failing search tests: readable exact excerpt blocks by default; matched/context distinction; omissions/completeness; clipped search anchor resolves larger retained chunk; null window ID remains null; Full unit is deliberate; structured-only segment remains selectable; HTML text escapes and non-BMP text remains exact. Preserve citations, original bytes, relations and response export.
- [ ] Simplify Search to query, named collection scope and one primary Search action. Put kind catalog, filters, provider/retrieval options and request JSON behind meaningful advanced controls. Display results with readable titles/snippets and a persistent selected reader. Returned passage / Stored chunks / Full unit are accessible view controls with clearly distinct text and ranges. Do not present a generated answer or imply a chat provider exists.
- [ ] Show a relevant always-visible notice when indexed review-required OCR is excluded by query filters, with a supported explicit inclusion action and useful empty-result guidance. Inclusion never accepts evidence or changes citable status. Test excluded/included behavior against the actual review contract. A compact visible query-state label may always state whether review-required content is included; derive it from the submitted filter, without inventing an excluded-document count or claiming scans exist when inventory is unknown.
- [ ] Build Explore around actual selected source/unit seeds and bounded relation responses. Provide table/list and optional graph views using only returned nodes/edges and explicit unavailable/truncated states. Node selection opens actual inspection; do not invent full-corpus graph coverage, labels or edges. Keep keyboard/list access equivalent to pointer graph use.
- [ ] Rework Jobs into an operational table and stage/event detail, showing durable state and supported cancel/retry actions. Connect source and document routes, retain SSE reconnect/dedup/recovery behavior and explain operation-specific action availability. Do not offer maintenance retry when the service cannot execute it.
- [ ] Build Models with separate extraction/OCR, text/image embedding and reranking sections. Use actual capability/setup readiness and configuration drafts; show local assets, configured provider, credentials reference and upload consent separately. Guided forms validate before activation and show incompatible-generation errors with a truthful next step. Keep the full expert TOML path if needed. Never store actual provider secrets or equate installed packages with a successful live connection.
- [ ] Build Settings sections for workspace context, measured health, compatible-channel rebuild and backup/restore operations; link to the canonical collection consent editor. Use existing plan/confirm original-purge workflow in an advanced retention panel. Show server path and clean-destination semantics explicitly where the backend requires them. Confirm destructive mutations in the product, link receipts and apply full workspace authority. Unsupported trace/history purge/model migration controls are not operational buttons.
- [ ] Refine Evaluations and Activity into readable tables/detail with existing dataset/run/compare and exact-scope history semantics. Add existing dataset registration if it can use the real bounded API; show metadata-only/replay-unavailable states and bounded-list limitations. No aggregate quality charts without measured data.
- [ ] Verify real HTTP journeys for exact search → retained chunk → full unit, archived-collection exclusion, source job recovery, bounded relationships, guided setting validation, health and evaluation results. Run covering backend tests if integration changes require them, full frontend tests/type/build/format and focused E2E. Commit source/tests/bundle and report unsupported engine capabilities accurately.

### Task 6: Streamed PDF workflow in the final document experience

**Prerequisite:** Independently reviewed PDF backend Tasks 1 and 2, with actual intake DTOs, captured policy and OCR provenance fixtures. This task also fulfills Task 3 of the PDF implementation plan; read its acceptance brief, do not build a second source form.

**Files:** extend the single Add documents workflow from Task 4; add a focused `pdfIntake` client/module, actual DTOs, tests and E2E fixtures; integrate document/job/OCR review presentation; update docs/dashboard.md, HANDOFF.md and generated resources.

- [ ] Implement File-body upload with preparation, progress, local analysis and exact analysis/policy confirmation. Show size/page count/native-scanned-mixed classification and capability blockers. Never base64-encode large PDFs or read them into one browser array buffer. Changing input or destination invalidates stale analysis/confirmation and abandons its private receipt safely.
- [ ] Preserve same-intent submission retry, current collection scope, cancelled request guards and durable job receipt. Plain text remains independently submit-ready after any PDF error. Failed upload/analysis/ingestion are distinct; accepted/queued is not indexed.
- [ ] Display actual OCR progress and readable derived content with physical page locators, review state and exact-input provenance. Clarify when the current search review filter excludes indexed OCR. Do not auto-accept transcription or fabricate source-native text.
- [ ] Run the complete PDF UI acceptance from the related plan using a generated synthetic PDF larger than the old JSON cap through actual HTTP. Cover native/scanned/mixed/blank/error/partial states, cancellation, retry and collection switching. Keep existing text/root-path workflows passing.
- [ ] Update usage and handoff documentation around the final navigation and indexing/query/admin workflows, including setup prerequisites and actual limits. Run full frontend/type/build/format, focused integration suites, real browser desktop/mobile journeys, static bundle drift and package resource checks. Report every unqualified case rather than claiming full model quality.

### Task 7: Integrated review, preserved preview and supplied-document verification

**Files:** integration tests and documentation corrections only as review requires; private migration/probe scripts and verification records remain ignored. The controller coordinates actual preview switching; no worker stops the original preview independently.

- [ ] Perform final whole-feature review across backend administration, exact inspection, PDF ingestion and final UI. Carry all earlier deferred/parked findings and focused test evidence; fix material findings through one reviewed implementation wave.
- [ ] Refresh the online SQLite/artifact backup of the running preview, verify integrity and preserved source/job/extraction/chunk identities, and start the reviewed application against a persistent restored workspace. Verify the existing source and new inspection routes before changing the advertised preview. Do not let the disposable process cleanup destroy the only copy.
- [ ] After synthetic and bounded local-model verification, ingest the user-supplied PDF locally through the real intake/job flow. Monitor resource/page coverage, retain a durable job and verify immutable original identity, derived OCR provenance and accessible indexed chunks/search behavior. No remote provider use or corpus contents in Git. If actual full-document qualification fails, preserve the failure and captured original and fix the reproduced defect within the established bounds; do not label a partial extraction complete.
- [ ] Run final focused smoke checks on the retained corpus, confirm the URL and connection instructions, preserve all verification records and report implementation status, test evidence and material limitations. Do not merge, push or publish without existing authorization for that external action.
