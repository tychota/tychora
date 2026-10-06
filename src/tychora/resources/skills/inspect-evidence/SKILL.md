---
name: inspect-evidence
description: Use when reading exact Tychora evidence, source lineage, original bytes, or derived records under partial or restricted inspection results.
---

# Inspect evidence

Confirm an existing authorized workspace and the caller's current scope. Discover the active kinds, source lineage and relevant filters before selecting an evidence ID; owner-opening `inspect` commands can create a missing workspace. Use `tychora --workspace WORKSPACE inspect evidence UNIT_ID --json` or public `kb.evidence.get(unit_id)` for a readable unit, and `kb.inspect(InspectRequest(...))` for a requested exact range. Use `kb.sources.revisions(source_id)`, `sources.extractions(source_id, revision_id)` and `sources.channels(source_id, revision_id, extraction_id)` to continue bounded lineage with returned cursors. The CLI also exposes `inspect sources SOURCE_ID --json` and `inspect revisions REVISION_ID --original --json`.

An `InspectionResult` with `evidence` contains the full authorized unit. With `partial_evidence` and `excerpts`, quote only the returned codepoint ranges; null `canonical_text_sha` and `canonical_text_length` do not authorize reconstructing the rest. Preserve unit, source, revision, extraction, locator and citation IDs exactly as returned. If the excerpt's literal range or citation locator was not supplied, describe it as supplied rather than inventing values.

A removed original returns `ORIGINAL_REMOVED`/HTTP 410. Retained derived evidence may still be readable: inspect its own provenance and `text_origin`/`citable` status, then label any quotation as derived. Do not cite it as the removed original or imply original bytes remain recoverable. Restricted or unknown asset IDs cannot be inferred from lineage counts or a coworker's recollection; keep inspection inside the effective grant. Report truncation, partial coverage and unavailable records explicitly instead of filling gaps with plausible text.
