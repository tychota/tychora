# Performance qualification and limits

The four cache states are `cold_owner`, `warm_resources`, `warm_query_embedding`, and `warm_result`. The benchmark keeps one immutable corpus, configuration, query, and output cap across all states. Its synthetic real-engine fixture starts the cold owner in a separate subprocess and records its PID/start identity, then uses a single warm owner. It traverses QueryService, canonical SQLite, lexical index, Chroma, and the Voyage request/response adapter with a synthetic recording provider transport. The generic `client_factory` interface cannot independently verify a caller's process isolation; only the fixture's explicit child identity does so. An owner close/open in one process is not process-cold.

Recorded stage times, nearest-rank p50/p95, throughput, provider/cache/eligibility calls, process-owned Python traced peak, RSS high-water, and index bytes come from executed operations. One synthetic sample on macOS arm64, 2026-10-05, ran `tests.performance.engine_probe.run_real_benchmark` with one query per state and a 203,716-byte Chroma index. Cold owner: child PID 14695, process start `Mon Oct 5 23:24:02 2026`, startup 2,030 ms, query 487 ms, one provider call/two retrieval calls/two policy revalidations, Python traced peak 7.9 MB, RSS high-water 149 MB. Warm resources: startup 1,408 ms, query 474 ms, one provider/two retrieval/two policy. Warm query embedding: query 473 ms, zero provider/two retrieval/two policy. Warm result: query 344 ms, zero provider/zero retrieval/one policy, one result-cache hit. All selected one unit and counted 14 output tokens. With one observation per state, p50 equals p95 and comparative tail-latency conclusions are unsupported. Process-cold does not clear OS page cache; the synthetic transport has no external provider cache, so this is not a live-provider latency benchmark. Native allocations and temporary copies need separate interpretation from Python tracing.

Query channels can overlap only the two configured independent text and visual awaits. They share one pinned attempt, deadline, and cancellation context, merge in deterministic channel order, and keep canonical SQLite work on the owner lane. Scheduler tests establish cancellation/deadline behavior. A synthetic combined test also ran both authorized provider requests through one real QueryService query with text and image Chroma families, proved both awaits entered before either provider call completed, and used EvaluationService to compare lexical-only and combined ablations against a canonical SQL source anchor. Its image family was an explicitly empty batch; this qualifies channel scheduling and evaluation mechanics, not visual relevance or an image parser. Canonical representation preparation has operation limits of 20,000 selected units, 100,000 rows, 16 MiB text, and 32 MiB serialized rows. These are admission bounds rather than proof of constant CPU or memory use.

A separate synthetic PNG fixture published one actual image vector with a retained caption-copy proxy and original-asset citation. QueryService selected the real image family, and EvaluationService observed a `proxy_match` to `multimodal_match` selection-reason change between lexical and image variants. Its recording embedding transport qualifies identity, citation, and ablation mechanics only; it does not measure image relevance or the installed visual model.

`QueryCaches.max_bytes=64 MiB` limits charged serialized entries, including a 256-byte charge per entry, rather than total process RAM. In one synthetic 128-result/80-embedding fill, the cache reported 67,099,392 charged bytes while traced retained Python allocation rose 67,182,776 bytes, 83,384 bytes above the charge. Fill peak was 68,275,623 bytes, and one 441,878-byte result lookup added a 1,630,426-byte traced peak during deserialization. A 4 MiB reserve beyond the charge covers this fixture's overhead and lookup peak with margin, not arbitrary decoded object graphs or concurrent callers. A strict total-memory guarantee needs a separate decoded-result and native-allocation bound.

Run-history discovery now reads a scope-filtered SQL keyset page of at most `limit+1` rows; a direct definition reads one ID. Before the change, asking for one item among 15,000 synthetic matching runs allocated a traced 9,872,863-byte peak and took 0.2600 seconds. With the bounded read, the same n=1 workload allocated 10,769 bytes peak and took 0.0005 seconds. The existing run-ID primary key can order the keyset; without a principal/scope/run composite index, SQLite may still scan nonmatching rows, so this sample is not a worst-case latency bound. Old v1 run-scope proofs match only the caller's exact current serialization and can fail after an ordering change; new v2 proofs sort unordered grant sets.

The assertion reader verifies all leaves before exposing records, then reopens them for delivery. At 8,000 synthetic assertions in 16 leaves, live duplicate-ID metadata occupied about 2.73 MB and full verifier traced peak was about 20.16 MB. Iteration requested 17.55 MB of logical reference bytes, roughly a root plus two reads of the 8.77 MB of leaves; `LocalArtifacts.open_verified` also hashes each opened file. Leaf payloads are bounded, but whole-reader memory is not constant and its I/O has two passes. These n=1 tracemalloc observations exclude OS page-cache and device traffic.

A six-state synthetic repository delta regression traverses Git capture, actual parser-worker admission, a recording embedding port, Chroma and canonical publication. Parser calls for initial/no-op/one-file edit/deletion/rename/dirty edit are 2/0/1/0/0/1. Changed revisions still rebuild revision-qualified canonical evidence and projection bindings; this is not sublinear projection indexing. Syntax reuse is keyed by exact blob, grammar version/dialect, adapter/options identity and Tree-sitter runtime. The synthetic embedder measures engine work, not external provider latency or price.

`cache/syntax-v1` holds discardable, checksum-validated JSON syntax facts under private workspace storage, with a 32 MiB entry and 256 MiB total on-disk cap (oldest entries are evicted). It contains source text and syntax, so treat it as private workspace data. It does not contain serialized executable objects or duplicate original byte fields. Missing, ordinary corrupt, incompatible or unsafe entries are treated as misses and files are re-parsed. A known resilience gap remains for deeply nested corrupt JSON: decoding can raise `RecursionError` rather than falling back to parsing. Valid cached syntax uses the expected bounded structure. Same-language renames reuse path-neutral facts, then resolve and normalize new paths, revision-qualified IDs, ranges and diagnostics. No schema migration is needed. Canonical extraction manifests retain their complete syntax closure independently; backup/export omit this cache, and restore can inspect/replay retained evidence without it. Closing the owner and removing this directory discards only parse acceleration; it is not the runtime query-cache namespace or a promise to erase retained canonical source evidence.

Repeated identical composition selectors previously rescanned every raw and canonical unit. A resolver-only untraced sample with 5,000 raw and 5,000 canonical units took 1.246 seconds for 500 identical valid selectors; an operation-local memo over the same synthetic shape took 0.00279 seconds (one sample each). The memo retains at most 256 validated selectors and 20,000 owner references for the current bundle, and invalidates on bundle replacement. These numbers do not include full document composition. Separately, 2,000 disjoint retained manual units (4,006 charged rows, 2.32 MiB serialized) loaded in 0.097 seconds and took 0.904 seconds to prepare ownership untraced; the text/code locator owner scan remains proportional to candidates. Inputs beyond the stated canonical operation caps fail admission; there is no constant-CPU claim.

In additional single-run synthetic probes, a 500-unit graph read took 0.021 seconds; a 500-unit native graph seal without assertions took 0.155 seconds; a 1,000-unit/500-assertion native seal plus manifest took 2.007 seconds. The measured RSS delta is not native peak memory. A clipped-code citation projection over 250 units, 64 clips, and 160,000 source characters took 0.0389 seconds untraced. Neither probe establishes worst-case scaling or a latency target.

No universal throughput target or provider cost is asserted. A verified local public model bundle was restored after the initial run, and the previously unavailable tokenizer/visual cases and focused real PDF, DOCX, OCR and selected-region cases were run. The synthetic rich-PDF Docling child reached a native-inclusive `wait4` peak of about 1.42 GB; DOCX and OCR reached about 407 MB and 785 MB, respectively. These single macOS arm64 samples are neither aggregate memory limits nor portable resource budgets. The PDF mask preflight caps individual image and separately referenced mask pixels at 40 million before conversion; it does not bound total native allocation. No live-provider latency or semantic-quality result is inferred from synthetic recording transports.


Vector retrieval uses logical cosine over validated unit-normalized finite float32 values. The embedded Chroma HNSW collection physically stores `ip` with `ef_search=200`, `embedding_function=None`, and exact stored little-endian float32 byte receipts. `1-dot` is a finite-precision cosine-distance approximation for unit vectors; near ties and ANN short results can change order or mark coverage truncated. The eligible-ID search partitions at 5,000 IDs, admits at most 64 partitions and 20,000 record checks, and never falls back to an unfiltered query when eligibility is empty. These bounds and receipts protect the published view; they do not prove universal top-k recall or a latency SLA.

## Benchmark corpus and harness

`benchmarks/bench-small.toml` (100 seeded synthetic documents) needs no download.
Once generated (see below), `benchmarks/bench-v1.toml` pins about 1,000 public documents by URL, SHA-256 and
licence (UK legislation, CC BY arXiv papers, Project Gutenberg books, Python
PEPs, CPython modules, CC0 images), plus synthetic fill, and 200 queries with
stable gold labels in `benchmarks/queries/bench-v1.jsonl`. Never commit the
downloaded documents.

    python -m tychora.interfaces.bench fetch --corpus bench-v1 --cache .cache/bench
    python -m tychora.interfaces.bench run --corpus bench-small --suite ingest|query|scale --profile offline

Each run writes `bench-results/<commit>/<suite>-<profile>.json` with the commit,
the machine fingerprint, the corpus digest, per-stage n, p50, p90, p95 and p99,
and a verdict against the budgets of the engine core design. Verdicts fail only
on the runner class named in `benchmarks/reference-machine.toml` (set
`TYCHORA_BENCH_RUNNER_CLASS`); elsewhere they are informational, and
`--compare <earlier result>` fails on a p95 regression above 20%. The weekly
`Benchmarks` workflow (`.github/workflows/bench.yml`) publishes results to the
`bench-results` branch; it needs the repository secret `VOYAGE_API_KEY` for the
online job and the repository owner's approval of that secret and branch.

### Curating `bench-v1`

`benchmarks/bench-v1.toml` and its query file are generated once, by hand, and
then committed; they are not part of the default checkout until that run has
happened, so `fetch --corpus bench-v1` and the online jobs fail until then.

    uv run --locked python benchmarks/curate_bench_v1.py --cache .cache/bench

- Hosts: `www.legislation.gov.uk` (UK Public General Acts, OGL v3.0, 1 s between
  requests; 429 and 503 answers are retried with bounded backoff that honours
  Retry-After), `export.arxiv.org` and `arxiv.org` (CC BY 4.0 papers, 2 s
  between requests, listing pages included), `www.gutenberg.org`, `api.github.com` and
  `raw.githubusercontent.com` (PEPs and CPython `Lib/`, unauthenticated, about
  three API calls), `commons.wikimedia.org` and `upload.wikimedia.org` (CC0
  images). Only https on the default port of these hosts is opened, and the
  generated corpus file lists the document hosts as `allowed_hosts`.
- Duration: plan one to two hours, dominated by about 1,400 to 1,800
  legislation requests (every kept act is requested twice).
- Disk: a few GB under `--cache` (an estimate; the summary does not measure
  it). Downloads stream to disk in 1 MiB chunks, so memory stays small.
- Determinism: legislation.gov.uk may regenerate a PDF per request, so each act
  and each Gutenberg book is fetched twice and pinned only when both responses have the same SHA-256.
  Unstable acts, PDFs over 100 pages, unusable PDFs, PEPs without a
  public-domain or CC0 statement and failed requests are skipped, counted in
  the printed summary and, for unstable acts, listed by key.
- Fill: when a source yields fewer documents, seeded synthetic documents bring
  the corpus to 1,000, and synthetic marker queries plus the probe bring the
  queries to 200.
- Afterwards: check `load_corpus` reports 1,000 documents and 200 queries, run
  `fetch --corpus bench-v1 --cache .cache/bench` (it must print `0` from a warm
  cache), then commit the two generated files.

The script is covered offline by `tests/evaluation/bench/test_curate_bench_v1.py`
with injected fixtures. The queries named "2026-10-15 real-PDF probe" in the
design were never written down; only the "verification outcome" probe exists.
