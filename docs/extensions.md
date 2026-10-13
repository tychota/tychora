# Extending kinds, relationships, and code languages

## Data-only packs

A profile pack can add versioned document kinds with bounded JSON Schema definitions, relationship predicates, roles and presentation lanes, facets, historical mappings, and retrieval or output policy. A pack can also extend another versioned pack. The [configuration guide](configuration.md) includes a small generic pack example.

A kind definition chooses a normalizer already implemented by the engine. A pack can use that normalizer with new schemas and metadata requirements, but TOML and JSON cannot add extraction behavior. A new normalizer requires engine code and a versioned fingerprint so retained extractions can be distinguished from new ones.

Relationship definitions declare what an assertion may connect: endpoint kinds and unit types, direction, cardinality, revision binding, method, status, support requirements, and inspection or retrieval directions. A definition does not create an assertion. The engine still needs a parser or normalizer to emit a supported relationship, or an explicit recorded assertion with typed endpoints and provenance. For example, registering a predicate called `clarifies@1` does not by itself connect two documents or change the authority of either one.

Retained evidence keeps its versioned kind definition and schema identity. When changing profiles, keep old definitions available for retained data or provide an explicit historical mapping; replacing an old schema under the same identity risks invalidating its interpretation.

## Code language support

Repository extraction currently dispatches these suffixes:

| Language or dialect | Suffixes |
| --- | --- |
| Python | `.py`, `.pyi` |
| TypeScript | `.ts` |
| TSX | `.tsx` |
| JavaScript | `.js`, `.mjs`, `.cjs` |
| JSX | `.jsx` |

The adapters record declarations, references, exact source ranges, and diagnostics. The Python adapter recognizes classes, functions, async functions, methods, imports, calls, and inheritance. The TypeScript-family adapters recognize declarations plus imports, exports, calls, inheritance, and JSX forms. Dynamic or computed expressions may be retained as unsupported or unresolved facts rather than guessed targets.

The resolver is static and bounded. It tries exact qualified names, explicit import bindings visible in the captured source scope and revision dependencies, and same-file lexical names. It does not act as a compiler or infer package/build semantics. General wildcard, dynamic or implicit import resolution, macros and language-specific name rules need further resolver support. Named and wildcard re-export syntax is captured for JavaScript/TypeScript, but resolving targets across modules and external dependencies still requires explicit mappings; this is not general package resolution. Ambiguous matches remain ambiguous.

Adding Ruby, Elixir, Java, Swift, Kotlin, Go, or Rust is an adapter project, not just adding a grammar package. Work includes grammar/runtime dependencies, suffix dispatch, declarations and references, source-coordinate mapping, cache fingerprints, tests and fixtures, and packaging. A useful first pass that supports declarations and same-file references could take roughly 2–6 engineer-weeks per language, depending on grammar behavior and language forms. Robust cross-file resolution that accounts for imports, package roots, build files, and language-specific visibility is a separate effort, commonly several additional engineer-months per language family. These ranges are engineering estimates, not commitments; the target feature set and qualification corpus determine the actual work.

For implicit names, use an explicit progression: syntax extraction first; deterministic package/import rules next; optional semantic index ingestion for compiler-aware relationships. An inferred candidate must not be labeled a proven call target. Language-specific challenges include dynamic constants and loading in Ruby, macros and aliases in Elixir, package/classpath lookup in Java and Kotlin, extensions and modules in Swift, package-wide declarations in Go, and macro/trait resolution in Rust.

An optional adapter for externally produced semantic indexes or language-server results could avoid rebuilding every compiler's resolver. This is a proposed direction, not an existing feature. The [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) standardizes definition/reference navigation, but integrating a server still requires revision alignment, dependency context and a policy for ambiguity and tool execution.

## Storage backends

The engine has narrow ports for vector indexes and graph stores. A Qdrant adapter can implement the existing vector port; a Neo4j adapter can implement the graph port. The ports still require immutable family or generation identity, verified batch receipts, cleanup, bounded search or traversal, and the caller-supplied eligibility rules. A backend that merely stores vectors or edges is not sufficient for parity.

PostgreSQL is a broader change. The canonical persistence implementation is SQLite-specific: the owner opens `corpus.sqlite3`, application services issue SQLite queries, migrations define an exact SQLite schema, and FTS behavior is built around SQLite. The small `CorpusStore` protocol does not cover every persistence operation. PostgreSQL therefore cannot be introduced as a drop-in class behind that protocol; it needs a complete persistence boundary or a substantial port across services and operations.

The following are judgment-based engineering ranges for an experienced engineer, assuming the existing product behavior must be preserved:

| Backend | Prototype | Production parity |
| --- | --- | --- |
| Qdrant vector adapter | 1–2 engineer-weeks | 4–8 engineer-weeks |
| Neo4j graph adapter | 2–4 engineer-weeks | 6–12 engineer-weeks |
| PostgreSQL canonical store | 4–8 engineer-weeks for a limited slice | 3–6 engineer-months |

“Prototype” means a narrow end-to-end path with explicit limits. “Production parity” includes recovery and rebuild behavior, authorization and eligibility semantics, concurrency, migration or upgrade handling, observability, and meaningful acceptance coverage. Replacing only one narrow repository or using PostgreSQL as a sidecar is a smaller scope than replacing canonical SQLite persistence. These estimates come from the current code boundaries and coupling; they are not vendor pricing or a delivery promise. See [architecture](architecture.md) for the persistence and adapter boundaries.

For an illustrative budget at **€800 per engineering day** and five days per week, the production ranges correspond to approximately €16–32k for Qdrant, €24–48k for Neo4j, and €48–96k for PostgreSQL (assuming 20 working days per month). The daily rate is an example, not a market-rate claim. Hosting, licenses, maintenance and any shared refactoring are separate; do not simply add the estimates if the work is planned together. A short compatibility spike should narrow the largest uncertainties before commissioning full parity.
