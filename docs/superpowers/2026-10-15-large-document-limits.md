# Handoff: real-PDF ingestion limits (2026-10-15)

Audience: whoever continues `feat/dashboard`. Branch `fix/docling-formula-spans` sits on top of `feat/dashboard` and carries two fixes found while ingesting real PDFs into an online workspace (`voyage-context-4`, `rerank-2.5`, Chroma, `general-documents@1`, Docling parser). Review and merge them, then address the open items.

## Fixed on this branch

| Commit | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| `fix: accept unenriched Docling formula spans over original text` | 8-page PDF with one formula fails as `JOB_INTERRUPTED`, no reason given | Without formula enrichment Docling emits `text=""` with `orig` set and `charspan` over `orig`; `mapping.py` measured the span against the empty text and raised a plain `ValueError` | `document_text_domain` treats an empty-text item with non-empty `orig` as `docling_orig_text_item` with an empty projection; regression test in `tests/ingestion/test_docling_mapping.py` |
| `fix: authorize large-document text uploads in bounded batches` | 287-page PDF fails as `FORBIDDEN_SCOPE` although upload consent is granted | `_text_records` authorized the union of all groups' dependencies (11,373) in one call; the owner bounds one authorization at 2,048 | `upload_batches` splits ordered groups so each batch's dependency union is ≤ 2,048, renumbers packing `request_index` per batch, and authorizes each batch through the unchanged owner checks; one group over the bound fails as `INVALID_REQUEST`; tests in `tests/application/test_upload_batches.py` |

| `fix: keep capped lexical results complete under strict retrieval` | Every strict search on a real corpus fails with `INDEX_INCOMPLETE` | The lexical adapter reported its intentional 80-candidate truncation as an incomplete channel (review-assessment item 1) | Report the channel complete after scoring all eligible rows; truncation stays visible through `distinct_candidate_count`; the existing 81-match test now asserts this |

| `fix: skip empty excerpt ranges on textless picture units` + `fix: skip whitespace separators between table cell fragments` | After the lexical fix, searches on a corpus with tables failed with `INTEGRITY_ERROR: Document excerpt has no physical fragment` | `_physical_ranges` required an overlapping locator even for an empty range (picture units keep zero-width locators) or a range covering only the tab between two table-cell fragments | Empty and whitespace-only ranges outside every fragment yield no fragment; non-whitespace text outside every fragment still fails; tests in `tests/retrieval/test_segments.py` |

The formula text itself is still not searchable (empty projection). Making `orig` the unit text for formulas is a separate semantic decision.

## Still open

1. **Workspace snapshot manifest ceiling.** Publishing fails with `INTEGRITY_ERROR: Required manifest exceeds byte limit` once the snapshot manifest passes `MAX_MANIFEST_BYTES = 16 MiB` (`adapters/files/references.py`). The manifest is cumulative over the whole workspace (observed 2.7 → 9.7 → 15.9 → 23.6 MiB as documents were added), so splitting documents does not help: a workspace holds roughly 200 dense book pages in total. Snapshot manifests need sharding or incremental composition; this changes the durable format and integrity verification, so it needs a design.
2. **Oversized page images.** Scanned books can embed single images above `MAX_IMAGE_PIXELS = 40,000,000` (seen: 6973×9928 bilevel scans); `_check_pdf_image_capacity` rejects the whole document as `INVALID_REQUEST` with no page reference. Consider naming the page and offering a bounded downscale-on-decode path. Workaround used: downsample those images in a derived copy.
3. **Opaque failures.** Unexpected exceptions in source jobs become `JOB_INTERRUPTED` (retryable) with no diagnostic, and parser-process failures become a bare `INVALID_REQUEST`. The server logs nothing. Each of the issues above needed an in-process reproduction to diagnose. Map known parser/mapping failures to typed errors with a reason, and record a bounded diagnostic on the job.
4. **Transient provider failure.** One `PROVIDER_FAILURE` occurred on a small PDF and did not recur on retry; nothing was logged, so its cause is unknown.
5. **Scanned PDFs.** Image-only PDFs need the OCR intake (`/v1/pdf-intakes`, redesign Task 6); the ordinary file add parses them with `do_ocr=False` and indexes nothing useful.

6. **Query latency.** On a ~22k-unit workspace (online Voyage text + rerank), strict searches take 15–31 s, close to the 30 s local HTTP deadline (one query returned `DEADLINE_EXCEEDED`). Not yet profiled.
7. **Review of `d6882ea` (streamed PDF composition): approve with nits.** Legacy composed manifests replay byte-identically (cross-commit check), bounds are enforced on descriptors before reads, and no partial canonical state was found. Follow-ups: (a) medium — `application/discovery.py:822-827` replays and decodes every page image of the raw extraction on each paginated `source_selections` request (~300 MB I/O for a 382-page scan); cache per extraction or verify only the selected image, with cancellation; (b) low — `application/restore.py:446-456` catches only `ValueError`, so `LIMIT_EXCEEDED` escapes the config loop and the outer `INTEGRITY_ERROR` mapping; it also hardcodes `512*1024*1024` instead of `MAX_COMPOSITION_REPLAY_BYTES`; (c) low — new admission validation raises bare `ValueError`, surfacing as retryable `JOB_INTERRUPTED`; (d) nit — raw images are read/hashed up to three times and the extraction revalidated per admission call; (e) add a fixed-digest legacy identity test.

## Verification on this branch

- New tests pass. `tests/qualification/test_docling.py tests/ingestion tests/contracts`: 386 passed; the 2 `test_tables.py` failures also fail without these commits; 22 `test_docling_mapping.py` errors come from a fixture defined in another module.
- `tests/security tests/runtime tests/providers tests/indexing tests/api` (live/release excluded): the same 18 failures with and without the batching commit (vision/model-asset environment).
- Real runs: the formula PDF ingests and publishes; the 287-page PDF passes upload authorization and embedding, then hits open item 1.
