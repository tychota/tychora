# Docling enrichment (sub-project 4)

Date: 2026-10-15. Status: design, awaiting review. Programme: `2026-10-15-tychora-v2-program-design.md` (called "the programme"). Flow review: `docs/superpowers/reviews/2026-10-15-ingestion-query-flow-review.md`.

Docling behaviour below was checked against the installed, pinned Docling 2.132.0 / docling-core 2.101.1 sources (the same versions `application/pdf_processing.py:58-65` qualifies) and the upstream pages `usage/enrichments`, `usage/heading_levels`, `usage/supported_formats` and `usage/model_catalog`.

## 1. Intent and success criteria

Use what Docling offers for ingestion, tiered as the product owner decided: cheap local enrichment always on, Claude Haiku 5.5 picture descriptions when online and consented, formula/code enrichment per preset, and heavy work in a background phase after the document is searchable. Text that Docling or the mapper reconstructs from the document's own content (tables merged across pages, reading order, de-hyphenation, list markers) is source text and citable. Only model-generated text (OCR, formula LaTeX, code enrichment, picture descriptions) is derived evidence, with its own origin, `needs_review` state and a locator back to the original region.

| # | Criterion | Measured by | Programme link |
| --- | --- | --- | --- |
| E1 | Core-phase ingestion p90 < 30 s per native PDF ≤ 100 pages, with the always-on tier enabled | Benchmark harness, `ingest` profile | §5.1 |
| E2 | Scanned documents: core phase ≤ 2.5 s per OCR page p90; background enrichment reported separately per item | Harness, scanned subset | §5.1 |
| E3 | Heading levels: ≥ 0.85 exact level agreement and ≥ 0.90 section-boundary F1 on the heading gold set; flat-level baseline recorded | `evaluation` heading suite | §5.3 |
| E4 | Retrieval quality with enrichment ≥ without, at every stage, on the evaluation set (figure and formula queries added) | Existing evaluation machinery | §5.3 |
| E5 | A 287-page scanned book with two over-capacity page images ingests; the two pages appear as typed gaps; no whole-document rejection | Synthetic fixture reproducing the case | §5.2 |
| E6 | Every parser failure reaches the job record with a typed reason and page number when one exists | Unit + integration tests | §5.4 |
| E7 | No remote upload without collection consent; every description call is one usage-ledger row | Consent tests, ledger assertions | §4 usage ledger |

## 2. Scope and non-goals

In scope: Docling pipeline options per tier and format; new pinned model assets; PPTX, XLSX and HTML formats (HTML with paragraph anchors); picture classification; Haiku picture descriptions; CodeFormulaV2 enrichment; whole-document heading hierarchy; annotation extraction (DOCX comments and tracked changes, PDF annotations) as discussion evidence; derived-text provenance; page-scoped image capacity; typed parser errors; benchmarks.

Non-goals: Docling chunking (no character offsets into canonical text; chunking v2 owns chunks); Docling's VLM full-page pipeline and local picture-description VLMs (slow, unverifiable provenance); `do_chart_extraction` (Granite Vision, unqualified); Docling's `PictureDescriptionApiOptions` (it would send pixels outside the consent gate and usage ledger); audio, video, EPUB, email; LibreOffice rendering of charts or DrawingML.

## 3. Current state

- **Two PDF paths.** Without an intake policy, `_Convert.__call__` builds one `PdfPipelineOptions` with `do_ocr=False`, `images_scale=1.0`, Heron layout pinned to `8f39ad3c`, accurate TableFormer, CPU, two threads (`adapters/docling/parser.py:350-383`). With a policy, `run_pdf_batches` converts in 16-page windows with `generate_parsed_pages=True` and a separate RapidOCR full-page converter for policy-selected image-only pages (`adapters/docling/pdf_batches.py:279-341,361`). Only PDF, DOCX and IMAGE are allowed (`parser.py:368`).
- **No enrichment.** No picture classification, description, code or formula enrichment, and no heading hierarchy. The parser fingerprint hard-codes `{"ocr": False, "image_scale": 1.0, ...}` (`parser.py:958`).
- **Flat headings.** Docling's layout model yields `SectionHeaderItem.level = 1` unless `HeadingHierarchyOptions.enabled`; `mapping.py:135-139` builds `heading_path` from those levels and treats `title` as level 1, so a title is popped by the first heading. The normalizer sets `boundary_key = unit.native_ref` (`ingestion/documents.py:99`), finding D5.
- **Empty formula/code text.** Unenriched formula/code items have `text == ""` and `orig` holding the PDF characters; `document_text_domain` maps them to `docling_orig_text_item` with an empty projection (`contracts/documents.py:266-281`). CodeFormulaV2 overwrites `item.text` with LaTeX or code while `orig` and `charspan` still address the PDF characters (Docling `code_formula_vlm_model.py:288-290`); fed to today's mapper, the domain check would compare `orig` spans against LaTeX length.
- **Derived text exists, but only for regions.** `TextOrigin = Literal["source","ocr","derived"]` (`contracts/evidence.py:21`). Asset proxies are non-citable, `needs_review` unless they are caption copies (`ingestion/assets.py:189-205`), and governed by `derived_proxy_policy` / `derived_proxy_review_states` (`profiles/documents/profile.toml:118-120`). OCR units are flagged by `text_origin="ocr"` (`parser.py:795-812`).
- **Vision adapter.** `AnthropicVisionProvider` sends one region synchronously with a strict JSON-schema envelope (literal transcription separate from interpretation) and sanitises failures (`adapters/vision/anthropic.py:26-61,111-169`); `dispatch_captured_vision` runs through `owner.run_captured_provider` with a policy epoch (`:193-228`). There is no batch mode and no ledger.
- **Whole-document image rejection.** `_check_pdf_image_capacity` walks every page and raises `INVALID_REQUEST` on the first image object above 40 MP (`parser.py:121-172`), before any page is converted; `_image_decode_guard` re-raises after conversion if any decode exceeded the limit (`parser.py:115-118`). A 287-page scanned book with two ~69 MP bilevel images was rejected whole.
- **Reasonless errors.** The child process sends only an error code; outside the bounded path it is always `INVALID_REQUEST` (`application/runtime.py:354-369`). Users see `INVALID_REQUEST` or `JOB_INTERRUPTED` with no page or cause.
- **Assets.** `model-inventory.json` pins Heron and TableFormer only; `resources/model-assets.json` repeats the same five files with URL, licence and notice. The two lists are maintained by hand.
- **Annotations dropped.** PDF annotations are reported as `NATIVE_ANNOTATIONS_UNAVAILABLE` (`parser.py:934-940`). Docling 2.132.0 already adds DOCX comments to the `NOTES` layer, linked to annotated items (`msword_backend.py:4224`), but `mapping.py` does not map them.
- **Hidden external tool.** Docling's DOCX backend renders DrawingML through LibreOffice whenever `soffice` is found on `PATH` or in `/Applications` (`backend/docx/drawingml/utils.py:198-210`). Output therefore depends on the machine.

## 4. Design

### 4.1 Tiers

| Tier | Runs in | Contents | Default |
| --- | --- | --- | --- |
| T0 core | Core phase (document becomes searchable) | Layout (Heron), accurate TableFormer with cell matching, RapidOCR full-page on image-only pages, picture classification, whole-document heading hierarchy, native chart data, HTML anchors, annotations | Always |
| T1 local heavy | Background phase | CodeFormulaV2 formula and code enrichment | Presets that opt in (the software and knowledge presets; the preset ontology is sub-project 5's) |
| T2 remote | Background phase | Haiku 5.5 picture descriptions | Online, `anthropic` among the collection's upload providers, and the preset does not disable it |

T0 changes the extraction and therefore the parser fingerprint. T1 and T2 never touch the core extraction. They publish an **enrichment set** through the engine core v2 background-phase contract (programme §4). Original unit identities never change.

### 4.2 Components

Each component lives in `adapters/docling/` unless stated otherwise.

| Unit | Single responsibility |
| --- | --- |
| `options.py` | Builds Docling options from a frozen `EnrichmentProfile`. It is the only place that constructs `PdfPipelineOptions` and format options, and it returns the canonical options JSON that enters the parser fingerprint. |
| `capacity.py` | Page-scoped image and raster preflight. Returns a `CapacityReport`; replaces `_check_pdf_image_capacity`. |
| `failures.py` | `ParserFailure` contract and the child-to-parent error channel. |
| `hierarchy.py` | Collects heading evidence per batch and assigns levels once per document. |
| `alignment.py` | Ordered text alignment of Docling items to DOM elements or OOXML paragraphs. HTML anchors and tracked changes both use it. |
| `annotations.py` | Extracts PDF annotations and DOCX comments and tracked changes into `AnnotationRecord`s. |
| `enrichment_runner.py` | Child-process T1 runner: re-renders formula and code regions and invokes `CodeFormulaVlmModel`. |
| `ingestion/enrichment.py` | Selects T1 and T2 candidates, applies caps, builds proxy records and the enrichment set. Pure. |
| `adapters/vision/anthropic.py` | Gains `submit_batch` / `collect_batch` that reuse the existing request renderer and `parse_response`. |

`parser.py` loses its standard PDF path. Every PDF goes through the intake policy and the batch path, so there is one PDF code path (programme: no dual paths). `_Convert.__call__` remains for the simple-pipeline formats and standalone images.

### 4.3 Pipeline configuration

`EnrichmentProfile` (frozen, canonical JSON, version `docling-enrichment@1`) records: tier switches, the classifier allow-list, description caps, the formula/code switch and the hierarchy options. Presets set it; collections may narrow but never widen T2.

**PDF, native pages (batch path):** `PdfPipelineOptions(do_ocr=False, do_table_structure=True, table_structure_options=TableStructureOptions(mode=ACCURATE, do_cell_matching=True), generate_parsed_pages=True, generate_page_images=True, generate_picture_images=True, images_scale=1.0, do_picture_classification=True, picture_classification_options=<pinned revision>, do_picture_description=False, do_code_enrichment=False, do_formula_enrichment=False, heading_hierarchy_options=HeadingHierarchyOptions(enabled=False), enable_remote_services=False, allow_external_plugins=False)`. The remaining keys stay as in `pdf_batches.py:279-301`.

**PDF, OCR pages:** the same options with the existing RapidOCR `FULL_PAGE` settings (`pdf_batches.py:312-341`).

**Why hierarchy is disabled inside batches.** Docling's style and numbering inference ranks headings relative to the other headings it sees. Running it per 16-page window would give level 2 in one window and level 3 in the next for the same font. `hierarchy.py` handles levels instead (section 4.6).

**Why `images_scale` stays 1.0.** The scale applies to page images and picture images alike. Raising it to 2.0 would quadruple retained page bytes and render cost for every page. Description and formula crops are re-rendered from the original at their own scale in the background (sections 4.8 and 4.9).

**DOCX, PPTX, XLSX, HTML (Docling `SimplePipeline`):** `ConvertPipelineOptions(do_picture_classification=True, do_picture_description=False, enable_remote_services=False, allow_external_plugins=False)`. Verified in Docling 2.132.0:

- `SimplePipeline` inherits `ConvertPipeline`, so picture classification and description apply to embedded images (`base_pipeline.py:204-247`; `prepare_element` uses the embedded image when there is no page).
- Code and formula enrichment, OCR and heading hierarchy exist only in `StandardPdfPipeline` (`standard_pdf_pipeline.py:658-690`) and do not apply.
- DOCX formulas are converted from OMML to LaTeX by the backend itself. DOCX code keeps its literal text. Headings come from Word styles, HTML `h1`–`h6` and PPTX slide titles.

Backend options:

| Format | Backend options | Notes |
| --- | --- | --- |
| DOCX | `MsWordBackendOptions(render_chart_images=False)` | LibreOffice auto-detection is neutralised in the child by a scoped replacement of `get_docx_to_pdf_converter` that returns `None`, the same technique as `_image_decode_guard`. A test asserts that no subprocess is spawned. |
| PPTX | `MsPowerpointBackendOptions(render_chart_images=False)` | Slide = page for locators (slide number). |
| XLSX | `MsExcelBackendOptions(parse_charts=True, render_chart_images=False, treat_singleton_as_text=False)` | Native charts carry their data as a table. |
| HTML | `HTMLBackendOptions(fetch_images=False, render_page=False, enable_remote_fetch=False, enable_local_fetch=False)` | No network and no headless browser. Anchors are recovered by `alignment.py`. |

**Native chart data.** DOCX, PPTX and XLSX charts become `PictureItem`s whose metadata holds the chart's own data table (`TabularChartMetaField`). `mapping.py` emits one `chart_data` unit per chart with the tab-separated table text. Its origin is `source`: the text is literal file content, not model output. It is citable and its locator is the picture's native reference.

**Standalone PNG/JPEG:** core phase runs the existing `LocalOcrProvider` over the whole image (one RapidOCR call), producing the existing `ocr` proxy (`needs_review`). Picture classification runs on the image. This replaces the current "image yields no text" outcome (`parser.py:791-794`) without inventing source text.

### 4.4 Pinned model assets

New `model-assets.json` selections:

| Selection | Repository | Files pinned | Use |
| --- | --- | --- | --- |
| `documents-figures` | `docling-project/DocumentFigureClassifier-v2.5` | `config.json`, `preprocessor_config.json`, `model.safetensors` | T0 |
| `documents-code-formula` | `docling-project/CodeFormulaV2` | Every file the Transformers `AutoModelForImageTextToText` loader opens for the pinned commit: config, generation config, weights, tokenizer and processor files, chat template | T1 |

Rules:

1. Docling's classifier preset says `revision="main"` (`stage_model_specs.py:1084-1092`). `options.py` overwrites `model_spec.revision` with the pinned commit SHA, as it already does for Heron (`pdf_batches.py:277-278`). The same applies to CodeFormulaV2's `VlmModelSpec`.
2. Each file records `id`, `selection`, `path`, `sha256`, `size_bytes`, `url` (a `resolve/<commit>` URL), `license`, `owner: docling==2.132.0`, `revision` and `notice_url`. Each selection gets a notice entry (`application/setup.py:200` requires one).
3. Licences are copied from each repository's model card at the pinned commit. A selection whose licence is not Apache-2.0, MIT or CDLA-Permissive-2.0 is not shipped, and the spec is reopened.
4. `model-inventory.json` stops being hand-written. It is generated from the `documents*` selections of `model-assets.json` by a build step, and a test asserts the two are equal. This removes one duplicated source of truth.
5. Verification remains bounded at four selections per call (`setup.py:233`). `manage setup documents` installs `documents` and `documents-figures`. `manage setup documents-code-formula` is separate because its weights are about 0.6 GB.
6. Offline smoke tests run with `HF_HUB_OFFLINE=1` and the network disabled:
   - the classifier labels a synthetic bar chart as one of the chart classes and a synthetic logo as `logo` or `icon`;
   - CodeFormulaV2 turns a rendered `E = mc^2` crop into LaTeX containing `mc^2`, and a rendered Python snippet into code with language `Python`.

   These run in the qualified (asset-provisioned) suite, not in thin CI.
7. When a T1 selection is missing, the preset's T1 tier records a `CAPABILITY_MISSING` enrichment gap. The document still publishes; ingestion never downloads.

### 4.5 Derived-text provenance

**Source text versus derived text.** The test is where the characters come from, not whether a model helped place them.

- **Source text** is text whose characters come from the document itself, even when Docling or the mapper reorders, joins or cleans them. It has `text_origin = "source"`, is citable, and starts `unreviewed` like any other source unit. This covers:
  - reading order assigned by the layout model;
  - tables whose structure TableFormer recognises, including a table continued across pages and merged into one table unit; cell text comes from the page's text cells through cell matching;
  - de-hyphenation of words split across lines, and joining of lines and paragraphs split by columns or page breaks;
  - list markers and enumeration recovered from the text or the layout;
  - heading levels assigned by the hierarchy step (section 4.6), which change structure and never text;
  - DOCX formulas converted from OMML to LaTeX by the backend, a deterministic rewrite of the file's own formula markup;
  - native chart data and annotations (sections 4.3 and 4.11).

  A reconstructed unit keeps a locator to every region it was built from: a merged table carries one provenance entry per page, so a citation can show each page. Reconstruction never adds characters that are not in the document, apart from separators (spaces, line breaks, cell delimiters) and the removal of a hyphen at a line break.
- **Derived text** is text that a model generates: OCR output, formula LaTeX and code text from CodeFormulaV2, and picture descriptions. It is never citable as the document's words. It enters search only as an asset proxy with `needs_review`, as below.

Reconstruction keeps the origin of its inputs. A table rebuilt from OCR cells on an image-only page is `ocr`, not `source`; a merged table that mixes native and OCR pages is split at the page where the origin changes, so each unit has one origin.

Every enrichment output becomes an asset proxy (`ProxyRecord`, `ingestion/assets.py`). That gives it an owner unit, a derivation artifact, a non-citable unit and the review machinery for free.

| Output | `text_origin` | `proxy_kind` / `unit_type` | Citable | Initial review state | Citation target |
| --- | --- | --- | --- | --- | --- |
| OCR page text (existing) | `ocr` | `ocr` | no | `needs_review` | page region |
| Formula LaTeX | `derived` | `formula_latex` | no | `needs_review` | formula region asset |
| Code text and language | `derived` | `code_text` | no | `needs_review` | code region asset |
| Picture description | `derived` | `description` (existing kind) | no | `needs_review` | picture asset |
| Picture class | none (attribute) | asset attribute `picture_class`, `picture_class_confidence` | n/a | n/a | n/a |
| Reconstructed body text and tables (reading order, merged tables, de-hyphenation, list markers, OMML formulas) | `source` | existing unit types | yes | `unreviewed` | every contributing page region |
| Native chart data | `source` | unit `chart_data` | yes | `unreviewed` | picture native reference |
| Annotation (section 4.11) | `source` | unit `discussion` | yes | `needs_review` | annotation locator |

**Formula and code regions become assets.** `DocumentAsset.asset_type` gains `"region"`. Its pixels are the exact crop given to CodeFormulaV2: page rendered at Docling's `images_scale = 1.67`, bounding box expanded by the model's `expansion_factor = 0.18`. The asset's `ImageLocator` holds the physical page and the original bounding box. The derivation artifact records:

- model selection and revision;
- the region pixel SHA-256;
- the raw model output;
- the parsed code language.

Searches see the LaTeX or code. Citations always point at the original region.

**Core mapping of formula and code items stays as on 2026-10-15.** In the core extraction these items still have `text == ""`, an `orig`-domain locator and an empty projection (`contracts/documents.py:279`). The core phase never runs CodeFormulaV2, so the core item's `text` never changes. The enrichment runner reads the retained Docling JSON. It does not re-map it.

`document_text_domain` gains a guard: an item from the PDF or image pipeline where `text != orig` and the label is `formula` or `code` raises `ValueError("Enriched text must arrive as a derived proxy")`. This keeps model-generated text from ever entering a source unit. DOCX formulas are outside the guard: their LaTeX is the backend's conversion of the file's own OMML and is source text.

**Search behaviour.** Derived proxies follow the mode's `derived_proxy_policy` and `derived_proxy_review_states`, the same filter that governs OCR today. With the core pack defaults (`permitted`, states include `needs_review`), enrichment text is searchable in every preset. Results carry `origin: derived` and a review badge. A mode configured with `accepted_only`, such as the documented workspace override that replaces the removed `verified` pack (kind ontology §4.1), excludes it until reviewed.

Proxy text is a member of the derived proxy's own context group. It is never merged into a neighbouring source chunk, which matches the existing `text_origin != "source"` exclusions in `retrieval/segments.py:350,411,504`.

### 4.6 Heading hierarchy and section boundaries

1. **Per batch.** `hierarchy.py` records for each `SectionHeaderItem`, and for each `list_item` that could be promoted:
   - the native reference after merge;
   - the page and bounding box;
   - its text;
   - a reduced `SegmentedPdfPage` holding only the text-line cells that overlap the heading's bounding box.

   Memory is bounded by heading count, not page count. The cap is 20,000 headings per document; beyond it the document gets a `HEADING_HIERARCHY_CAPACITY` diagnostic and keeps flat levels.
2. **Outline.** The PDF outline is read once with `ThreadedDoclingParseDocumentBackend.get_document_outline` before the first batch.
3. **Assignment.** After the last batch, the child builds a heading-only `DoclingDocument` and calls the public `HeadingHierarchyModel.assign_heading_levels(document, parsed_pages, outline)` (`heading_hierarchy_model.py:553`) with these options:
   - `use_bookmarks`, `use_numbering`, `use_style` and `use_font_style` all on;
   - `max_level = 6`;
   - `numbering_schemes` from the preset, null by default.
4. **Output.** The child emits a final `heading-levels.json` batch: `{native_ref → level}` plus promoted references. `merge_pdf_page_exports` applies it before `map_document`. The record is retained and its digest enters the parser fingerprint.
5. **Mapping fixes.** `title` becomes level 0: it is the root of `heading_path` and is never popped. Furniture (`page_header`, `page_footer`) is excluded from `heading_path`.
6. **`boundary_key` for Docling documents**, in engine core's `"<scheme>:<native_ref>"` format (engine core §4.10):
   - a body unit takes `section:` plus the native reference of its deepest enclosing heading;
   - a table takes `table:` plus its own reference;
   - a unit before the first heading takes `section:#root`;
   - furniture takes `section:#furniture`.

   Units also carry `section_path`, the ordered ancestors as engine core's `{key, level, depth}` entries, where `depth` is the assigned heading level and `level` a named level when the preset's heading patterns or `numbering_schemes` identify one (article, annex, clause). Chunking v2 cuts within engine core's derived `chunk_section_key` and groups by `context_key`, both computed from `section_path`; it never cuts across a chunking section. The non-PDF formats use the backend's native levels and the same rule.

**Gold set.** Thirty public or synthetic PDFs: regulations with legal numbering, books with bookmarks, papers without bookmarks, scans. Each heading is annotated with its level. Metrics:

- exact level agreement;
- section-boundary F1, where a boundary is a change of `boundary_key` between consecutive body units, compared with gold;
- the share of headings whose level came from no signal.

E3 thresholds apply. If they fail, the hierarchy still ships for bookmark-bearing PDFs only (bookmarks are authoritative), and style inference stays off until it passes.

### 4.7 Large documents and capacity

`capacity.py` replaces the whole-document preflight with a per-page `CapacityReport`. For each page it lists:

- image objects, with their pixel size, bits per component and component count;
- page size;
- the OCR raster size: page points × 4.5, as in `pdf_batches.py:344-360`.

Decisions are per page:

| Condition | Action |
| --- | --- |
| Every image object ≤ 40 MP | Normal. |
| An image object > 40 MP, and its decoded size `ceil(w·h·bpc·components/8)` ≤ 64 MiB (a bilevel 69 MP image is about 8.6 MiB) | **Downscale-on-render.** Pillow never decodes the object. The page is converted by PDFium rendering at the page's normal scale, so the raster is bounded by page size. The child's existing RSS limit (4 GiB, `PDF_LIMITS.rss_bytes`) is the backstop. The page gets diagnostic `PAGE_IMAGE_DOWNSCALED` with the original pixel size. |
| An image object exceeds both limits, or the OCR raster > 40 MP | **Skip the page** and record gap `PAGE_OVER_CAPACITY {page, reason}`. The policy moves the page to a new `skipped_pages` partition (`PdfProcessingPolicy` version `pdf-processing@2`). Coverage reports it. |
| Every content page skipped | Existing `INDEX_INCOMPLETE "PDF contains no usable transcription"`, now with a typed reason. |

`_image_decode_guard` becomes item-scoped for the simple pipeline:

- a JPEG over 40 MP is decoded with Pillow `draft()` at the smallest power-of-two reduction that fits under 40 MP;
- any other format over the limit is not decoded, and the picture keeps its locator with gap `IMAGE_OVER_CAPACITY`.

The guard no longer fails the conversion after the fact (`parser.py:115-118`).

**Memory and batching** keep the 16-page windows, the stage batch size 1 and queue size 2. The T1 runner processes regions in pages of 32, releases rendered pages after each, and shares the 4 GiB RSS limit. The 1,000-page and 256 MiB metadata limits are unchanged.

### 4.8 Typed parser failures

`ParserFailure` is a contract with:

- `code`: one of the existing registered codes;
- `reason`: a dotted reason registered in engine core's error-reason registry (engine core §4.13) under the `parser.` prefix — `parser.pdf_malformed`, `parser.pdf_structure_over_capacity`, `parser.page_over_capacity`, `parser.page_coverage_mismatch`, `parser.conversion_timeout`, `parser.unsupported_content`, `parser.sdk_failure` or `parser.output_over_capacity`; a missing model asset uses engine core's existing `CAPABILITY_MISSING` reason `model_asset.absent`;
- `page`: an integer or null;
- `stage`: `preflight`, `layout`, `table`, `ocr`, `hierarchy`, `assembly` or `enrichment`.

Enums only: no free text, paths or document content. The child sends this record instead of a bare code (`application/runtime.py:354-369`), on every path, so the unbounded `INVALID_REQUEST` fallback disappears.

`validate_conversion` reports the first missing page. Docling `ConversionResult.errors` map to `parser.sdk_failure` with the error's page when Docling gives one. The job record stores the failure. `TychoraError` gains an optional `detail` mapping, rendered by CLI and HTTP as `INVALID_REQUEST: parser.page_over_capacity (page 141, preflight)`. Page-scoped gaps (`PAGE_OVER_CAPACITY`, `IMAGE_OVER_CAPACITY` and the other gap codes below) are coverage records, not errors. A page-scoped gap never fails a job.

### 4.9 Background T1: formula and code

**Selection.** Items labelled `formula` or `code` in the core extraction, PDF and image only, with a provenance bounding box of at least 8 × 8 points. Capped at 2,000 items per document; any further items get a `CODE_FORMULA_CAP_REACHED` gap.

**Execution.** One child process per document:

1. Re-render each region from the retained original with PDFium at scale 1.67 and expansion 0.18, under the 40 MP guard.
2. Build `ItemAndImageEnrichmentElement(item, image)` over the deserialised retained `DoclingDocument`.
3. Call `CodeFormulaVlmModel` with `extract_code` and `extract_formulas` set from the profile, batches of five as upstream.

Constructing elements directly bypasses Docling's `prepare_element`, which needs a live conversion. The crop arithmetic is copied from `BaseItemAndImageEnrichmentModel.prepare_element` (Docling `base_model.py:184-215`), and a test compares both on a fixture page.

**Output and publication.** Region asset, derivation artifact and `formula_latex` / `code_text` proxy. Empty or whitespace model output gives gap `CODE_FORMULA_EMPTY` and no proxy. The enrichment set publishes once the whole document is processed: one publication, not one per item.

### 4.10 Background T2: picture descriptions

**Selection rules**, applied in this order:

1. The picture has `picture_class` in the allow-list:
   - charts: `bar_chart`, `stacked_bar_chart`, `line_chart`, `pie_chart`, `scatter_plot`, `scatter_chart`, `box_plot`, `heatmap`, `other_chart`;
   - diagrams and drawings: `flow_chart`, `engineering_drawing`, `electrical_diagram`, `cad_drawing`, `chemistry_structure`, `chemistry_molecular_structure`, `chemistry_markush_structure`;
   - maps: `geographical_map`, `topographical_map`, `map`;
   - screenshots: `screenshot_from_computer`, `screenshot_from_manual`, `screenshot`;
   - `table` (a table drawn as an image).

   The top class's confidence must be ≥ 0.5. Logos, icons, signatures, stamps, codes, thumbnails, full-page images (OCR covers them), photographs and `other` are skipped and recorded as `skipped_class`. There is no images preset (kind ontology §4.8). Instead, for a standalone image ingested as `visual@1` or a narrower kind, `photograph` and `natural_image` are added to the allow-list, because there the picture is the document.
2. Native chart pictures that already have `chart_data` are skipped.
3. Size: on a paginated page, area ≥ 2 % of the page area (Docling's own default of 5 % misses small diagrams); otherwise ≥ 128 × 128 source pixels.
4. Duplicates by region pixel SHA-256 are described once. The existing vision cache key already deduplicates identical requests across documents.
5. Cap: 200 per document, ordered by class group (charts, then diagrams, maps, screenshots, tables), then by area descending. The excess gets gap `DESCRIPTION_CAP_REACHED`.

**Crops.**

- PDF: re-rendered from the original with PDFium at scale 2.0 (144 dpi) under the 40 MP guard.
- Other formats: the embedded image is decoded under the item-scoped guard.

Then the crop goes through model defaults' `fit_vision_region` (model defaults §4.5: long edge at most 2,576 px and at most 4,784 visual tokens, PNG, base64 under 10 MB). Its transform id is recorded in `region_pixel_transform`.

**Prompt and schema.** `AnthropicVisionProvider` is reused unchanged: same envelope, `literal_transcription` separate from `interpretation`, the same `parse_response`. The task is `describe_figure` and the task schema is:

```json
{"type":"object","additionalProperties":false,
 "required":["figure_kind","summary","entities","data"],
 "properties":{
  "figure_kind":{"enum":["chart","diagram","map","screenshot","drawing","table_image","other"]},
  "summary":{"type":"string","minLength":1,"maxLength":1200},
  "entities":{"type":"array","maxItems":50,"items":{"type":"string","minLength":1,"maxLength":200}},
  "data":{"type":"array","maxItems":100,"items":{"type":"object","additionalProperties":false,
    "required":["label","value"],"properties":{"label":{"type":"string"},"value":{"type":"string"},"unit":{"type":["string","null"]}}}}}}
```

`candidate_context` holds the caption unit interval and the heading path text (`CandidateContext`, exact intervals). Proxy text is `summary`, `entities` joined, and `literal_transcription`. `data` stays in the derivation artifact and is shown in inspection.

**Batch API.** Background descriptions dispatch through model defaults' `VisionDispatcher` (model defaults §4.5): a background job with at least 20 selected regions uses the `batch` implementation, which calls the Anthropic Message Batches API through `submit_batch(regions) -> batch_id` and `collect_batch(batch_id) -> results`; a smaller job uses `interactive`. Request bodies are rendered by the same code as `interpret`. The batch identifier is checkpointed in the background job step, so polling survives restarts: every 60 s, give up after 24 h. Synchronous `interpret` also serves interactive re-description of a single picture. The dispatcher is chosen once per job by that rule; a job never switches mid-way.

**Retries.**

- A result that is errored, expired or a transient `PROVIDER_FAILURE` is resubmitted once in the next batch.
- An invalid structured response is resubmitted once.
- After that the picture gets gap `DESCRIPTION_FAILED {reason}`.

**Cost caps and ledger.** Caps are counted in images, because counts are provider-neutral and deterministic; the dashboard converts them to estimated cost with the versioned price table (programme §4). Defaults:

- 200 images per document;
- 2,000 images per collection per rolling 30 days.

Each submitted request writes one usage-ledger row: provider `anthropic`, the model, operation `describe_image`, `batch` set when it went through the Batches API, units (images, input and output tokens from the result), latency, outcome, collection and job reference. Rows are written on collection; a cancelled batch writes rows for whatever the provider reports as billed. A cap that is reached stops selection and records `DESCRIPTION_BUDGET_REACHED`.

**Consent.** Authorisation is captured per region with the policy epoch (`CapturedRegionAuthorization`) and checked at three points:

1. selection;
2. submission;
3. collection.

If the epoch changed and `anthropic` is no longer in the collection's upload providers, the runner cancels the batch, discards results unread, records `CONSENT_REVOKED`, and publishes nothing from that batch. Descriptions published before revocation remain, labelled with their derivation time. Offline mode skips T2 entirely and records nothing.

### 4.11 HTML anchors and annotations (discussion evidence)

**HTML anchors.** In the child, after conversion, `alignment.py` parses the retained HTML with the same BeautifulSoup parser Docling uses. For each block element it records the nearest anchor:

- its own `id`;
- otherwise the closest preceding `<a name>` or `id` within the same block;
- otherwise the nearest ancestor's `id`.

It then aligns Docling text items to DOM blocks in document order by whitespace-normalised text equality, with a greedy forward scan that never moves backwards. The anchor is applied only after alignment succeeds:

- `DocumentLocator` gains `fragment: str | None`. Matched items get `fragment = anchor`, so citations render `source_url#anchor`.
- `native_ref` remains the Docling reference.
- Unmatched items get no fragment, and the document gets an `HTML_ANCHOR_UNALIGNED` count diagnostic.

Docling-generated identifiers (`docling_auto_input_*`) are never used as anchors.

**DOCX comments.** These are mapped from the `NOTES` layer, using the comment's `FineRef` targets that Docling already sets. Each becomes a `discussion` unit with:

- text: the comment body without Docling's `[author…, time…]` prefix;
- attributes: `annotation_type` (`comment`), `author` and `created_at` parsed from that prefix;
- locator: the comment's native reference;
- a `reviews` relationship assertion to every target unit (the `reviews` relation already exists in `profiles/documents/relations/`).

**DOCX tracked changes.** `annotations.py` reads `w:ins` and `w:del` runs with `w:author` and `w:date` from the retained DOCX XML (defused parser, as in Docling's own safe parser). It aligns each change's paragraph to Docling items through `alignment.py`. Each change becomes a `discussion` unit:

- `annotation_type` is `insertion` or `deletion`;
- text is the inserted or deleted text;
- it carries a `reviews` assertion to the aligned unit.

**PDF annotations.** In the child, pypdf reads `/Annots` of subtypes `Text`, `FreeText`, `Highlight`, `Underline`, `StrikeOut` and `Squiggly`, taking `/Contents`, `/T`, `/M`, `/Rect` and `/QuadPoints`, within the existing pypdf limits. The annotated range is the set of units whose provenance boxes intersect the quad points (or the rectangle) on that page. Character intervals are clipped where text coordinates exist. Each annotation becomes a `discussion` unit with a page and geometry locator and a `reviews` assertion. This replaces `NATIVE_ANNOTATIONS_UNAVAILABLE`. Annotations without content and highlights without targets are counted, not emitted.

**Rules for all discussion units:**

- `text_origin = "source"` (literal file content) and citable;
- `review_state = "needs_review"`;
- `boundary_key = "section:#annotations"`, so they never merge into body chunks;
- `author` is excluded from lexical and embedding text by default and shown in inspection only.

The lane is resolved by the kind ontology (sub-project 5); default: the `feedback` lane.

### 4.12 Performance budget

| Stage | Budget (benchmark machine, CPU, 2 threads) |
| --- | --- |
| T0 native PDF ≤ 100 pages, including classification and hierarchy | p90 < 30 s per document, of which classification ≤ 0.1 s per picture and hierarchy ≤ 1 s per document |
| T0 OCR page | ≤ 2.5 s p90 |
| Simple-pipeline formats | ≤ 5 s p90 per document under 10 MB |
| T1 | ≤ 4 s per region p90, reported per document; never on the core path |
| T2 | Wall time not budgeted (batch); reported as time-to-description p50/p90 |

The harness records per stage: wall time, peak RSS, pictures classified, regions enriched, descriptions requested, cost estimate. Two configurations are run: T0 only, and T0+T1. A T0 regression above 10 % against the previous commit fails the benchmark gate.

### 4.13 Configuration and security summary

All options are fingerprinted and retained with each extraction or enrichment set. No arbitrary upstream arguments are exposed. Remote services, plugins, network fetch, headless browsers and LibreOffice are disabled in every child. Model loading is offline-only from pinned assets. T2 is the only remote path, and it is behind the existing consent gate.

## 5. Migration and compatibility

New format, then rebuild (programme §2). The parser fingerprint changes, as do the `pdf-processing@2` policy, `DocumentAsset.asset_type` (adds `region`), `DocumentLocator.fragment` and the boundary keys. `manage rebuild` re-extracts from retained originals; no reading of the old format. Enrichment sets are rebuilt by the background phase after the core rebuild. T2 re-runs only where consent exists and within caps, and descriptions with an identical vision cache key are reused, so a rebuild does not upload a picture twice. The standard PDF path and the legacy `text_origin` serializer shim (`contracts/documents.py:128-133`) are deleted.

## 6. Testing and verification

- **Unit:**
  - `options.py` snapshot of the canonical options per format and tier;
  - `capacity.py` decisions table, including the bilevel 69 MP case;
  - `ParserFailure` serialisation;
  - the `document_text_domain` guard, including a DOCX OMML formula that passes as source;
  - origin of reconstructed text: a table continued across two native pages is one `source` unit with two page locators; the same table with one OCR page is split into a `source` unit and an `ocr` proxy; de-hyphenated text maps back to the original character spans;
  - heading assignment across two batches with equal fonts (same level);
  - `alignment.py` (reordered, duplicated and missing text);
  - selection rules and caps;
  - consent-epoch transitions at each of the three checkpoints;
  - batch result parsing through the existing `parse_response`.
- **Integration (real local assets, qualified suite):**
  - synthetic PDF with a chart, a logo, a formula and a code block: classification, a T1 proxy with a region asset, and citation to the original box;
  - a synthetic 287-page scan with two 69 MP bilevel pages: E5;
  - DOCX with comments and tracked changes, PDF with highlights and notes: discussion units and `reviews` assertions;
  - HTML with paragraph anchors: fragments;
  - PPTX and XLSX with native charts: `chart_data` units;
  - no subprocess spawned with `soffice` on `PATH`.
- **Provider:** a mocked Batches API covering expiry, partial errors, cancellation on revocation and ledger rows. One opt-in live test, never in CI.
- **Evaluation:** heading gold set (E3); retrieval evaluation with figure, formula and annotation queries, run with and without each tier (E4).
- **Benchmarks:** E1 and E2 on the harness, recorded per commit.

## 7. Code-health ratchet

- One PDF path: the standard path in `parser.py:350-383` and its fingerprint branch are deleted.
- `model-inventory.json` is generated, not duplicated.
- `parser.py` (1,100 lines) loses its capacity code (to `capacity.py`) and the inline option construction duplicated with `pdf_batches.py` (to `options.py`). No function in the touched modules exceeds the baseline function-size limit; `run_pdf_batches` (380 lines today) is split into options, per-window conversion and staging.
- `INVALID_REQUEST` without a reason disappears from parser paths.
- New modules import downward only: adapters depend on contracts and ports, never on `application`.
- Every new module has unit tests, plus mutation testing on `capacity.py` and selection rules.

## 8. Risks and open questions

1. **Direct `CodeFormulaVlmModel` invocation relies on an internal element type.** Default: pin Docling and keep the crop-equivalence test. On failure, fall back to a page-range conversion with enrichment on, matching items by page, label and bounding box equality.
2. **Licences of the two new repositories are unverified.** Default: block shipping a selection whose licence is not in the allowed set.
3. **Descriptions after consent revocation remain searchable** (section 4.10). The product owner may prefer hiding them. Default: keep them, with a dashboard filter by derivation date.
4. **Discussion-unit lane and the author attribute** depend on sub-project 5 and on privacy expectations. Default: feedback lane, author never embedded.
5. **The heading gold set may fail E3 for style-only PDFs.** Default: ship bookmarks and numbering, keep style off.
6. **Batch latency** (up to 24 h) may surprise users. Default: the dashboard shows "description pending" from enrichment status (sub-project 6).

## 9. Flow-review findings

- **D5 (Docling side).** Closed for Docling-normalized documents by section boundary keys (section 4.6). The contract is engine core's; the generic chunking side belongs to chunking v2.
- **D2 and D6.** Deferred to engine core v2. The flow review's "Item 3" says richer extraction makes them worse. This design adds proxies and region assets, so delivery order (programme §3: 1 before 4) is a precondition.
- **C5 (never-populated trace).** Not addressed here; engine core.
- **Not tracked in the flow review, but closed here:** the 40 MP whole-document rejection and reasonless parser errors (2026-10-15 real-PDF probes).
