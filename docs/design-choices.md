# Design choices: identity and chunking

This note records two current engine choices: SHA-256 for durable content and identity, and lossless token-counted windows for text indexing. It explains the trade-offs without claiming that the choices are optimal for every workload.

## SHA-256 identities

The engine hashes artifact bytes and derives version-tagged IDs for revisions, extractions, evidence units, chunks, and vector records. Identity inputs use canonical JSON before hashing, and the digest includes a domain tag such as `revision-v1` or `chunk-v1`. Artifact storage uses the SHA-256 of the exact payload as its content key.

SHA-256 is a conservative choice because it is specified in NIST's Secure Hash Standard. BLAKE3 is an alternative with an official specification and implementations; its authors describe it as highly parallelizable and publish their own performance claims, which should be measured on representative workloads before they are used to justify a change. That can matter in large local content stores, but changing the current digest is not a local optimization: identifiers and content paths are durable references across databases, manifests, backups, caches, and index receipts. A switch would require a versioned identity scheme and a migration or dual-read strategy. A benchmark on representative artifact sizes and hardware should come before considering that work. See [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) and the [BLAKE3 specification](https://github.com/BLAKE3-team/BLAKE3-specs).

Python's standard-library `hashlib` guarantees SHA-256 availability, so the base package does not need an additional hashing dependency or native wheel. Existing tools, manifests and external model checksums also use it. This is an interoperability and durable-identity choice, not evidence that SHA-256 is faster or more secure than BLAKE3 for this application. See the [Python hashlib reference](https://docs.python.org/3.12/library/hashlib.html).

BLAKE3 would be reasonable for a new content store or a measured internal hashing bottleneck. There is no current benchmark showing hashing dominates Tychora's parsing, embedding or indexing time. Preserve existing public identifiers even if a future internal cache uses a different hash.

These hashes are identifiers and integrity checks. They do not authenticate a sender or prove that a document is trustworthy. Authentication and access policy are separate concerns.

## Lossless token-counted windows

The current text indexer preserves exact source spans and partitions each prepared evidence unit into adjacent windows. It does not add overlap. It prefers paragraph breaks, sentence boundaries, and line breaks near a target of 512 tokens; it accepts a window up to a soft ceiling of 800 tokens. If no suitable boundary fits, it splits at the largest prefix that fits the target. The tokenizer and chunker settings are included in the chunker fingerprint, so a changed tokenizer or policy creates different chunk identities.

This policy makes every indexed window traceable to an exact source interval and avoids duplicated overlap text. Semantic boundaries can keep related sentences together, while the ceiling bounds an individual window. Provider request limits are separate. The trade-off is that a fact at a boundary may be retrieved without neighboring context; context groups and retrieval formatting can supply related evidence when configured. The 512/800 values are constants in `indexing/windows.py`, not currently user-adjustable TOML settings and not a claim of optimality. A short paragraph is not padded to 512 tokens; a remaining 700-token interval can stay intact.

When adjusting the policy, compare retrieval quality, citation span precision, indexing cost, and provider limits on a representative evaluation set. A change to target size, ceiling, or boundary rules changes the chunker fingerprint and therefore the indexed representation. See [performance](performance.md), [evaluation](evaluation.md), and [architecture](architecture.md).

## Optional reranking and current Voyage models

The base profile is offline and provider-free. Reranking sends the query and selected evidence to a remote service, adding authorization requirements, latency and cost. It therefore requires explicit provider/model configuration, verified tokenizer assets and upload permission. Optional means deployments can remain local; it does not mean reranking is unimportant to semantic retrieval quality. An explicitly online, quality-oriented profile should normally evaluate and enable it.

As of 2026-10-13, the runtime qualifies `voyage-context-4` for contextual text, `voyage-code-4` and the retained `voyage-code-3` path for independent code embeddings, and `voyage-multimodal-3.5` for images. Availability of a model path does not establish live quality or guarantee its local tokenizer assets have been provisioned.

Voyage released `rerank-3` and `rerank-3-lite` on **2026-09-30**. The vendor reports improvements for code and long documents with the same API and pricing as the corresponding 2.5 models. Those are vendor results, not a Tychora evaluation. See the [official announcement](https://blog.voyageai.com/2026/09/30/rerank-3/) and [reranker guide](https://docs.voyageai.com/docs/reranker).

Tychora currently has a mismatch: configuration accepts `rerank-3`, but runtime tokenizer/capability validation only qualifies `rerank-2.5`. `rerank-3-lite` is not a configuration choice. Do not treat changing the TOML model string as a working upgrade. Qualifying the new tokenizer, pinned asset identity, limits, response behavior and retrieval comparison is the next implementation step. A reranker upgrade does not inherently require rebuilding document embeddings, but cached rerank/query results must retain the correct model identity.
