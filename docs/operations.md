# Operations and retention

Start with scoped discovery and status:

```bash
tychora --workspace ./knowledge inspect sources --json
tychora --workspace ./knowledge inspect settings --json
tychora --workspace ./knowledge manage health
tychora --workspace ./knowledge inspect job JOB_ID --json
```

`add` and source sync return durable job handles. A job may be queued, running, succeeded, failed, cancelled, or interrupted. `manage sync SOURCE_ID` runs registered sources under management scope; source-specific `sources.sync` uses write scope. Retry is explicit and limited to qualifying transient or interrupted jobs, with current authorization and policy rechecked. A successful job has a publication receipt; a failed optional vision stage can retain valid base evidence with an incomplete diagnostic.

`manage export DESTINATION` captures a consistent owner-serialized cut of canonical SQLite, registry/config projections, and retained artifact payloads. `manage import ARCHIVE DESTINATION` restores into an empty destination without opening the lost source workspace. Restore checks digests and bindings before activation. Native vector/graph files are private rebuildable projections and are not a canonical backup; readiness is reverified after restore, and unavailable native channels remain unavailable until a qualified compatible projection is published. If a legacy vector family lacks complete capability proof, recovery reports `CAPABILITY_MISSING` before provider/native mutation. The supported repair is a fresh workspace and reingestion of available originals; an in-place native rebuild does not upgrade that family.

```bash
tychora --workspace ./knowledge manage export ./knowledge-backup
tychora manage import ./knowledge-backup ./knowledge-restored
tychora --workspace ./knowledge-restored manage health --verify-payloads
tychora --workspace ./knowledge-restored manage rebuild text graph
tychora --workspace ./knowledge-restored manage health --verify-native
```

`health --verify-native` is an explicit, bounded, read-only audit of **ready text/image vectors and graph generations in the active snapshot**. It rereads stored Chroma embedding bytes against retained record/value receipts and Ladybug nodes, assertions, and edges against the sealed canonical graph input. The response names `native_verified_snapshot_id`, `native_verified_records`, `native_verified_generations`, and `native_skipped_pending_batches`; pending projections are counted but not verified or repaired. It does not cover historical unmounted snapshots. Missing ready files or collections remain unavailable and produce `INTEGRITY_ERROR` without native recreation by health; canonical inspection, lexical search, and export remain available. A qualified explicit rebuild handles pending projections. The same `verify_native` flag is available in sync/async `management.run` and authenticated `POST /v1/jobs` health requests; it needs a whole-workspace read grant covering every collection, access floor, and current or retained allowed use. Restricted readers receive `FORBIDDEN_SCOPE` before the audit. Deletion resume additionally requires `manage`.

Removal has separate effects. `manage remove SOURCE_ID` withdraws a registered source from retrieval. For a withdrawn source, `manage purge-originals SOURCE_ID REVISION_ID` previews exact original removal; `--confirm` executes that plan. An original tombstone means original bytes cannot be read, but retained derived evidence may remain with its own revision and derivation provenance. Shared payloads needed elsewhere are retained. Backup copies made earlier are separate. Inspect the plan and relevant evidence before executing an irreversible purge.

`manage purge-cache NAMESPACE` removes named runtime cache entries, not query history or canonical source evidence. `manage purge-history BEFORE_ISO_TIMESTAMP` removes eligible metadata-only history, not cached data or source artifacts. Default history retains metadata for 30 days; replay needs retained request inputs and otherwise returns `REPLAY_UNAVAILABLE` (HTTP 409). Purging one surface does not imply purging another.

Provision each optional model or tokenizer asset explicitly. `--offline manage setup --asset output-tokenizer --bundle BUNDLE` imports a verified local bundle and never fetches a missing file. Documents, code, and visual paths need their own extras/assets. Provider configuration does not grant upload permission; `manage provider-upload-consent COLLECTION PROVIDER grant|revoke` changes owner and collection policy, and the default owner refreshes its scope on reopen. A narrower caller or token remains narrowed.
