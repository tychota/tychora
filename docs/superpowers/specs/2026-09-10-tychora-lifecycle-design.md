# Tychora — lifecycle, backup and recovery contracts

Date: 2026-09-10
Status: engineering review draft; implementation acceptance remains outstanding
Related: [Engineering](2026-09-10-tychora-engineering-design.md), [Storage schema](2026-09-10-tychora-storage-schema.md), [Configuration](2026-09-10-tychora-configuration-design.md)

These contracts refine the existing backup/import, source-removal, destructive original-deletion and idempotent-write operations. SQLite remains canonical; Chroma and Ladybug remain rebuildable projections. An operation is complete only after its durable receipt describes what actually happened.

## 1. Default backup and restore

### Default mode: canonical backup

`manage export` defaults to `canonical` mode. It produces a self-contained directory/archive containing:

- A consistent SQLite backup, including source/revision/extraction records, current policy and tombstones, exact snapshot/batch bindings, retained operational/evaluation records, and the copied lexical tables/FTS data.
- Every payload required by the copied database's retained typed roots, including originals and repository files, extraction/assertion/diagnostic artifacts, resolved registries and schemas, index input manifests, retained query/evaluation payloads, and referenced embedding caches. Include all registered artifacts marked `present` or `delete_pending` as additional conservative backup roots, even if logically unreferenced, so no copied availability row falsely promises absent bytes. Walk typed manifest references transitively; neither a filename list nor database foreign keys alone establish completeness.
- A versioned `backup-manifest.json` inventory of each included payload's hash, size, media type and relative archive path; intentional tombstones; the database hash; workspace/schema/config versions; captured policy/statistics epochs; snapshot IDs; and an explicit list of omitted native projection namespaces.

The copied database defines the retention cut. A tombstoned historical original binding does not itself require payload retention; its metadata/tombstone remain. A backup still includes bytes marked present or pending deletion at that cut, even if their original binding was removed. Deletion already durably reconciled to `deleted` is represented as intentional absence. Therefore export may contain tombstoned bytes whose physical cleanup had not finished; it is not a substitute for finishing an original-payload purge. A payload reached through a required retention dependency is required: a missing or corrupt original, extraction, registry or retained referenced cache fails this backup; it is not silently classified as optional. A null cache reference means no cache was promised.

`backup-manifest.json` has `schema_version=1`, backup/workspace IDs, capture time, policy/statistics epochs, schema/config digests, database inventory entry, artifact entries, retained snapshot IDs, tombstones and per-channel rebuild requirements. The final completion receipt binds its SHA-256. Relative payload paths are hash-derived, never caller filenames. Unknown required reference schemas fail export/import rather than being skipped. Only registered typed references are followed; provenance-only hashes may intentionally lack payloads.

Default mode **omits Chroma's native directory and Ladybug's native database files**. It retains their manifests/receipts and rebuild inputs. It is a source-preserving, rebuildable backup, not a promise of identical native indexes or historical ranks. If some vectors have no retained cache, the manifest declares that rebuilding those entries requires their configured embedding provider. Restoration itself performs no provider calls. Re-running extraction or embedding must use an explicitly authorized maintenance operation, and missing historical capabilities produce a declared unavailable projection rather than replacement semantics.

Exact-environment replay is outside the initial canonical-backup guarantee. A future separately qualified mode would require a quiescent export of native projection files plus recorded compatible engine/runtime/model configuration. The first release rejects requests for that unsupported mode; a canonical backup never promises reproducible remote-model behavior.

### Capture protocol and lifetime

1. Acquire the owner's maintenance barrier for publication/reference mutations, artifact deletion and projection GC. Ordinary read queries may continue. Finish or roll back any current bounded write transaction. Reconcile pending deletions before the cut: `delete_pending` with a durably absent file becomes `deleted` with a deletion timestamp; `present` with a missing file is corruption and fails export.
2. Copy SQLite with the SQLite backup API while SQLite mutations are paused. The copy has one definite policy/membership/statistics cut; do not assemble it by copying a live database file and forgetting its WAL.
3. Derive the transitive payload inventory from that copy using registered manifest schemas. While deriving the inventory, keep deletion blocked. Missing required payloads fail the operation. Convert the global deletion barrier to a backup lease on the complete inventory before resuming mutations. No payload may disappear between inventory discovery and lease acquisition.
4. Copy each pinned payload into a private destination staging area and verify its size/hash. Native projection GC may resume because canonical mode does not copy those files. Source withdrawal or a new snapshot after the cut does not change the captured backup.
5. Write the final manifest, verify the complete inventory and database integrity, flush payloads/manifest, fsync directories, then atomically publish the completed destination on a supported local filesystem. A destination lacking the final verified completion receipt is incomplete and cannot be imported. Never overwrite an existing completed backup implicitly.
6. Release backup leases only after copying and all associated filesystem workers have exited, including after cancellation. A crash leaves an incomplete destination, not a successful backup. Restart may verify/resume it against the fixed inventory or discard its staging area.

Backup is an owner-authorized whole-workspace operation, not a scoped query export. It can contain private originals, resolved packs and retained operational payloads. Current source-use/provider restrictions are retained in the copied database. Runtime tokens, owner endpoints/locks, credentials, temporary uploads and unrelated files are excluded. The receipt states the capture time/epochs: later withdrawal or deletion does not retroactively erase an already exported backup.

### Restore protocol

Restore defaults to a new, empty destination. An existing workspace is never merged or replaced by ordinary import. Verify the entire archive inventory and reject path traversal, escaping symlinks, duplicate paths and unsupported schema/manifest versions before activating it. Extract to private staging and verify database integrity/foreign keys and referenced payload closure there.

The backup is data: never execute an included pack, migration, query, trigger or view. Validate SQLite schema objects against the supported, trusted schema before querying imported tables; reject unexpected objects and load no extensions. Size/count/decompression limits apply before activation.

Preserve original stable workspace/source/evidence IDs and captured policy/tombstone state; generate fresh runtime credentials and owner files. Restore does not start a server, run jobs or construct remote providers. Report that policy is restored as of the backup cut; it cannot know restrictions introduced afterward in another workspace copy. Opening/serving the restored workspace uses current owner grants and explicit capability configuration.

For omitted native projections, preserve the sealed historical `receipt_json`, entry identities/digests and snapshot bindings. Set only their copied operational `index_batches.state` to `pending` before activation: that field describes physical availability, not a new content receipt. Record the transition in the restore job. Exact inspection and retained lexical data can operate under current eligibility; strict requests requiring missing channels fail, while explicitly permitted degraded searches report them. No query may trust a historical ready receipt without checking current batch availability.

An explicit rebuild may reuse a historical batch only when the retained inputs reproduce its original record IDs, counts, input/value digests and graph assertions. Write a separate verification event/receipt for the rebuilt namespace; never replace the sealed historical receipt or mutate snapshot membership. Sealing receipts bind sorted `(record_id, input_sha, value_sha)` records; vector value digests use finite post-normalization little-endian float32 stored bytes. Graph digests bind canonical node/edge/assertion records. Family identity includes its persisted creation nonce; a fresh family can have the same configuration digest in a distinct namespace. A provider call without retained vector caches is not proof of the old values: authorize new indexing under a new family/build identity and publish a new snapshot, leaving historical projections unavailable when equality cannot be established. Backend-native ANN layout may differ even for identical vectors, so rebuilt ranks are not guaranteed identical.

First reconcile jobs whose completed publication receipt is already present. Remaining copied queued/running/cancelling jobs become failed with an interrupted-by-restore diagnostic; import never resumes remote calls or publishes their pending work automatically. A later authorized retry uses the ordinary manifest/reconciliation protocol. Copied pending physical deletions remain suspended until explicit owner maintenance; restore does not run purge workers automatically. Atomic destination activation and a restore receipt complete the operation.

## 2. Withdrawal, original-payload release and shared artifacts

`remove_source` remains an immediate eligibility withdrawal: set the source withdrawal state and increment `policy_epoch` atomically. Physical cleanup is separate. It is not an assertion that every copy of source content has been erased.

The explicitly destructive original-payload operation is `purge_originals` in this contract. It requires an already withdrawn source and an explicit set of retained revisions. Its plan enumerates original bindings: `revisions.original_sha`, original repository manifests/files, and any original-role `revision_artifacts`. Generated figures, normalized text, extraction JSON, embeddings, query history and external backups are **not** erased by this operation; disclose that scope in the plan/result. Comprehensive content erasure is not promised by the original-payload operation.

Original deletion removes a binding's ability to serve original bytes; it does not delete the immutable revision or break its foreign keys. Append a tombstone for each selected `(revision_id, artifact_sha)` original binding. Tombstones are permanent for that historical binding and survive backup/restore. Explicit inspection of its original returns `ORIGINAL_REMOVED`, even when the same bytes remain physically present for another source. Normalized evidence, if separately retained and currently permitted, must identify the missing original and cannot claim that re-extraction remains possible.

Every artifact reference has a retention meaning. Original bindings and their original locators retain payloads only while that binding is not tombstoned. Metadata-only provenance hashes remain valid after a tombstone and do not themselves force the payload to remain. Other non-tombstoned originals, extraction/registry/evaluation payload dependencies, active build roots and filesystem/query/backup leases continue to retain their required payloads. Registered manifest reference walkers distinguish required payload dependencies from provenance-only hashes; do not treat every SHA-shaped string as a reference or ignore nested required references.

After an acquisition has terminally completed and its revisions are durable, original-byte hashes in its retained capture/job manifests are provenance-only: the revision's original bindings own payload retention. Active or not-yet-bound captures remain required roots. Thus retaining an idempotency record/capture manifest does not silently defeat an explicit purge of all of its published original bindings. The same typed-reference rules govern backup closure and garbage collection.

An artifact shared by two sources or roles is not unlinked while any live payload reference remains. The removal result distinguishes `binding_removed_payload_deleted`, `binding_removed_payload_retained_shared`, and `binding_removed_payload_pending_lease`. Shared retention counters/details are management-only, not query diagnostics. Removing source A cannot make source B's original or extraction unreadable.

### Durable deletion protocol

1. Under the owner, validate the requested original bindings and current withdrawal state; compute the proposed reference impact. Stop affected ingestion jobs from publishing/reacquiring those bindings during this operation.
2. In one SQLite transaction append binding tombstones, increment `policy_epoch`, and mark zero-live-reference payloads `delete_pending`. Commit before unlinking. Existing queries revalidate eligibility; active filesystem readers keep their leases until they exit.
3. For each pending SHA, serialize the reference recheck and physical unlink against new artifact bindings. Recompute live roots and leases immediately before deletion. If a new live reference exists, return the payload to `present` and report shared retention; if leased, defer the unlink without claiming physical completion.
4. Unlink only the registered hash-derived payload, fsync its containing directory, then commit `payload_state='deleted'`. Keep the artifact metadata row. A crash after tombstoning/before unlink leaves a safe pending deletion; a crash after durable unlink/before the final SQL update is reconciled from `delete_pending` plus observed absence. Missing files still marked `present` are corruption, not an intentional deletion.
5. Complete the job with the per-binding/per-payload outcome. Deferred physical work has an explicit pending state and is resumed by owner recovery/maintenance. Do not claim all requested payload bytes were erased while shared roots or leases retain them.

A subsequent new source can explicitly ingest identical bytes into the CAS again, after hash verification, and restore global payload availability. It does not clear an older revision's binding tombstone. Raw hash access is not a public authorization shortcut: serving originals always resolves an authorized, live revision binding first.

Before creating a new required binding, an artifact writer takes the same per-SHA guard and verifies durable payload availability, re-materializing and hash-verifying bytes when needed; an existing metadata row or `delete_pending` state alone is never proof that the payload exists.

## 3. Idempotent writes

Every write request has a canonical `request_digest`, whether or not the caller supplies an idempotency key. Hash a domain/API-version tag, workspace/principal/operation identity, and canonical validated **caller intent**: normalized source bindings/locators, supplied metadata/options, inline/uploaded content hashes and explicit configuration references. Do not include secrets, timestamps, current job state or freshly resolved mutable defaults. Preserve omitted-setting markers in caller intent; do not fill them with current defaults before comparing an existing key. This comparison requires no reread of live source bytes. Resolve default configuration once for a new job and retain its immutable registry/config artifact through `jobs.effective_config_sha`, separately from optional request diagnostics.

Atomically create or locate the job using the existing `(principal_id, operation, idempotency_key)` uniqueness constraint. If the key exists, compare its stored request digest before returning/resuming anything. Equal digests reuse that job and its pinned inputs/settings; unequal digests return `IDEMPOTENCY_CONFLICT`/HTTP 409 without modifying or scheduling work. Concurrent equal requests create one job; concurrent unequal requests cannot both be accepted under the same key. Replay still checks the caller's current capability and response-disclosure permissions.

A local pathname identifies acquisition intent, not a promise that its bytes never change. Once a job has durably captured its file/tree/sidecar inputs, a request-identical retry **does not reread that pathname**: it returns/resumes the original capture even if the live file later changes. The response identifies that captured revision and replayed job. To ingest later bytes at the same path, submit a new key or a new sync operation. Inline/upload bytes or an explicitly supplied immutable content digest are part of caller intent; changed supplied bytes/digest under the same key conflict.

Before a first durable capture exists, retry may finish acquisition from the current path; no original-byte identity has yet been promised. Persist the capture manifest and actual captured hashes/time before any extraction/indexing/provider stage. Recovery never quietly substitutes new live bytes for an already committed capture. A missing committed capture is an integrity/recovery error, not permission to recapture under the old job.

The durable idempotency binding outlives optional request-body/job-diagnostic retention. Retain at least job identity, request digest, terminal state and result reference for the workspace lifetime; purging diagnostic payloads does not free a used key for another request. A replay whose original detailed response was deliberately removed returns its surviving job/result metadata with an explicit retention limitation, never new ingestion.

## 4. Storage bindings and transport mapping

The [SQLite schema](2026-09-10-tychora-storage-schema.md) declares:

- `artifacts.payload_state` (`present`, `delete_pending`, `deleted`) and `payload_deleted_at`. Artifact identity, size and content digest remain immutable; availability changes explicitly. The deletion timestamp is present exactly in the `deleted` state and is cleared after verified re-materialization.
- `jobs.request_digest` and `jobs.effective_config_sha`, required immutable bindings. `jobs.captured_inputs_sha` is null until acquisition commits and thereafter immutable. Its typed manifest binds captured originals/sidecars, hashes and acquisition settings. Retaining the capture manifest does not indefinitely retain tombstoned original payloads after publication.
- `original_payload_tombstones(revision_id, artifact_sha, removal_job_id, removed_at)`, keyed by `(revision_id, artifact_sha)`. The removal repository validates that each denotes an original binding of that revision; this union of root-original, repository-manifest and original-role references is not expressible as one existing foreign key. Tombstones are append-only and cannot be cleared by ordinary registration.

Artifact-payload leases accompany existing owner-local snapshot/index leases; all canonical artifact reads/copies run in owner threads. Parser children receive independent, hash-verified private staging copies and never open canonical artifact paths or databases. Owner death ends canonical readers, so no persistent CAS reader-lease table is needed; it does not imply that children die. Successor cleanup retains child staging until exit or explicit fencing/termination. Initial staging copies hold ordinary payload leases until copying actually exits. Existing jobs/steps/events retain backup/deletion receipts and recovery state; no separate backup database is required. Minimal keyed job receipts and their effective configuration/capture bindings are not subject to diagnostic-history expiration.

Owner management exposes `purge_originals`, canonical export and import through Python and typed maintenance requests under the existing `manage` CLI and `/v1/jobs` HTTP namespace. They are management capabilities, not part of a reader's MCP tools. `purge_originals` requires explicit revision IDs and `confirm_original_removal=true`; the default is a read-only removal plan. A plan reports its policy/reference cut and is revalidated at execution. The dashboard must show the plan's scope/outcomes before submitting that flag. This is part of the product operation, not an approval flow for editing these specifications.

Map `IDEMPOTENCY_CONFLICT` to HTTP 409. A permitted inspection of a removed original returns `ORIGINAL_REMOVED`/410; an unauthorized caller receives the normal scope/not-found response without disclosing tombstones or sharing. Missing required backup members return `INTEGRITY_ERROR`; unsupported schema/capability errors remain distinct. Restore activation emits a management receipt listing unavailable channels and interrupted jobs.

## 5. Failure-case acceptance

- Restore a canonical backup onto a clean machine without the source workspace, external pack directory or native vector/graph files. All untombstoned originals/extractions/registry resources open by verified hash; omitted channels are explicit and no provider is called. Restored batches remain unavailable until verification; altered reconstructed vector values fail old-batch verification and can only publish under a new family/snapshot.
- Delete/corrupt one required backup member: import fails before activation. Test a nested registry/repository manifest reference, not only direct SQL references; reject malicious paths/symlinks/duplicate entries.
- Publish/withdraw/purge during backup copying: the completed backup matches its recorded cut, and pinned payloads survive until every copier exits. Crash/cancel before completion cannot produce an importable success receipt. Include logically unreferenced registered present payloads; reconcile pending-but-already-unlinked payloads before the cut and suspend remaining deletion work after restore.
- Remove A's original where B or another retained role shares its SHA: A's original API returns `ORIGINAL_REMOVED`; B remains readable; the physical-retention reason is accurate.
- Crash the owner with a parser child alive: its private input remains independent of canonical artifact GC, and staging cleanup waits for the child to exit or be fenced/terminated.
- Release the last original binding while an original reader or backup holds a lease: logical removal is immediate, unlink is deferred, and physical success is not reported early.
- Crash before/after tombstone commit, unlink, directory fsync and final state update: recovery distinguishes pending deletion from corruption and never removes a newly referenced payload.
- Reingest identical bytes under a new authorized source: global payload availability returns, but the old binding tombstone remains effective. Backups taken after deletion preserve that distinction.
- Repeat an identical keyed write concurrently and after restart: exactly one job/capture/publication results. Change metadata, options, supplied bytes or an explicit content reference under the key: HTTP 409 with no new work.
- Modify a live path after its capture, then retry the same request/key: reuse the old committed capture. Submit a fresh key: capture the new bytes. Remove a committed capture: fail rather than silently rereading the path.
- Purge optional job/request diagnostics and replay the key: retain the binding and terminal metadata; never reuse the key as a fresh write. Restore copied in-flight jobs without automatically resuming external calls.

## 6. Backup sequence

```mermaid
sequenceDiagram
    participant C as Owner client
    participant O as Workspace owner
    participant S as SQLite
    participant A as Artifact store
    participant B as Backup destination
    C->>O: Export canonical backup
    O->>O: Pause metadata publication and deletion
    O->>S: Backup consistent database cut
    S-->>O: Copied database
    O->>A: Resolve typed closure and pin all required payloads
    O->>O: Resume mutations; keep payload leases
    O->>B: Copy database and verified pinned payloads
    O->>B: Verify closure, fsync, publish completion receipt
    O->>A: Release leases after all copy workers exit
    O-->>C: Backup receipt, capture epochs, rebuild requirements
```
