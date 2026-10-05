# Retrieval evaluation

An evaluation runs the existing query pipeline against a registered, immutable gold dataset. It scores selected evidence, not words that appear in assembled context. The public fixture in `tests/fixtures/evaluation` contains synthetic integrity cases for mechanics and access rules. It is not an independently reviewed semantic benchmark, and its scores must not be used as document, code, or visual quality claims.

## Gold format and registration

A gold directory contains `manifest.json` and `cases.jsonl`. The manifest uses `format: "tychora-gold@1"`, a unique `dataset_id`, a name, `case_count`, and `version_sha` equal to SHA-256 of the exact JSONL bytes. Each line is a `GoldCase` with a stable `case_id`, `family_key`, `split` (`development` or `test`), and a complete `QueryRequest`. Related editions, derivatives, feedback, and repositories share one family key and cannot cross splits. Cases may label expected and forbidden source, document, revision, unit, asset, definition and file IDs; exact codepoint spans; graded relevance; paths; references; and edges. Semantic cases require independent label provenance. The loader rejects digest, count, schema, and family conflicts. Manifest bytes are limited to 1 MiB, case bytes to 64 MiB, and lines to 100,000.

Register only a locally selected directory with `kb.evaluations.register_dataset(path)`, or upload bounded manifest and JSONL bytes to `POST /v1/evaluations/datasets`. HTTP evaluation requests select a registered `dataset_id`; they cannot select server filesystem paths. Registration and evaluation require a management grant. Run retrieval under the caller's narrowed scope; get/list reports authorize the original job scope and the current policy epoch. Dataset labels and per-case results remain scoped artifacts.

## Runs and ablations

The full programmatic API is `kb.evaluations.run(EvaluationRequest(...))`. Its request pins `dataset_id`, `split`, active `snapshot_id`, `variants`, `seed`, `repetitions`, and `offline`. The CLI shorthand `tychora manage evaluate DATASET_ID SPLIT --dataset-path GOLD_DIR --json` registers the local gold directory and maps to one `configured` variant on the active snapshot, seed 0, one repetition, and the workspace offline setting. `--request-file REQUEST.json` supplies the full `EvaluationRequest`, with matching positional dataset and split. The shorthand preserves the configured public query path. A run returns an evaluation job ID; use `kb.evaluations.get(job_id)` or the matching HTTP get route for the report.

Variants use validated `ConfigurationVariant.overrides`. Query controls are `mode`, `modalities`, `exact_only`, `limit`, and `hints`. Execution controls are an explicit `channels` array drawn from `lexical`, `text`, `image`, `structural_graph`, and `semantic_graph`, plus boolean `rerank`. Labels have no execution effect. For example:

| Comparison | Controls |
| --- | --- |
| Lexical | `{"channels":["lexical"],"rerank":false}` |
| Hybrid | `{"channels":["lexical","text"],"rerank":false}` |
| Reranked | `{"channels":["lexical","text"],"rerank":true}` |
| Structural graph | `{"channels":["lexical","structural_graph"],"rerank":false}` |
| Semantic graph | `{"channels":["lexical","semantic_graph"],"rerank":false}` |
| Visual | `{"channels":["lexical","image"],"rerank":false}` |

Graph variants declare their seed channel explicitly; `structural_graph` and `semantic_graph` independently select the graph strategy. A graph channel without lexical, text, or image seeds is rejected. Disabled channels do not retrieve or call their providers. Required vector family, graph generation, strategy, or reranker absence returns a typed capability error, never a renamed fallback. Text/image model comparisons require compatible prebuilt families in the pinned snapshot. An evaluation does not construct providers, embed new content, rebuild indexes, or change the public default search configuration. Executed controls are retained with every variant/repetition result.

At admission, the runner rejects more than 1,024 case × variant × repetition searches before accepting a job. It rejects retained case artifacts above 64 MiB cumulatively, and a final serialized report above 64 MiB, with `LIMIT_EXCEEDED`; it never truncates results. A comparison pauses new lexical publication, rebuild, and garbage collection admissions, then waits for admitted mutations to finish before taking its snapshot and epoch cut. Accepted async source jobs wait outside the owner worker and resume afterwards. Internal synchronous mutators receive retryable `WORKSPACE_BUSY`. Query owner callbacks and content-addressed history writes continue. A policy or lexical-statistics epoch change invalidates the comparison, including at the final owner-serialized valid-result commit. Cancellation and deadlines release the pause after in-flight mutation work drains.

## Scores and release use

The report retains each case's variant, repetition, seed, query/result trace, metrics, usage, config identity, and execution controls, plus aggregate metrics and hardware/model/tokenizer inputs. Recall, reciprocal rank, nDCG, span completeness, locator validity, path precision/recall, and forbidden leakage have explicit denominators. An empty gold denominator is N/A rather than 1. File ranks use first distinct selected `(source, revision, path)` evidence; symbol ranks use selected structured definition keys. Context-only mentions do not count as retrieval hits. Resolution rate is N/A because `QueryResult` does not expose extracted references; judged edge precision is separate from path scoring.

The synthetic fixture checks mechanics and the zero-failure integrity gate for exact IDs, wrong versions, forbidden sources, and invalid locators. Semantic thresholds require independently reviewed development/test labels and an actually provisioned runtime. The fixture does not provide those labels. A missing verified model/tokenizer bundle is an unavailable runtime qualification, not a passing semantic result.
