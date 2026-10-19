# Reviewed dead-code allow-list for vulture (gate G12). One name per line; the comment
# gives the reason and, for code a later sub-project calls or deletes, its owner.
# The number of entries may only fall; `python -m tools.health check` enforces it.

__getattr__  # public API: lazy attribute export of the tychora package (src/tychora/__init__.py:33)
_.context_spans  # LanguageAdapter port method implemented by the Python and TypeScript adapters
_.readinto  # file-object protocol method called by the standard library HTTP response reader
_.makefile  # socket protocol method called by http.client
UrlAcquisition  # URL acquisition adapter; engine core v2 wires it into source registration
_.hydrate  # CorpusStore port method; engine core v2 repository layer decides its callers
_.purge  # retention purge path (flow review C5); owned by engine core v2
_.purge_results  # query cache purge (flow review C5); owned by engine core v2
_.expire_diagnostics  # retention purge path (flow review C5); owned by engine core v2
_.raise_access_floor  # collection policy write exercised by tests; engine core v2 owns it
_.record_assertion_decision  # assertion decision write exercised by tests; engine core v2 owns it
_.is_pinned  # lease query exercised by tests; engine core v2 snapshot leases own it
_.migrate  # maintenance migration entry point; engine core v2 (new workspace format) owns it
_.reconcile  # recovery entry point exercised by tests; engine core v2 recovery owns it
_.prune_native  # explicit native index garbage collection; engine core v2 recovery owns it
_.rebuild_native  # native index rebuild; engine core v2 `manage rebuild` owns it
create_reranker  # reranker factory exercised by tests; model defaults (sub-project 3) owns provider construction
_.traverse  # public client method kb.relations.traverse (sync, async and HTTP clients)
_.discover_kinds  # registry query exercised by tests; kind ontology (sub-project 5) owns it
_.hint_contributions  # registry query exercised by tests; kind ontology (sub-project 5) owns it
import_inclusive_end  # date bound helper exercised by tests; kind ontology facets (sub-project 5) own it
DateInterval  # contract type exercised by tests; kind ontology facets (sub-project 5) own it
repository_manifest  # code repository manifest exercised by tests; chunking v2 code ingestion owns it
process_memory  # benchmark helper exercised by tests; engine core v2 benchmark harness owns it
recall  # evaluation metric exercised by tests; kept beside the runner's typed metrics
_.binding_count  # evaluation provenance counter read by tests
graph_value_sha  # graph receipt digest exercised by tests; engine core v2 incremental graph writes own it
proxy_context_allowed  # visual proxy policy exercised by tests; Docling enrichment (sub-project 4) owns it
validate_document_source_parts  # document part validation exercised by tests; Docling enrichment owns it
reconstruct_rows  # table reconstruction exercised by tests; Docling enrichment (sub-project 4) owns it
selective_interpret  # vision selection exercised by tests; Docling enrichment (sub-project 4) owns it
_.should_exit  # uvicorn server flag set to stop the MCP HTTP server
Acquisition  # port protocol, implemented structurally by the acquisition adapters
UploadPolicy  # port protocol, implemented structurally by CanonicalUploadPolicy
LanguageAdapter  # port protocol, implemented by the Python and TypeScript code adapters
Reranker  # port protocol, implemented by the Voyage reranker
