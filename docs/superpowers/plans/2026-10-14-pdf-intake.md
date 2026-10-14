# PDF Intake and Local OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. The user has already selected that execution method. Steps use checkbox syntax for tracking. Read the spec first; do not ask for an execution method again.

**Goal:** Let the user choose, locally analyse, review and ingest a large scanned PDF through authenticated streamed intake and existing durable source jobs, retaining truthful OCR provenance and the current corpus.

**Architecture:** A private receipt owns streamed bytes and a local PDF analysis before submission. A trusted acquired input enters the existing source scheduler/capture path; an explicit captured PDF policy selects the qualified native or local OCR adapter without changing the workspace default profile or old generations. Three sequential deliverables cover intake/capture, bounded OCR/provenance, and the user workflow with real verification.

**Tech Stack:** Python 3.12, existing FastAPI/ASGI and SQLite/CAS services, pinned pypdf/PDFium/Docling/RapidOCR/ONNX Runtime, React/TypeScript/Vitest/Playwright. No new dependencies or live providers.

**Spec:** [PDF intake design](../specs/2026-10-14-pdf-intake-design.md)

## Global Constraints

- Upload bytes are capped at min(104857600, current configured max_file_bytes, current configured max_total_bytes); ordinary JSON remains capped at 1048576 bytes.
- PDF processing uses installed pinned dependencies and verified local assets only; no remote fallback, provider upload, model download, dependency installation, or provider activation.
- Every intake is bound to workspace, principal, effective grant, collection, canonical logical descriptor, original SHA-256, analysis identity, and resolved PDF processing policy.
- Original bytes and revision identity are immutable; OCR changes extraction identity and remains text_origin=ocr, citable=false, review_state=needs_review with source-pixel dependencies and derivation receipts.
- Existing native parsing behavior, workspace profile, configuration identity, and index generations remain intact; the PDF parser is explicitly selected per intake.
- Existing caps remain: 40 million pixels per image/canvas/mask, 100000 mapped/preflight items, 256 MiB parser output per child collection, 256 MiB aggregate parser metadata, 16 MiB retained extraction/composition manifest, 1 GiB default CAS object ceiling.
- The new processing aggregate budget is 1 GiB for original plus derived artifacts, narrowed only by an explicit lower owner processing budget; streaming counted enforcement precedes retention. Acquisition max_file_bytes/max_total_bytes bound input originals and sidecars, not derived OCR artifacts. This is a new aggregate quota, not the existing CAS per-object ceiling.
- Synthetic fixtures only; no user-document bytes, transcriptions, filename, or absolute path in committed docs, tests, fixtures, or logs.
- The current preview on port 8767 and its data remain untouched until an explicitly reviewed migration/deployment step.

## Review Focus

- A chosen large file or chunked overrun must stay within the binary ceiling without widening the 1 MiB JSON boundary; Task 1 tests both declared and actual byte counts.
- Stale, foreign, expired or changed-scope receipts must never disclose/open another original; Task 1 tests ownership, no-follow staging, cancellation, handoff recovery and concurrency reservations.
- A mixed PDF, blank scanned page or partial SDK success must not become native text or empty ready coverage; Task 2 tests per-page classification, from_ocr evidence and typed incomplete failures.
- Model/backend/scale changes, batched page references and replay/restore must not reuse old extraction identity or shift physical pages; Task 2 tests legacy identities, exact-input pixels and bounded merged exports.
- Needs-review OCR can be indexed yet excluded by query filters; Task 3 tests explicit review/filter messaging, origin inspection and no automatic acceptance.

## Interfaces and file responsibilities

New files: contracts/pdf_policy.py (immutable policy independent of requests); contracts/pdf_intake.py (bounded public DTOs); application/acquired_source.py (trusted acquisition with future/TYPE_CHECKING request annotations); application/pdf_intake.py (private receipts and orchestration); ingestion/pdf_analysis.py (structural PDF census); interfaces/http/pdf_intakes.py (HTTP handlers); adapters/docling/pdf_batches.py (bounded export/ref merge); adapters/docling/ocr_evidence.py (OCR-cell origin and exact-raster evidence). Avoid growing application/sources.py with HTTP or file-receive logic.

Existing interfaces to preserve: SourceJobScheduler.add_async, SourceService.accept_add/execute_accepted, Acquirer.capture/_manifest, JobRepository.accept/capture, DoclingParser._prepare/_retain, map_document, validate_document_units/assets, compose_document/replay_composed_document, planned_vision_proxy, ProxyFactory, SettingsService.activate, pinned_runtime_compatible and indexed_representation_identity. Ordinary FileSource root checks remain intact. New UploadSource is accepted only with a validated trusted acquisition or persisted job capture, never as an arbitrary CAS read through normal sources.add.

### Task 1: Streamed intake, local analysis and durable acquired capture

**Files:** create src/tychora/contracts/pdf_policy.py, src/tychora/contracts/pdf_intake.py, src/tychora/application/acquired_source.py, src/tychora/application/pdf_intake.py, src/tychora/ingestion/pdf_analysis.py, src/tychora/interfaces/http/pdf_intakes.py; modify contracts/requests.py, application/services.py, application/sources.py, application/jobs.py, ingestion/acquire.py, interfaces/http/auth.py and app.py. Create tests/transports/test_pdf_intake.py, tests/ingestion/test_pdf_analysis.py, tests/jobs/test_acquired_sources.py; extend tests/transports/test_http.py and tests/jobs/test_recovery.py.

**Consumes:** RequestScope, SourceInput/SourceBase, WorkspaceOwner.run_blocking, LocalAuth/checked_headers, ServiceRuntime.operation/admit/release, Registry metadata checks, LocalArtifacts.put_stream/pin_payloads, existing job repository and scheduler.

**Produces:** these exact proposed interfaces, all defined in this task:

```python
# contracts/pdf_policy.py: independent of requests.py
from typing import Literal
from tychora.contracts.base import Contract, FrozenJsonMap, Sha256
class PdfProcessingPolicy(Contract):
    version: Literal["pdf-processing@1"] = "pdf-processing@1"
    mode: Literal["native", "local_ocr"]
    native_pages: tuple[int, ...]
    ocr_pages: tuple[int, ...]
    blank_pages: tuple[int, ...]
    backend_identity: FrozenJsonMap
    runtime_inventory: FrozenJsonMap
    model_inventory_sha256: Sha256
    options: FrozenJsonMap
    limits: FrozenJsonMap
```

```python
# contracts/pdf_intake.py: public intake DTOs
from typing import Literal
from uuid import UUID
from pydantic import Field
from tychora.contracts.base import Contract, FrozenJsonMap, NonEmptyStr, Sha256
from tychora.contracts.pdf_policy import PdfProcessingPolicy
class PdfIntakeDescriptor(Contract):
    source_key: NonEmptyStr
    collection_id: UUID
    kind: NonEmptyStr
    metadata: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    expected_size_bytes: int = Field(gt=0, le=104857600)
    expected_sha256: Sha256 | None = None
    format: Literal["pdf"] = "pdf"

class PdfAnalysis(Contract):
    original_sha256: Sha256
    size_bytes: int
    page_count: int
    native_text_pages: tuple[int, ...]
    image_only_pages: tuple[int, ...]
    blank_pages: tuple[int, ...]
    unknown_pages: tuple[int, ...]
    requires_ocr: bool
    blockers: tuple[NonEmptyStr, ...]
    proposed_policy: PdfProcessingPolicy | None
    analysis_sha256: Sha256

class PdfIntakeSubmission(Contract):
    analysis_sha256: Sha256
    policy_sha256: Sha256
```

```python
# application/acquired_source.py (separate module)
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.pdf_policy import PdfProcessingPolicy
if TYPE_CHECKING:
    from tychora.contracts.requests import RequestScope, SourceInput

@dataclass(frozen=True)
class AcquiredSource:
    workspace_id: str
    scope: RequestScope
    source: SourceInput
    original_ref: ArtifactRef
    receipt_binding_sha256: str
    policy: PdfProcessingPolicy
```

requests.py imports PdfProcessingPolicy only from the independent pdf_policy.py module. Public intake DTOs may import policy and existing JobHandle but never requests.py. The AcquiredSource private module uses TYPE_CHECKING/future request annotations. Add a fresh-interpreter import smoke test for requests, pdf_policy, pdf_intake and services before the service edits; subprocess.run([sys.executable, "-c", "import tychora.contracts.requests; import tychora.contracts.pdf_policy; import tychora.contracts.pdf_intake; import tychora.application.services"], capture_output=True, check=True) must succeed.

Validate policy page tuples as sorted, unique, positive and disjoint; their union must cover the analysed document. Revalidate server-resolved runtime/model/options and policy digest at submit and replay.

PdfIntakeStatus adds intake_id, state, reserved byte ceiling, received bytes, expiry, optional PdfAnalysis, optional JobHandle, and sanitized error envelope; state uses the spec's finite enum. UploadSource extends SourceBase with type=upload, content_sha256 and size_bytes, never a data/path field. SourceBase gains optional pdf_processing; serializers explicitly omit this new field when absent so historical defaults/digests do not change.

PdfIntakeService(owner, services, *, max_retained_bytes=1073741824) binds the owner-only original-plus-derived processing quota; it is separate from input acquisition limits and cannot be raised by the caller. Its methods are async prepare(descriptor, scope, ctx)->PdfIntakeStatus, receive(intake_id, chunks: AsyncIterator[bytes], scope, ctx)->PdfIntakeStatus, analyse(intake_id, scope, ctx)->PdfIntakeStatus, inspect(intake_id, scope, ctx)->PdfIntakeStatus, submit(intake_id, submission, scope, key, ctx)->JobHandle, and abort(intake_id, scope, ctx)->PdfIntakeStatus. SourceJobScheduler.add_acquired_async(acquired: AcquiredSource, scope, key=None)->JobHandle passes the trusted binding into SourceService.accept_add and execute_accepted. Acquirer.capture_acquired(source, acquired, ctx)->CaptureManifest constructs the same manifest as capture. analyse_pdf(path: Path, limits: FrozenJsonMap)->FrozenJsonMap returns structural facts only; application service adds capability preflight and policy/analysis identities.

- [ ] **1. Add failing ASGI admission tests using actual create_app and a tiny generated PDF.** Reuse Counter from tests/transports/test_http.py and Tychora.open; add make_pdf(tmp_path, scanned: bool, blank: bool=False)->bytes inside test_pdf_intake.py using reportlab with synthetic text or a generated Pillow raster. Include a receive callable that raises if invoked: denied tokens/scopes/Host/Origin or declared oversize must be rejected before receive. Exercise configured ceiling=small synthetic value, exact cap, cap+1, no Content-Length, duplicate/conflicting lengths and declared-vs-actual mismatch. Ordinary JSON 1 MiB+1 must still fail.

```python
@pytest.mark.asyncio
async def test_binary_intake_preserves_json_gate(tmp_path):
    from httpx import ASGITransport, AsyncClient
    from tests.transports.test_http import Counter
    from tychora import Tychora
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = create_app(kb._workspace.owner, kb._workspace.services,
                         LocalAuth({"writer": kb.scope}, authority="127.0.0.1:8765"))
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1:8765",
                               headers={"Authorization": "Bearer writer"}) as client:
            rejected = await client.post("/v1/sources", content=b" " * (1048576 + 1))
            assert rejected.status_code == 400
            prepared = await client.post("/v1/pdf-intakes", json={
                "source_key": "synthetic.pdf", "collection_id": kb.scope.collection_ids.__iter__().__next__(),
                "kind": "manual@1", "expected_size_bytes": 2048,
                "metadata": {"title": "Synthetic", "publisher": "Example", "edition": "1"}})
            assert prepared.status_code == 201
            assert prepared.json()["state"] == "prepared"
            assert "path" not in prepared.json()
```

- [ ] **2. Run those tests and confirm the new route/service absence is the failure.** Run PYTHONPATH=src python -m pytest tests/transports/test_pdf_intake.py tests/transports/test_http.py::test_raw_gate_rejects_ambiguous_headers_and_streamed_oversize -q. Do not weaken existing boundary assertions to make a new route pass.
- [ ] **3. Implement receipt reservation, exact-route admission and private receive.** Add optional max_request_bytes to checked_authority_headers/checked_headers rather than temporarily mutating shared LocalAuth. RawBoundary authenticates first, selects the exact PUT content route's configured ceiling, and forwards a counted receive callable without accumulating/replaying bytes; all other paths use the old buffering/limit. Preserve the activation gate and no-store headers. PdfIntakeService validates owner/grant/collection/descriptor before opening a no-follow generated file, writes bounded chunks on the owner worker, fsyncs, hashes, atomically closes receiving->staged, and cleans up after cancellation/idle timeout/overrun. Reserve against four/principal, eight/owner, 512 MiB/owner and 30-minute TTL; reap exited workers before unlink. Use atomic private records and reconcile restart handoffs.

```python
# receive invariant; enforce the server-reserved ceiling, not a caller override
received += len(chunk)
if received > min(receipt.byte_ceiling, 104857600):
    raise TychoraError("LIMIT_EXCEEDED", "PDF upload exceeds its byte limit.")
# finalize only after EOF, actual-size/hash checks and fsync
if received != receipt.descriptor.expected_size_bytes:
    raise TychoraError("INVALID_REQUEST", "PDF upload length differs from the prepared input.")
```

- [ ] **4. Add classification and ownership failures, then implement analysis.** Test native/scanned/mixed/blank/encrypted/malformed synthetic PDFs, image strips and all-page census. Count text without returning it, classify unknown pages conservatively, enforce 1000-page/40M-pixel/100k-object limits and 60-second isolated analysis timeout. Model/parser/normalizer preflight returns blockers; a markdown-default owner receives an explicit proposed per-intake PDF policy, never a changed workspace file. Test foreign workspace/principal, narrower collection, expired receipt, token rotation with stable grant, changed grant, stale analysis and replaced staging inode. Reject before content open and never disclose numeric census from a foreign receipt.
- [ ] **5. Add acquired-capture/job retry tests before implementation.** Construct immutable AcquiredSource on the owner worker from the authorized receipt after CAS promotion. Compare capture original SHA and metadata with source intent; assert jobs.capture precedes parser invocation. Repeat submit and simulate crash after job acceptance/before response: the same descriptor/content/policy/key resolves one job. Changed content/policy/key binding fails IDEMPOTENCY_CONFLICT. Retry reconstructs UploadSource from capture without reading original bytes or calling base64. An ordinary POST /v1/sources with type=upload must fail FORBIDDEN_SCOPE before a CAS open. Scope/config changes during submit invalidate the receipt. Upload sync uses retained immutable original; a changed original requires a new receipt.

```python
assert capture.original_sha == acquired.original_ref.sha256
assert capture.source["pdf_processing"] == acquired.policy.model_dump(mode="json")
assert retried.id == accepted.id
assert persisted_job.captured_inputs == capture.manifest_ref
assert owner.config.config_sha == config_before
```

- [ ] **6. Implement only the shared acquired-input seam.** Add trusted optional acquired argument to SourceService.accept_add/execute_accepted, validate receipt binding on the owner, canonicalize metadata exactly as existing _canonical_source, and call capture_acquired only before a retained capture exists. Keep scheduler slot/cancellation/idempotency behavior. source_intent uses received content hash and policy rather than receipt paths; source registration has a stable UploadSource descriptor. Reuse _manifest and jobs.capture, retain original pin until capture ownership exists, and update retained retry/sync reconstruction. Do not call the index coordinator from intake service. Expose route status codes 201/200/202, typed errors from existing ERROR_HTTP_STATUS, and abort/inspect with original receipt authorization.
- [ ] **7. Verify the independently reviewable intake deliverable.** Run new intake/analysis/acquired tests plus transports HTTP/MCP, jobs recovery/idempotency and existing FileSource root regressions. Confirm absence of arbitrary CAS access, large body materialization and global profile changes. Commit only this task's implementation/tests; no user data or private receipt files.

### Task 2: Qualified bounded PDF OCR, origin evidence and replay

**Files:** create src/tychora/adapters/docling/pdf_batches.py and ocr_evidence.py; modify adapters/docling/parser.py, application/runtime.py and owner.py, contracts/documents.py, extraction.py and composition.py, ingestion/document_views.py, document_parts.py, document_records.py, documents.py, composition.py, application/sources.py, adapters/vision/local.py, adapters/files/references.py only for declared artifact closure; add tests/ingestion/test_pdf_batches.py, tests/ingestion/test_ocr_origin.py, tests/runtime/test_parser_bounds.py and tests/qualification/test_scanned_pdf.py. Extend tests/ingestion/test_docling_mapping.py, test_public_vision_selection.py, tests/api/test_public_vision.py, tests/qualification/test_docling.py, and tests/operations/test_restore.py and tests/security/test_restore.py; use tests/acceptance/parser_crash_probe.py for actual-exit assertions.

**Consumes:** Task 1 independent contracts.pdf_policy.PdfProcessingPolicy and application.acquired_source.AcquiredSource captured policy; existing DoclingParser, map_document, validate_document_units/assets, ExtractionArtifact, planned_vision_proxy, ProxyFactory, DocumentCompositionPlan, LocalOcrProvider and original CAS references.

**Produces:** merge_pdf_page_exports(exports: tuple[FrozenJsonMap,...])->FrozenJsonMap; collect_ocr_page_evidence(pages, original: ArtifactRef, policy: PdfProcessingPolicy)->tuple[OcrPageEvidence,...]; retain_ocr_page_observation(owner, region: RegionInput, evidence: OcrPageEvidence, ctx)->DerivedObservation; normalize_native_document_units(parsed)->tuple[DocumentUnit,...] excludes origin=ocr while retaining all parser units in ExtractionArtifact. OcrPageEvidence contains physical_page, exact model-input image ArtifactRef and pixel hash, source PDF SHA, rendered dimensions/transforms, cell text/boxes/scores/from_ocr, model/runtime/policy digest and transcript order. Define these types/functions in ocr_evidence.py and contracts/documents.py; factor LocalOcrProvider's existing receipt serializer into the shared receipt builder, rather than create a competing receipt schema.

A ParserExecutionLimits dataclass in application/runtime.py binds page_count, wall_seconds, rss_bytes, output_bytes and cancelled context. parser_process gains optional limits and progress callback; legacy calls retain behavior. _Convert.run(paths, report) emits bounded typed stage-output batches and page progress; the child receives no owner/database objects. Existing _Convert.__call__ remains the native compatibility entry. Final output collection uses the same no-follow descriptor validation; each batch is reaped/acknowledged before private cleanup.

- [ ] **1. Pin no-policy compatibility and explicit-policy identities before touching the adapter.** Record exact legacy parser fingerprint input and absent-policy source serialization. Tests must show native fixtures keep old original/extraction behavior; explicit PDF policy works with a markdown-default owner without changing config_sha, pinned_runtime_compatible or indexed_representation_identity. Changing OCR mode/backend/model hash/scale changes parser/extraction identity; native source revision remains bound to the same original. Changing metadata/policy between analysis/submit is rejected. Retry and restore reuse retained policy and manifests, not current ambient cache paths.

```python
assert source_intent(legacy_source) == old_intent
assert explicit_parsed.original_ref == native_parsed.original_ref
assert explicit_parsed.parser_fingerprint != native_parsed.parser_fingerprint
assert current.config_sha == original_config.config_sha
assert indexed_representation_identity(current) == indexed_representation_identity(original_config)
```

- [ ] **2. Add failing batched merge/origin tests with complete synthetic exports.** Reuse mapping_case from tests/ingestion/test_docling_mapping.py, create two batches with duplicate local #/texts/0 refs but distinct physical page keys, nested body/furniture groups, captions and table-cell provenance. merge_pdf_page_exports remaps every known self_ref/$ref according to array offsets while preserving physical page numbers and hierarchy order. Unknown reference domains, duplicate physical pages, disconnected hierarchy or missing cell evidence fail INTEGRITY_ERROR. Validate the merged map with map_document and validate_document_units/assets. Test a native/ocr mixed document and blank page: retained units cover complete SDK JSON, but only native units enter source text/caption/record normalization.

```python
from tests.ingestion.test_docling_mapping import mapping_case
export, original = mapping_case()
assert map_document(export, original).units[0].locators[0].physical_page == 2
# New merge fixture deliberately uses local refs twice, while pages remain distinct.
assert [u.locators[0].physical_page for u in merged.units] == [2, 20]
assert len({u.native_ref for u in merged.units}) == len(merged.units)
assert all(u.text_origin == "ocr" for u in ocr_proxy_bundle.units if u.unit_type == "asset_proxy")
assert all(not u.citable and u.review_state == "needs_review"
           for u in ocr_proxy_bundle.units if u.text_origin == "ocr")
```

- [ ] **3. Implement explicit PDF mode and exact-input provenance.** In _prepare, qualify the captured policy without changing owner.config; reject unsupported non-PDF policies and missing/corrupt runtime/assets. Native pass retains existing backend/options. Scanned passes select PyPdfiumDocumentBackend, pinned RapidOcrOptions FULL_PAGE, CPU two threads and telemetry/offline controls. Retain parsed pages before cleanup and from_ocr markers. Match retained input raster to actual OCR input pixel identity; if it differs, fail INTEGRITY_ERROR rather than attach the transcript to a lower-resolution display image. Keep full mapped parser JSON/units and sidecar origin bindings; omit OCR from native normalization. Compose deterministic physical-page pixel-owner units/assets before creating planned OCR proxies through the shared LocalOcrProvider receipt builder. Verify same-extraction dependencies, source hashes and crop transforms using existing proof code. Preserve explicit captions/native relationships and label OCR layout/reading order as derived.

```python
ocr = RapidOcrOptions(backend="onnxruntime", mode=OcrMode.FULL_PAGE,
                     det_model_path=str(det), rec_model_path=str(rec),
                     cls_model_path=str(cls), scale=3.0)
options = PdfPipelineOptions(artifacts_path=model_root, do_ocr=True, ocr_options=ocr,
                            generate_parsed_pages=True, enable_remote_services=False,
                            allow_external_plugins=False,
                            accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU,
                                                                   num_threads=2))
assert evidence.model_input_pixel_sha256 == region.region_pixel_sha256
assert evidence.original_sha256 == parsed.original_ref.sha256
proxy = planned_vision_proxy(region, observation, asset_native_ref=page_asset.native_ref)
```

- [ ] **4. Prove the representation/budget choice before full-book execution.** The private measured scale-3 sample raster is about 0.635 MiB; 382 identical pages would be about 242 MiB PNG or 323 MiB base64. These are estimates, not coverage measurements. Do not embed scale-3 base64 across the full merged export or enlarge caps. Implement scan-external-images@1 in the retained extraction envelope and image-binding sidecar: retain exact PNG as separate declared CAS artifacts, use urn:tychora:artifact:sha256:<digest> image identifiers in the explicit scan export and native_ref-to-ArtifactRef sidecar bindings, never fetchable paths/URLs, and keep embedded images valid for every legacy native export. Extend validate_document_assets/replay to resolve only manifest-declared exact image refs, validate hash/size/media/dimensions/geometry, and use authorized pinned payloads. The scan asset validator accepts external identity URIs only under this explicit version. A caller-supplied undeclared CAS reference fails. Existing composition/replay byte limits remain enforced; read and verify authorized image payloads in bounded units rather than eagerly materialize the entire original/derived closure. Stream bounded 16-page output batches through the supervisor collector, count bytes before retaining them, and keep each under the existing 256 MiB StagedOutput cap, with a new explicit 1 GiB aggregate original-plus-derived processing ceiling, narrowed only by the owner's max_retained_bytes; input acquisition max_file_bytes/max_total_bytes continue to bound originals/sidecars and are not repurposed as derived-output quotas; merged metadata stays under 256 MiB and retained extraction/composition under 16 MiB. Release batch intermediate rasters after verified transfer. Use a reused loaded pipeline within a bounded child, not one model load per page. Report committed/accepted batch page progress through the job event service, without document text.
- [ ] **5. Add actual-exit/time/partial-output tests, then enforce bounds.** A top-level synthetic parser callable that waits past a 0.1-second limit must be terminated and joined before staging disappears; cancellation must do the same. A fake SDK result with PARTIAL_SUCCESS, timeout errors, missing/duplicate pages or detected text with zero usable transcription fails before publication. Known blank pages produce explicit coverage diagnostics. A processed image-only page with no detected text retains its pixels and a no-text-detected diagnostic, not a fabricated blank label; all pages processed plus usable text elsewhere may publish as indexed with explicit gaps, never complete transcription. No usable text anywhere cannot become searchable-ready. Enforce 1000-page/object/pixel caps, 5400-second whole OCR limit, 120-second SDK batch timeout, 180-second batch wall watchdog, two CPU threads, batch=1/queue=2 and reviewed sampled RSS threshold. No RSS support -> CAPABILITY_MISSING. Reject output/manifest/aggregate-derived overruns with LIMIT_EXCEEDED. Preserve sanitized child errors and original capture for retry.

```python
assert converted.status == ConversionStatus.SUCCESS
assert not converted.errors and not converted.has_timeout_errors()
if actual_pages != expected_pages:
    raise TychoraError("INDEX_INCOMPLETE", "PDF processing did not cover every selected page.")
if required_text_page and not transcription.strip():
    raise TychoraError("INDEX_INCOMPLETE", "A scanned text page produced no usable OCR.")
```

- [ ] **6. Run synthetic regressions and qualified local-model tests.** Run new batching/origin/supervisor tests plus existing mapping/composition/public vision, root acquisition, configured-runtime, restore and Docling qualification. Model qualification is explicit via TYCHORA_TEST_DOCLING_MODELS/TYCHORA_MODEL_CACHE pointing to a verified local bundle; do not auto-download missing assets. Add a generated striped scan with synthetic small margin text and compare clean PDFium raster/physical page identity, native versus OCR origins and retained pixel receipts. Require complete SUCCESS and nonempty mapped text including tables, not document.texts alone. Replay merged/external-image manifest and compare extraction/proxy/dependency IDs. Check historical no-policy/native records still restore. Report real test outcomes and runtime limits; commit this independently reviewed adapter/provenance task.

### Task 3: Unified-dashboard PDF intake acceptance and preserved-corpus verification

**Ownership:** The forthcoming unified dashboard redesign plan owns the one final UI implementation for one workspace, multiple collections and full administration. This task supplies PDF DTO/client interfaces, acceptance tests and integration verification to that plan. It does not authorize a second temporary PDF/source-form implementation. Execute this task as the PDF integration milestone within that redesign.

**Files:** create dashboard/src/pages/sources/pdfIntake.ts and the single final PDF intake component at dashboard/src/pages/sources/PdfIntake.tsx unless the unified dashboard plan relocates that same component (the existing SourceForm.tsx is its current integration boundary); modify dashboard/src/api/contracts.ts, client.ts, pages/sources/Coverage.tsx, components/EvidenceReader.tsx, pages/Search.tsx and styles.css only as required. Add dashboard/src/test/pdf-intake.test.tsx; extend dashboard/src/test/operations.test.tsx, dashboard/e2e/dashboard.spec.ts, e2e/server.py and test_server.py, dashboard/scripts/generate_contracts.py; update docs/dashboard.md and committed dashboard resource bundle. No new browser/server dependency.

**Consumes:** Task 1 exact six HTTP routes and DTOs; Task 2 actual job progress, OCR text_origin/review/citation metadata and channel coverage. Uses existing ApiClient token/scope/abort handling and SubmissionIntent for stable same-intent idempotency.

**Produces:** The unified dashboard owns the final file-picker/analysis/review component taking session/client and selected destination; this integration provides uploadPdf(client: ApiClient, intakeId: string, file: File, signal: AbortSignal)->Promise<PdfIntakeStatus>; prepare/analyse/submit/abort wrappers with Task 1 DTOs. Existing client.request supports Blob/File BodyInit already: explicitly set Content-Type for binary and never serialize it. Ordinary text and registered file/folder/repository flows remain unchanged.

- [ ] **1. Write failing UI tests for the chosen-file lifecycle with synthetic File data.** Test prepared/uploading/staged/analysing/analysed/blocked/submitted/failed states, selected collection/kind/metadata, raw File body versus JSON, declared max-size check, preserved idempotency retry, and disabled submit until exact analysis+policy confirmation. Changing file or destination invalidates analysis/confirmation; unmount/session disconnect aborts outstanding uploads and clears local data. Old base64 helper is removed from PDF choosing; non-PDF existing behavior stays supported. Error text reflects typed server errors without invented capability claims.

```typescript
const body = requestCalls.find((call) => call.method === "PUT")?.body;
expect(body).toBe(file);
expect(requestCalls.some((call) => typeof call.body === "string" && call.body.includes("data:"))).toBe(false);
expect(screen.getByRole("button", { name: "Ingest PDF" })).toBeDisabled();
```

- [ ] **2. Implement accessible analysis and review UI.** Show size/page count, native/scanned/mixed/blank/unknown coverage, local OCR requirement, captured processing choice and capability blockers. Explain that the default profile remains intact and this PDF explicitly uses local Docling. Labels distinguish upload, analysis, confirmed ingestion, durable job and searchable projection. Use keyboard-accessible file control, cancel and explicit confirm; do not expose model paths or receipts as product instructions. Render job progress/typed failures through existing components. Keep review-required OCR indexed coverage visible, state whether current query filters include it, and never auto-accept or quietly loosen filters.
- [ ] **3. Add origin/search/citation tests and inspection UX.** Verify source coverage lists OCR units and processing provenance, needs_review status and non-citable distinction, pixel original/page location and exact matched excerpt/stored windows. A query excluding needs_review OCR explains zero visible OCR results rather than claiming the file has no text; an explicit user filter change may include it. Do not fabricate a native citation from OCR. Test empty text, model missing, unreadable scan, partial timeout, asset removal and stale scope responses. Preserve original/native inspection and prior search/source workflows.

```typescript
expect(screen.getByText("OCR transcript · Needs review")).toBeVisible();
expect(screen.getByText(/current review filters exclude/i)).toBeVisible();
expect(reviewAcceptCalls).toHaveLength(0);
```

- [ ] **4. Capture actual HTTP contracts and run real browser flow.** Extend generate_contracts.py with disposable generated native/scanned/mixed PDFs and receipt redaction; no user content or private IDs/paths. Extend the Playwright fixture server to use verified local OCR assets when qualification is explicitly enabled. Browser test chooses a generated PDF larger than the old 1 MiB JSON cap, uploads raw bytes, observes scanned census, confirms OCR plan, waits for actual job completion, reads OCR provenance/page geometry, checks review filter behavior and aborts a second upload. Keep route mocks absent and providers offline. Keep desktop/mobile screenshots and runtime measurements in ignored verification artifacts.
- [ ] **5. Run the reviewable local qualification sequence.** Run PYTHONPATH=src python -m pytest tests/transports/test_pdf_intake.py tests/ingestion/test_pdf_analysis.py tests/jobs/test_acquired_sources.py tests/ingestion/test_pdf_batches.py tests/ingestion/test_ocr_origin.py tests/runtime/test_parser_bounds.py tests/qualification/test_scanned_pdf.py -q in the qualified environment, then the relevant existing HTTP/jobs/native/vision/restore regressions. From dashboard run npm test, npm run typecheck, npm run build and focused npm run e2e; verify generated resources and notices. Run existing Ruff/type checks for modified Python and formatting checks for frontend. Report missing local qualification honestly; do not claim skipped tests pass.
- [ ] **6. Preserve the running corpus, then carry out the authorized local ingestion.** First review concrete changes and verify a backup using the existing maintenance service; record prior source/revision/extraction IDs, active snapshot/generation and counts without secrets/content. Do not replace the preview workspace or stop port 8767 during synthetic verification. Apply only necessary maintenance schema migration, if any, and prove prior indexed reads and counts persist before introducing the updated runtime. Run an isolated bounded sample of the user's supplied original with the exact committed policy, including exact scale-3 input artifacts, merged/external-image representation, parser output budget and OCR review semantics. Then ingest the full 382-page original as the durable local job already requested, with observable page progress, finite bounds and no uploads/providers. Verify original hash, page coverage, retained OCR receipts/origins, extraction/projection identities and search filter behavior. If a technical limit fails, preserve unsuccessful job/capture and explain actual coverage; do not report ready, ask for redundant permission or erase old data. Update docs/HANDOFF with only generic capability and actual verification coverage, never the book's identity/text/path. Commit the reviewed UI/docs/resource deliverable separately.

## Implementation review gates

Task 1 can be approved independently with analysis/capture and truthful blocked OCR capability. Task 2 must pass provenance, exact raster binding, external-image declared closure, batching and legacy replay before Task 3 enables OCR submission. A controller reviews each task using the preserved subagent workflow and reviews the final branch. The existing user's local ingestion authorization survives these technical gates.

No full-document OCR accuracy claim follows from the one-page feasibility result. Record actual bounds, test/qualification results and full-job page/projection coverage. Sampled memory enforcement, exact SDK raster identity and external-image manifest replay are the load-bearing review items; fail or stop implementation review if their proof is missing.
