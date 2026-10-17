# Dashboard redesign: knowledge workflows and workspace administration

Date: 2026-10-14. Supersedes the original six-peer-page information architecture. The owner explicitly requested simple indexing and querying plus a real administration panel, and selected **one workspace with multiple collections**. The existing warm, quiet, dense visual direction remains the baseline.

## Product outcome

A new user can create a named collection, add a document, understand its processing status, and find an exact passage without learning revision IDs, JSON contracts or provider configuration. An operator can inspect and control the underlying ingestion, retrieval and workspace configuration from the same application. The interface describes observed state and performs real operations.

The scanned-PDF intake and exact-content inspection work remain required. This redesign incorporates their user interfaces; they are not separate future features or duplicate implementations.

## Benchmark synthesis

The supplied screenshots are interaction references, not feature-parity promises.

| Reference | Useful pattern | Application in Tychora |
| --- | --- | --- |
| Qdrant | Collection management and a distinct technical console | Named collections first; advanced query controls alongside normal search. [Official Web UI guide](https://qdrant.tech/documentation/web-ui/). |
| Neo4j | Results with alternate representations and selected-object inspection | Passage, stored chunks, full unit and relationships share a persistent selection. [Browser visual tour](https://neo4j.com/docs/browser/legacy/visual-tour/). |
| ArangoDB screenshots | Clear administration navigation, query controls and bounded graph exploration | Separate administration group; relationship exploration starts from an inspected object and exposes limits. |
| OpenAI storage screenshot | Searchable file list with a readable detail pane | Documents table, persistent selected document, status and details next to the inventory. |
| Voyage AI screenshot | Workspace/project context separated from administration | One workspace context and named collection selector; context stays visible across routes. |
| LiteLLM | Operational tables with filters, model setup and dedicated administration | Compact management tables, useful actions and clear model capability states. [Official admin UI guide](https://docs.litellm.ai/docs/proxy/ui). |
| RAG upload concept and RAGFlow screenshots | Prominent upload and collection entry points | An Add documents action, useful empty states and a direct path from indexing to search. |
| OpenSPG/KAG | Knowledge construction, model configuration and exploration as separate jobs | Separate ingestion, model administration and evidence exploration. [Project reference](https://github.com/OpenSPG/KAG). |

Choose a knowledge-first workbench with grouped administration. A console-first design would make the easy path unnecessarily technical; a separate administration application would split collection context and duplicate navigation. Keep one shell and progressively reveal depth.

## Information architecture

The sidebar has two labeled groups. **Knowledge:** Collections, Documents, Search, Explore. **Administration:** Jobs, Models, Evaluations, Activity, Settings. Workspace identity and connection status sit in the shell; a named collection selector scopes relevant pages. IDs are copyable secondary details, never the only names in navigation.

Collection scope supports one, several, or all granted active collections. Search and Documents use that selection. Ingestion always names exactly one destination; multiple selection never silently chooses the first. Workspace-wide forms visibly say **Entire workspace**, use the complete authorized admin context and retain the collection selection for returning to knowledge work. Jobs defaults to all jobs visible to that full grant, with an explicit collection filter where supported; jobs spanning collections are not falsely labeled single-collection work. Activity represents the exact selected query scope and explains that changing scope changes the available history. Restricted grants never expand when a collection is created.

- **Collections** is the starting page: names and truthful status/count summaries, with Create collection, Open documents and Search actions. Collection settings support the lifecycle actually implemented in the accompanying backend plan. No fake multi-workspace selector, team or billing destinations.
- **Documents** is a sortable/filterable inventory with an Add documents action. Show name/source key, collection, processing state, type and known size/time/counts. Unknown is not zero. Selecting a row opens a readable detail pane. Within it, Overview, Content, Chunks, Relationships and History distinguish purpose; JSON appears only under Technical details.
- **Search** opens with a query field, named collection scope and Search action. Filters and retrieval settings start collapsed. Results show readable snippets, source names and locator labels. The reader defaults to Returned passage, with deliberate Stored chunks and Full unit views. A structured request mode edits the real supported query contract; it is not an arbitrary SQL/Cypher executor or a generated-answer chat.
- **Explore** is bounded evidence/relationship exploration from a selected source or unit. Table/list inspection remains first-class and keyboard accessible. A graph displays only actual returned nodes/edges and explicit truncation; it cannot imply a global corpus graph. Empty and unavailable relationships are distinct states.
- **Jobs** shows queued/running/completed/failed/cancelled work with stage, source and elapsed time when reported. Selection opens stage history and recovery actions. Retry/cancel require supported current job state and durable backend results.
- **Models** groups extraction/OCR, embedding and reranking capabilities, showing active configuration, local availability and remote-upload consent separately. Configuration forms use qualified options and credential references; availability is not a successful provider test. Incompatible representation changes explain the blocker and preserve active generations.
- **Evaluations** provides existing evaluation-set/run workflows and readable metric/result tables. **Activity** gives the recorded operations and available history detail without implying unavailable trace or replay data.
- **Settings** groups workspace details, collection administration, processing defaults, access/data policies and supported maintenance operations. Every mutation maps to a tested authorized service. Destructive actions disclose their effect and require confirmation in the product.

Each operation has one canonical home: collection editors live in Collections, provider/model editors in Models, and Settings links to them. Explore is a deep-linked shared relationship inspector rather than a second relation implementation. Documents and Search reuse evidence and chunk readers. Inventory filters/sorting must apply to the server result set or be explicitly labeled as applying only to loaded rows; bounded/partial counts and continuation stay visible.

The initial inventory may report unit/window counts as unknown even for a small document when the existing authorization evaluator cannot establish an exact count within the listing budget. It must not scan whole extraction artifacts or introduce a second authorization implementation just to populate a summary. Content and Chunks remain the exact paginated inspection surfaces; an unknown inventory count does not mean no content was indexed.

## Required administration operations

The new collection/grant/lifecycle and guided settings/readiness contracts are required backend work. Existing maintenance services are reused.

| Home | Required operation | Authority and review | Observed outcome |
| --- | --- | --- | --- |
| Collections | Create, edit title/slug, archive, reactivate | Managed workspace owner; confirm archive effect | Stable UUID, updated named scope; archive retains evidence, excludes ordinary search and blocks new indexing |
| Collection settings | Read, grant, revoke registered-provider upload consent | Authorized owner; explicit collection/provider consequence | Persisted policy and refreshed effective grant, with configuration/consent kept distinct |
| Models | Inspect and provision required local assets | Owner; explicit asset set and local/download plan | Verified cache readiness and durable setup result; no silent download |
| Models | Draft, validate and activate guided settings | Entire workspace authority; review effective changes | Preserved credential references/profile semantics; active configuration or precise compatibility blocker |
| Document actions | Withdraw a source | Manage; confirm search/ingestion effect | Authoritative removal receipt; retained bytes are not claimed purged |
| Settings / Data | Plan and apply original-byte purge | Manage; review actual reference-cut plan | Removed/shared/pending byte receipt and remaining derived-evidence explanation |
| Settings / Operations | Run health check | Whole-workspace read, manage for repair | Measured report and specific findings |
| Settings / Operations | Recover compatible projections | Entire workspace manage; review selected channels | Recovery receipt; no claim of embedding/parser migration |
| Settings / Backup | Export backup to an explicit server path | Entire workspace manage; show destination | Verified backup receipt, not a fictional browser download |
| Settings / Backup | Restore into a clean destination | Entire workspace manage; review archive/destination | Verified separate restored workspace; current workspace is not silently switched |

The remote-provider setup journey must be operable through the same administration UI. For a fresh workspace before first indexing, Models exposes workspace online/offline policy, the qualified provider/model/reference, and its supported text/image index backend; Collections owns upload consent. These are distinct prerequisites. A documented synthetic smoke test verifies ingestion and retrieval/reranking through real dashboard/HTTP operations with a simulated provider transport, while live provider qualification remains explicitly separate. Existing embedding generations cannot be silently converted by changing a collection or model selector.

Credentials are references to owner-provisioned secrets. This redesign does not add a secret store, source move/rename semantics, arbitrary model migration or unsupported history/cache purge.

## Add and index documents

The primary action opens an inline workflow: **Choose → Review processing → Index**. Files and pasted text are the easy paths; registered local paths and detailed metadata are advanced options. Collection defaults to the current named collection, source name derives from the filename or first text heading and remains editable. A qualified default kind is shown with a human label; advanced kind/metadata editing remains available.

The simple path uses a real registered **Document** kind (`document@1`) with a required title, optional supported provenance metadata and the qualified structural-section normalizer. It creates no publisher, author or edition on the user's behalf. Existing specialized kinds, schemas and immutable identities remain unchanged; choosing one shows its actual required fields through the registered metadata schema. A collection may explicitly choose a different default kind.

PDFs stream to a private local intake, then every page is structurally analyzed. The review step shows size, page count, native/scanned/mixed classification, proposed local OCR and concrete blockers. Index starts only after the user confirms that analysis and destination. It creates the existing durable job and shows its progress. Browser disconnect does not cancel accepted ingestion. OCR output is visibly derived and review-required; it is not automatically promoted to verified source text.

Text can be indexed without touching upload state. Input switches clear irrelevant errors and invalidate stale reads. Failures stay attached to the attempted item and step, with a meaningful retry/change action. Never display an empty encoded placeholder size as the selected file's size.

First use checks the prerequisites for the chosen operation. If a parser, tokenizer, model asset, configured capability or consent is missing, Review processing shows the specific blocker and an authorized **Set up** action into the canonical Models/collection screen. Preserve the draft and unexpired intake in memory, return to the same step, and refresh authoritative readiness. Expired receipts or changed analysis/policy require renewed analysis and review. Installed dependency, verified assets, active configuration and upload permission are separate facts. No user must discover the raw TOML editor to complete the ordinary qualified local workflow.

## Content inspection

Preserve the exact-content inspection design's distinctions. Returned excerpts can be multiple disjoint blocks and have clipped query-anchor ranges. A stored chunk is resolved by its retained ID and displays its full server-provided text, bounds and stored token count. Full unit is explicit. Source content and chunks are paginated in source order. Unicode is rendered as supplied, not sliced using JavaScript offsets. Citations, locators, original bytes, source/revision identity, omissions and scope limits remain available.

Put useful text in the primary reading surface and machine identifiers in compact expandable metadata. Source type, OCR/review state, page or section, and exact-vs-context role are concise badges/labels with text equivalents. The document detail should answer what was indexed before presenting ingestion internals.

When relevant indexed OCR is excluded by the current review filter, Search shows that fact outside the collapsed advanced controls and offers an explicit supported way to include review-required content. This changes query filtering, never the evidence's review/citation state. Empty-result guidance distinguishes no matching passage from excluded OCR. Indexed-derived, included in this search and verified/citable are separate states.

## Visual and interaction direction

Keep forest green as the action/selection accent, with lighter warm neutral surfaces and stronger text contrast. Use a compact sidebar, a stable top context bar, consistent page title/action rows and well-spaced table toolbars. Main UI text must be readable at normal browser zoom; long IDs and JSON no longer dominate layout. Tables use subtle rules and aligned status/action columns; empty states offer the relevant next action. Reading panels have a comfortable measure and preserve whitespace. Avoid a decorative metric-card homepage.

Use shared field, button, status, table, tab, detail-pane and empty-state components. Loading, refresh, error, permission-denied, partial and empty states are visually and textually distinct. Selection is URL-addressable where identity is safe; credentials, queries and imported contents remain memory-only. Collection changes abort requests and clear incompatible selections. Desktop uses list/detail panes; narrow screens use an accessible list-to-detail transition with a back action and no document-width overflow.

## Delivery boundaries

The capability audit and implementation plan must identify backend work needed for real named-collection administration, source status summaries, current grant refresh and each mutation. Unsupported controls must not be rendered as working affordances. Preserve all existing dashboard operations while reorganizing them; retain compatible routes or redirects. Preserve the live preview's current corpus before changing its process and verify the restored source/job/chunk identities.

Accept against real HTTP journeys: create collection → add text → inspect chunks → search exact passage; large scanned PDF → analysis → local OCR job → inspect derived content; change collection → no stale content; manage models/settings with truthful constraints; recover a failed job; inspect bounded relationships and evaluation results. Include desktop/mobile and keyboard checks, independent code review, focused backend security tests and full frontend verification. No live remote provider calls or user-document content in committed fixtures.

Also require a clean-workspace setup-and-return journey, a multi-collection query with one explicit ingestion destination, archive/reactivate behavior, workspace health/backup while a collection is selected, consent/configuration changes with real readiness refresh, and scan → inspect → search with OCR both excluded and explicitly included. Test scope-specific empty Activity without claiming that no history exists elsewhere.
