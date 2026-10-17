# Inspect returned passages and stored chunks

Date: 2026-10-14. Follow-up to owner feedback on the implemented dashboard: readable indexed segments/chunks are missing from Search and Sources. This is independent of the simultaneously investigated scanned-PDF intake path.

## Intent

A maintainer must be able to see what was extracted, what exact windows were stored for retrieval, and what the query actually returned. Preserve the chosen quiet, dense, warm workbench. Raw JSON remains optional diagnostics, not the primary reader.

## Three distinct views

Search defaults to **Returned passage**: exact returned excerpt blocks, their unit-local codepoint bounds, supplied locators, matched/context roles, omissions and completeness. A composed segment is not falsely rendered as a contiguous original span. Structured-only segments remain selectable.

**Stored chunks** resolves each actual indexed_window_id to the canonical retained chunk. Returned anchors may have been clipped to fit the query budget, so they cannot supply full chunk bounds. Show full stored chunk text/bounds, stored token count/fingerprint, and returned anchor bounds separately. Null window IDs are legitimate and never synthesized. Label these stored retrieval windows; do not claim current snapshot membership from row existence.

**Full unit** deliberately opens the existing evidence reader. Source revision coverage also offers source-ordered **Extracted units** and **Stored chunks**, paginated by extraction. Unit summaries lead to full evidence; exact chunk text is supplied by the server, never JavaScript slicing of Python codepoint offsets.

## Backend boundary

Add read-only lineage resources under `/v1/sources/{source}/revisions/{revision}/extractions/{extraction}/units` and `/windows`. Default page20, maximum50; response budget1MiB. Windows accept optional `chunk_id` and `unit_id`; units optionally accept unit_id. Opaque cursors bind lineage, filter, scope, limit and policy/snapshot context. Invalid/wrong-lineage cursors fail rather than silently restarting.

Reuse retained inspection authorization, canonical hydration, range ownership and delivery-time policy revalidation. Verify source/revision/extraction relationships before reporting data. Retained pins intentionally have no active slots; do not use their empty chunk_ids set as window authorization. Validate stored chunk identity and exact [start,end) before slicing hydrated text. Never regenerate chunks with the current tokenizer or invent a tokenizer ID that is not stored. Order units by canonical source ordinal; chunks by unit source ordinal, bounds and ID. No unsupported per-unit ordinal assumptions.

Fully readable units may be summarized. Hidden/denied child content, IDs, lengths, hashes and parent links must not leak. Range-readable chunks may be admitted independently of full-unit readability through existing ownership semantics. Cursor progress must cross denied rows without hiding later authorized results. Removed original bytes do not remove independently retained readable evidence. Surface scope-limited/incomplete states honestly.

No retrieval/chunking algorithm changes, new remote dependencies, live-provider use, full-corpus graph, or native qualification claims are part of this feature. Preserve running preview data and other worktrees. Real user document bytes/text/paths must not enter committed fixtures.

## Verification

Use real synthetic long-unit chunks, Unicode codepoints, clipped search output, parent/child restrictions, wrong lineage and scope-bound continuation. Frontend tests assert readable exact passages/chunks, explicit full-unit selection, text escaping and cancellation. Real browser journey checks source->revision->stored chunks and search->passage->chunk->unit at desktop and mobile. Keep fixtures synthetic and preserve existing ingestion/recovery tests.
