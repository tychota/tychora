# Improving document ingestion

This page distinguishes implemented Tychora behavior from upstream capabilities worth evaluating. Upstream references were checked on 2026-10-13. These are proposed improvements, not configuration switches already exposed by Tychora.

## Current pipeline

The Docling adapter accepts PDF, DOCX and supported images, retains structured JSON and source geometry, extracts tables, and generates page/picture assets. It uses a pinned local layout model, CPU execution and explicitly provisioned model artifacts. Remote Docling services and external plugins are disabled.

The standard Docling pipeline sets `do_ocr=False`. Separate selected-region OCR and optional Anthropic vision exist, but a scanned document does not automatically receive a complete Docling OCR pass. Optional Anthropic region interpretation is also different from converting entire pages with Docling's VLM pipeline.

## Highest-value experiments

| Improvement | Why it matters | Work needed before shipping |
| --- | --- | --- |
| OCR as an explicit extraction policy | Recover text from scans and image-only regions | Language/engine selection, pinned assets, confidence and exact provenance; scan gold set |
| Heading hierarchy recovery | Better sections, context groups and useful neighbor boundaries | Enable the upstream hierarchy stage, preserve its provenance, test mixed numbering/bookmarks/styles |
| Table extraction controls | Improve difficult cell alignment and choose accuracy/runtime trade-offs | Compare FAST/ACCURATE and cell matching on a labeled table set; preserve fragments and row locators |
| Layout and resource controls | Improve reading order and predictable throughput | Versioned options for layout rules, render resolution, accelerators and limits; memory/time measurements |

Docling's [advanced options](https://docling-project.github.io/docling/usage/advanced_options/) expose model prefetching, table behavior, image scaling and execution limits. Tychora should expose a small validated subset per profile, include it in extraction fingerprints, and retain the effective settings with each extraction. Simply passing arbitrary upstream arguments would weaken reproducibility.

The [heading hierarchy stage](https://docling-project.github.io/docling/usage/heading_levels/) can use document structure and layout cues. It requires `HeadingHierarchyOptions(enabled=True)` and parsed-page information. Its choices can change parentage and chunk context, so compare section boundaries and citation association before enabling it by default.

## Enrichments and full-page vision

[Docling enrichments](https://docling-project.github.io/docling/usage/enrichments/) offer code/formula and picture processing. These can improve formula retrieval, diagram descriptions and figure search. Store generated text as derived evidence with model, prompt/configuration and source dependencies; preserve the original crop as the citation target. Additional enrichments add model downloads, latency and memory, so activate only what the corpus needs.

The [VLM pipeline](https://docling-project.github.io/docling/usage/vision_models/) is a separate conversion path worth testing for layouts where standard extraction fails. Evaluate retained source geometry, hallucinated text, reading order and cost. A locally run model needs asset and hardware qualification; a remote model also needs the existing upload-consent boundary. Do not route private source bytes through a new provider implicitly.

The [model catalog](https://docling-project.github.io/docling/usage/model_catalog/) is useful for selecting task-specific candidates. Tychora's asset manifest should pin model identity, bytes, license and expected runtime rather than selecting whichever upstream alias is newest at execution time.

## Audio and video

[Docling audio/video processing](https://docling-project.github.io/docling/usage/processing_audio_media/) makes transcripts and media-derived structures possible, but Tychora does not currently support those source formats. Useful evidence would need time ranges, transcript alignment, frame identities and source-media retention. Speaker identity, ASR uncertainty and frame sampling also need explicit policies.

Treat this as a separate adapter/provenance extension. A generic document-kind definition alone cannot add media decoding, ASR assets or timestamp citations.

## Acceptance criteria

For each experiment, keep the original immutable and compare the same labeled source set. Measure text accuracy, heading boundaries, table-cell/row reconstruction, figure association, citation validity, retrieval recall, wall time and peak memory. A better-looking Markdown export is insufficient if locators or retrieval quality regress. See [evaluation](evaluation.md) and [architecture](architecture.md).
