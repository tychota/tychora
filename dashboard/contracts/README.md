# Dashboard HTTP examples

These JSON responses were captured through the real FastAPI boundary using a temporary workspace and one synthetic pasted-text source. They contain no bearer credentials or local filesystem paths. Identifiers and timestamps are capture-specific, but references remain consistent across these files. This is a response-shape fixture, not a retrieval-quality benchmark; evaluation labels use the public identity of the synthetic evidence.

Regenerate from the repository root with `PYTHONPATH=src python dashboard/scripts/generate_contracts.py`. The script requires the project HTTP test dependencies (`httpx`) and accepts `--output` for a temporary alternative destination. It makes no external provider calls and uses a declared synthetic output counter. Production Python hosting does not require Node; frontend builds remain a separate developer operation.

| Examples                                                | Actual shape and limits                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `handshake.json`                                        | Workspace/instance identifiers, full granted scope and permitted default collection ID. No collection display names or global workspace list.                                                                                                                                                                                                                                                                                                     |
| `discovery-kinds.json`, `sources.json`                  | Discovery envelope: subject, definitions, counts, capabilities, capability_status and next_cursor. Definition entries have id/version/label/definition/schema_digest. This bounded kinds page may have a continuation. Source label is its source key; its revision preview is not a current/latest marker. Source counts can be absent.                                                                                                          |
| `source-revisions.json`                                 | Source ID, revision entries with retained stages and bounded extraction/channel previews, next_cursor and scope_limited. No percent-complete coverage, revision dates, or current revision flag.                                                                                                                                                                                                                                                  |
| `source-add.json`, `job-status.json`, `job-events.json` | Acceptance is a JobHandle. Persisted status has snapshot/config/capture references, state and latest_sequence; it lacks current progress counters and created/updated timestamps. Event-page response is a bare event list, with actual stage/state/counters and sequence. Counters need not provide a completed/total pair.                                                                                                                      |
| `query.json`                                            | Canonical query envelope has segments, citations, visuals, relations, coverage, coverage_gaps, decision_requirements, checks, warnings, budget_contract and trace. Segments carry exact source/revision/extraction IDs and matched/context unit distinctions, signals, anchors and citations. No invented ranking score. Trace is null despite requesting it.                                                                                     |
| `evidence.json`, `original.json`                        | InspectionResult envelope: evidence/partial_evidence/excerpts/records/original_ref/payload/media_type/truncated. The retained text original has URL-safe base64 payload, not a raw content URL. Null fields remain null.                                                                                                                                                                                                                          |
| `query-history.json`, `query-run.json`                  | Bounded discovery listing and inspection records. request_retained is false: no stored question/result/trace, and replay must remain unavailable. No aggregate activity analytics.                                                                                                                                                                                                                                                                |
| `evaluation-dataset.json`, `evaluation-receipt.json`    | Registered dataset carries cases; execution returns a JobHandle after completing the current synchronous service call, despite HTTP 202. Fixture hardware reports offline=false; context is synthetic and has no external providers.                                                                                                                                                                                                              |
| `evaluation-list.json`, `evaluation-detail.json`        | List is a bare list of saved run summaries. Detail contains evaluation_id/dataset_id/snapshot_id/job_id/epochs/config_sha/hardware/report/cases. Cases is a dictionary keyed by case_id; each value has case_id/runs/schema_version. Report cases are separate per-case metric summaries. MetricSet is `{metrics: [...]}`; MetricValue includes name/value/sample_count/denominator/not_applicable_reason. No paging or label-revision endpoints. |

All data endpoints remain bearer-authenticated and scope-authorized. Example responses are for DTO and browser tests, never substitute live workspace data.

`extraction-units.json` and `extraction-windows.json` are captured through the
real retained-content HTTP routes. Units include only fully readable summaries;
windows contain exact server-sliced stored ranges and projected locators. Stored
window presence does not assert active snapshot membership. Tokenizer identity is
not stored and is omitted. Pages default to 20 entries (maximum 50), with a 1 MiB
response budget and opaque continuation. Cursors expire when the inspection
service restarts, the policy/scope/request binding changes, or the bounded 1024
cursor cache evicts them; expired cursors return `INVALID_REQUEST`. A bounded scan
may return an empty, scope-limited page with continuation past denied rows.

`metadata-document-schema.json` and `metadata-manual-schema.json`
are actual `GET /v1/discovery/kinds/{kind_id}/metadata-schema` responses. They
include fields, constraints, required flags and the retained schema closure.
The general Document kind requires only title; its overall advanced flag also
reflects optional nested metadata and does not make title-only ingestion invalid.
Specialized Manual requires title, publisher and edition. No field values are
generated by the form. The capture script regenerates both shapes.

The `ocr-*` captures come from the real authenticated HTTP opt-in browser
journey over `generated_scan("columns")`: actual pinned local OCR, registered
exclude/include modes, successful visual/LabelledProxy query, retained windows
and full evidence. Content is synthetic; OCR remains needs_review/noncitable.
No token, private path, user document or live provider is captured. The ignored
qualified journey also records a restricted-scope HTTP403 and actual windows.

The `pdf-*-status.json` captures full actual intake-status route envelopes for generated native/scanned/mixed/blank PDFs using `generate_contracts.py --pdf`. `pdf-{analysed,accepted,events,excluded,included,evidence,windows,units,coverage,diagnostics}.json` comes from the qualified generated 2,749,832-byte PDF browser journey's actual authenticated HTTP capture, exported with `--pdf-evidence`. Analysis is nested in the status envelope, submission acceptance is a direct JobHandle, and events are a returned event page. Private receipt identifiers and volatile expiry are redacted; policy/analysis hashes remain opaque server values. OCR is noncitable/needs_review and no-text-detected is detector-relative. These are synthetic local integration evidence, not accuracy or full-document capacity qualification.
