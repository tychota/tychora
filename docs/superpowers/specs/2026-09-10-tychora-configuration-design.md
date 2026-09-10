# Tychora — kinds, relations and output profiles

Date: 2026-09-10
Status: specification; configuration loader and compatibility renderer not implemented
Related: [Product](2026-09-10-tychora-design.md), [Engineering](2026-09-10-tychora-engineering-design.md), [Storage](2026-09-10-tychora-storage-schema.md), [Algorithms](2026-09-10-tychora-pipeline-algorithms.md)

## 1. Configuration boundary

A domain can supply its vocabulary, kinds, metadata, relationships, query modes and evidence presentation through a local profile pack. It must not need a fork of the engine. Public presets use generic material; organization-specific packs, labels, fixtures and manifests stay outside the project and its release artifacts.

There are three separate concepts: **kind** describes what a source is; **role** describes its evidentiary purpose; **lane** controls grouping and optional diversity in a particular profile. File format selects a parser independently. A PDF can be a standard or feedback; changing a display label never changes its authority or acceptance state.

A role definition requires versioned ID/key, label, description and intended-use description; its classification never grants authority or access by itself. Kind `role` keys resolve against that registry. Lane definitions require a key, label, description, unique citation prefix and mapped kinds; mode definitions require allowed lanes/uses/review states and may declare narrower defaults. Ordering belongs to the profile, not a fixed core enum.

A pack contains `profile.toml`, explicit kind/relation TOML files, local JSON Schemas and optional language/renderer labels. It is data only: no Python imports, shell commands, SQL/Cypher, template execution, remote includes or automatic package installation. Installed Python adapters extend supported extraction algorithms explicitly; a pack can select their registered names/options but cannot invent executable algorithms.

Built-in packs are package resources. A workspace may explicitly reference external local pack directories; canonicalize and authorize the selected root, then keep all includes inside it. Do not scan parent folders or arbitrary environment paths. Record effective values with their origins; redact secrets and private paths when discovery is not owner-authorized.

Example workspace binding in `tychora.toml` (paths are relative to that file):

```toml
schema_version = 1

[profiles.workbench]
path = "./profiles/workbench"

[defaults]
profile = "workbench"
```

`--profile workbench` and the public client's profile selector resolve this same registered binding. The selected directory must contain `profile.toml`; named output settings belong to that resolved profile. Configuration inspection can validate bindings and schemas without opening database/model clients.

## 2. Resolution, identity and storage

Resolution order is validated defaults → selected built-in preset → declared pack dependencies → selected pack itself → workspace configuration → permitted request overrides. Dependencies are explicit `(id, version, digest)` references, topologically sorted; cycles or unresolved/conflicting definitions fail. Kind, relation and schema objects with the same versioned ID must have identical content. Built-in dependency shorthand resolves to the digest bundled with the installed release; external packs require the explicit digest. Lane/mode settings may be overridden by key and are versioned by the effective configuration hash. A changed definition requires a new version; display aliases are explicit. Lists replace as a whole, maps merge by declared keys, and no truthiness-based merging drops `false`, zero or empty lists.

Request overrides are allowlisted typed fields: query, narrower filters, requested mode, output profile and lower resource limits. They cannot load packs, replace registries, loosen granted permissions or raise configured ceilings. Collection/source usage restrictions and caller capabilities always intersect the selected mode. Selecting `inspect` or `audit` does not grant additional access.

The resolved registry is an immutable content-addressed artifact, containing full kind, role, lane, facet, relation, mode and renderer definitions. `config_sha` in snapshot/job/query manifests binds its digest and the effective settings; relation/schema digests also enter graph-family configuration and extraction fingerprints where relevant. `kinds` retains version-qualified kind rows and `facet_definitions` retains typed query registrations. Relation definitions live in this canonical registry artifact, not in a SQL adjacency table. The graph is rebuildable from that registry plus assertion artifacts.

Changing parsing or record rules creates new extractions; changing embedding representation creates a new index family; changing relation semantics creates a new predicate version and graph generation. Changing lane labels or rendering does not reembed. Query/run/result-cache identity still includes the resolved query/output configuration. A request pins those settings along with its snapshot; edits affect subsequent requests. Restrictive policy edits also increment the policy epoch immediately.

## 3. Kinds and record normalization

Each kind requires `id` (version-qualified), stable `key`, `label`, `description`, `role`, `metadata_schema`, `normalizer` and `examples`. It may declare `record_schema`, identifier extractors, facet bindings, default lane and usage restrictions. Unknown metadata is rejected unless its schema explicitly allows it. Schemas use a bounded local JSON Schema subset: object properties/required/additionalProperties, primitive types, enum, bounded arrays and local references. No remote references or executable validators. Validate schema documents and instances with pinned `jsonschema` Draft202012Validator, using only a preloaded local reference registry and explicit format validation for the permitted date formats. Enforce the supported keyword/depth/size limits first. [Validator contract](https://python-jsonschema.readthedocs.io/en/stable/validate/).

The built-in catalog must describe at least these kinds; a bare enum is insufficient:

| Kind | Description / default role | Normalized units |
| --- | --- | --- |
| law, regulation | Primary normative text, with publisher/jurisdiction and declared dates | Articles, numbered paragraphs, annex/table rows |
| decision | Published decision with deciding body, date and reference | Original passages; explicitly supplied structured findings remain distinct |
| standard | Published or internal requirements, with issuer and edition | Identified rules or structured sections |
| guidance, manual | Explanations or instructions whose authority is separately declared | Headings, paragraphs, steps, tables and figures |
| template | Output shape and completion instructions | Fields, slots and instruction blocks |
| procedure | Ordered workflow with preconditions and controls | Steps and linked rules |
| feedback | Attributed comment, target and decision | One annotation/review record per unit |
| evidence | Fact, measurement, claim or research record with declared provenance | One typed record per unit |
| example | Previous or approved illustrative material | Original passages; approval never converts an example into authority |
| rationale | Explanation of a rule or known failure | Maintenance material, excluded from composition by default |
| research_note | Attributed research observations and source references | Sections or supplied records |
| code_file, architecture_decision | Implementation or a recorded design decision | Definitions/source ranges, or decision sections |

Kind assignment applies to a registered source revision. Mixed source material can be registered as separately identified logical sources with explicit original-region provenance; a formatter never changes kind merely to place text in another lane. Structural `unit_type` (paragraph, rule, table row, symbol) is independent of source kind.

Registered normalizers initially cover `structured_sections@1`, `numbered_provisions@1`, `rule_records@1`, `feedback_records@1`, `template_slots@1` and `code_symbols@1`. Rule/feedback/template schemas retain literal requirement or comment, target, provenance, decision/status and foundation IDs. A Markdown rule normalizer uses parser heading tokens plus configured identifier prefixes and field-label aliases; it retains exact heading/body ranges and diagnostics for ambiguous blocks. A pack cannot supply arbitrary regular expressions or turn a generated interpretation into original text. Unsupported custom structures need an installed adapter or schema-conforming records supplied by the caller.

Example kind document (`kinds/standard.toml`):

```toml
schema_version = 1
id = "organization_standard@1"
key = "organization_standard"
label = "Organization standard"
description = "Approved requirements for producing and checking internal work."
role = "normative_reference"
default_lane = "standard"
metadata_schema = "schemas/standard-metadata.json"
record_schema = "schemas/rule-record.json"
normalizer = "rule_records@1"
allowed_uses = ["compose", "audit", "lookup"]
examples = ["Find the requirement identified by STD-12."]

[normalizer_options]
identifier_prefixes = ["STD-"]
requirement_labels = ["Requirement", "Exigence"]
proof_labels = ["Expected evidence", "Preuve attendue"]
```

All references in a pack must resolve during validation. Discovery returns configured zero-count kinds with descriptions, metadata/record schemas, supported normalizer capabilities and examples; source counts remain scoped separately.

## 4. Metadata and scoped filters

Facet registrations define a namespaced key, value type, operators, source binding, `filter_scope` and missing-value semantics. A persisted facet key is semantically immutable: changing its type, binding, filter scope or global-value meaning requires a new namespaced key. A profile may map a stable public alias to that key, pinned in its registry digest; old `facet_values` continue referencing the old key. Only registered fields enter FilterExpr; arbitrary metadata cannot become SQL. `filter_scope=matched_candidates` selects matching evidence; `all_units` also constrains context and relation supports. The code profile defines the two candidate-only code facets. Mixed-scope OR expressions are rejected; conjunctions may separate scoped subtrees.

Some source sets combine project-specific records with globally applicable references. Represent that explicitly: a registry facet can permit `global`, and revision metadata can declare `scope.global_facets=["project"]`. A project query includes exact project matches plus explicitly global records when its profile requests `include_global=true`. Missing metadata alone does not mean global; unknown fields are excluded or diagnosed according to the declared schema. Source-level scope applies to every child; narrower child facets cannot broaden it. Global applicability never bypasses access, dates, review or upload restrictions.

Import bindings can intentionally map an old absent scope value to explicit global applicability. That is a migration rule, recorded with provenance; it is not the default interpretation for all new ingestion. Likewise, an imported inclusive last-effective date is converted to an exclusive following date with the original value retained, or rejected if conversion is invalid. Unknown dates remain unknown.

Relevance hints are distinct from FilterExpr. A profile may register non-policy fields for `QueryRequest.hints`; those fields do not exclude a candidate or become provider instructions. Optional `scoped_rrf_bonus` adds the configured nonnegative bonus per exact hint match after base RRF, with a total per-candidate cap of 0.01, before seed/pool selection; default is disabled. Record each contribution and evaluate the profile against its no-hint baseline. Access, dates, uses and required review predicates can never be converted into hints. A hard filter remains hard even when the same contextual value is supplied as a hint.

## 5. Declarative relationships

A relation definition requires versioned ID/key, label/description, directedness, endpoint kind/unit constraints, permitted derivation methods, default status and revision-binding policy. A relation may declare `diagnostic_category=conflict|limitation|dependency|none` and `requires_decision=true|false` for deterministic projection; this labels an explicit eligible assertion, not a newly inferred judgment. Optional fields include inverse display label, self-link policy, cardinality validation, metadata schema, discovery examples and allowed search/inspection directions. Built-ins include `supports`, `contradicts`, `limits`, `supersedes`, `reviews`, `illustrates`, `derived_from`, `contains`, `precedes`, `calls`, `imports` and `inherits`.

Example custom relation document (`relations/substantiates.toml`):

```toml
schema_version = 1
id = "substantiates@1"
key = "substantiates"
label = "Substantiates"
description = "The subject provides explicitly recorded support for the object."
directed = true
inverse_label = "Substantiated by"
subject_unit_types = ["paragraph", "rule", "review"]
object_unit_types = ["paragraph", "rule"]
methods = ["native", "human", "parser_inferred"]
default_status = "recorded"
self_links = false
cardinality = "many_to_many"
revision_binding = "explicit"
require_origin = true
require_supports = true
search_directions = ["outgoing", "incoming"]
```

In v1 `require_origin` and `require_supports` are required literal `true`; rejecting a false value preserves the graph invariant of eligible origin and a nonempty complete support set. An inverse label does not create another assertion. An undirected predicate uses canonically ordered endpoints for assertion identity; independent observations keep separate identities/provenance. Defining `supports` does not discover support automatically. Assertions come from explicit manifest records, native links or installed parser adapters. Generic packs cannot enable hidden LLM extraction. Model-derived assertions, if supplied through a future adapter, begin as proposed regardless of a permissive default.

Assertions store the **version-qualified predicate ID**. Explicit endpoints reference canonical unit IDs or namespaced identifiers with source/revision bindings; unresolved or ambiguous references become diagnostics, never an arbitrary latest target. Cross-source and cross-revision relations require explicit bindings. `supersedes` records a relationship but does not rewrite effective dates or select a current version. Endpoint/support policy and current assertion decisions still govern traversal.

The graph compiler validates predicates, endpoints, cardinality, origin and all support dependencies before sealing. Search/inspection policy chooses allowed predicate IDs, directions and statuses within that registry. Unknown or unavailable required predicates fail; they are not silently ignored. Endpoint constraints, native structural invariants and query-time traversal budgets are separate. `contains`/`precedes` used by code structural context keep the invariant definitions from the code profile; a pack cannot redefine those reserved IDs.

Graph configuration selects named strategies and their validated options. Basic predicate/direction selection and code structural context are release requirements. Iterative score propagation, per-edge ranking weights and greedy segment scoring remain separately qualified experiments.

## 6. Lanes, modes and selection

The following profile excerpt illustrates the contract; its kind/relation/schema files must be supplied as a complete pack:

```toml
schema_version = 1
id = "standards-workbench"
version = "1.0.0"
extends = ["builtin:documents@1"]
kind_definitions = ["kinds/standard.toml"]
relation_definitions = ["relations/substantiates.toml"]
default_mode = "compose"

[lanes.standard]
label = "Internal standards and templates"
description = "Requirements and output conventions; distinct from primary sources."
citation_prefix = "S"
kinds = ["organization_standard@1", "template@1"]

[modes.compose]
allowed_lanes = ["authority", "standard", "feedback", "evidence", "example"]
required_lanes = ["standard"]
allowed_uses = ["compose"]
original_review_states = ["accepted"]
derived_proxy_policy = "accepted_only"

[modes.audit]
allowed_lanes = ["authority", "standard", "feedback", "evidence", "example", "maintenance"]
required_lanes = ["standard"]
allowed_uses = ["audit"]
original_review_states = ["accepted"]
derived_proxy_policy = "accepted_only"

[retrieval.graph]
semantic = false
predicates = ["substantiates@1"]
directions = ["outgoing", "incoming"]
statuses = ["recorded", "confirmed"]

[checks.relations]
enabled = true
required = false
max_assertions = 100

[selection]
lane_reservation = "one_per_available_lane"

[output]
renderer = "hierarchical-evidence@1"
lane_order = ["authority", "standard", "feedback", "evidence", "example", "maintenance"]
include_empty_lanes = true
locale = "en"
title = "Evidence pack"
envelope_tag = "evidence_context"
```

The generic documents preset supplies those six descriptive lanes and kinds. A resolved kind maps to exactly one lane; conflicts fail validation. New lanes and mode names are supported; no special behavior depends on the English words in this example. Every exposed mode must declare uses/review rules. A read-only lookup mode can inspect a supplied exact ID within its granted uses; it never implicitly bypasses exclusion policy.

Default generic ranking remains global. The optional policies are `one_per_required_lane` and `one_per_available_lane` (every permitted lane represented in the eligible fused pool). They reserve assessment and selection within existing budgets: after exact/graph admission, reserve the strongest remaining fused candidate for each selected lane in configured order, then fill the rerank pool normally. A lane already represented in an admitted slot satisfies its reservation. If slots are insufficient, report that condition. After reranking/assembly, select the best fitting segment per selected lane, then fill remaining positions by rank. Shared token, diversity, policy and segment limits still apply. Distinguish no eligible evidence, candidate-budget omission and output-budget omission; never fabricate a lane or claim corpus-wide absence from a bounded search. The trace records reservations and actual lane coverage.

Requirements are separate from reservations. Each requirement has an ID, description, a bounded `when` expression over mode and explicitly supplied request context (`eq`, `in`, `exists`, `and`, `or`), and expected lanes/kinds/record schemas with a minimum returned count. Evaluate conditions before search; absent context never activates an invented condition. `required_lanes` is shorthand for unconditional minimum-one lane requirements. Evaluate outcomes on authorized selected evidence and return typed unmet requirements with reasons, including unknown coverage caused by channel/budget failure. They cannot establish corpus-wide absence or authorize an automatic workflow decision.

## 7. Output compatibility without a domain-specific core

`search` always returns canonical, versioned QueryResult. Extend its specified contract with permitted `structured_records` attached to units, configured lane IDs, and deterministic `coverage_gaps`/`decision_requirements`. Decision requirements arise only from recorded conflicts, unresolved applicable-version choices or explicit input records; retrieval does not invent an instruction requiring human approval.

After selection and before formatting, the application runs a separate **relation/decision inspection stage** when enabled. It asks GraphStore for eligible assertions incident to selected source units, including configured diagnostic predicates, without adding search candidates or reranking. This works with semantic rank expansion disabled. Validate both endpoints, origins and every support using the pinned view and current policy. Use at most 100 returned assertions and the remaining shared 2,000 assertion-occurrence request work budget; deterministic ordering, pagination and deadlines follow the graph contract. Record this consumption separately from ranking expansion. No remaining budget yields a truncated check, not an empty completed check.

The result carries `checks.relations.status=complete|partial|not_evaluated`, checked predicate IDs, and permitted reasons. Disabled/unavailable graph checks are not evaluated; capped work is partial. Empty `conflicts` means none found in that checked scope only when complete. The mode decides whether an incomplete required check makes overall coverage partial or fails strict execution; an optional incomplete check still emits its status/warning. Edition ambiguity comes from temporal selection independently of the graph. Structured decision records are materialized through CorpusStore with the same scope checks. Text from records, context and diagnostic projections shares the output budget; omissions are explicit with inspection references, never silently altered literal quotations.

`format_result(result, output_profile)` is pure: no retrieval, network, filesystem lookup, model or policy promotion. The application materializes authorized fields first. A formatter cannot reorder search to recover missing evidence or restore withheld text. Canonical citations, original/derived status and mandatory coverage warnings survive every projection.

Built-in `hierarchical-evidence@1` provides the following stable projection:

| Field | Source / semantics |
| --- | --- |
| `request` | Sanitized allowed request fields, with explicit profile-defined aliases |
| `hierarchy[]` | Configured lane ID/label and ordered segments; optional empty groups |
| Segment metadata | Kind, authority/publisher, dates, citations, exact ranges, matched/context-only IDs, signals and permitted structured records |
| `answerability` | Compatibility field for deterministic coverage: `sufficient`, `partial`, `insufficient`; never truth or generated answer correctness |
| `answerability.missing_evidence` | Required-lane gaps, including reason and bounded-search qualification |
| `answerability.human_decisions` | Recorded, permitted decision requirements |
| `conflicts` | Eligible explicit conflicting assertions and separately identified edition ambiguity |
| `citations`, `visual_evidence`, `retrieval_trace` | Authorized originals, selected assets and actual channel/provenance diagnostics |

`insufficient` means zero returned evidence for the requested task; `partial` means evidence exists but required channels/lanes or a configured decision condition is incomplete; `sufficient` means those deterministic conditions pass. The canonical coverage object and warnings remain available. Neither `sufficient` nor a zero-gap result asserts that a source is true or the task is answered.

Formatting configuration supports lane order/labels/prefixes, localized literal labels, title, envelope tag, empty-lane behavior, coverage-status display aliases and schema-validated field aliases. An external status label is a presentation alias only; its underlying meaning remains deterministic coverage. Alias sources are an allowlist of canonical request/result/structured-record paths; collisions and missing required fields fail. Envelope tags use a restricted identifier syntax and payload strings are escaped so source text cannot terminate the wrapper. Profile labels and invariant contract fields remain separate. Literal wording can reproduce an external consumer's presentation while invariant source-use rules cannot be removed. This is a fixed renderer with parameters, not a general scripting/template language.

The API keeps rendering explicit. CLI `--json` returns canonical QueryResult; an explicitly selected output profile with `--format json` returns its versioned projection. MCP structured content remains canonical and can add the selected rendered envelope as text; HTTP `POST /v1/queries` defaults to canonical `application/json`; an explicit output profile plus `Accept: application/vnd.tychora.evidence-hierarchy+json` selects the versioned hierarchical projection. Unsupported media/profile combinations fail negotiation. CLI `--json` and conflicting explicit format switches fail validation rather than guessing. All transports use the same formatter and display its renderer/profile/schema versions. External framework adapters consume canonical evidence, not Markdown.

Import manifests may bind external document/version/node identifiers through dedicated identifier namespaces. Output aliases may select those preserved identifiers when unambiguous; canonical IDs remain available and missing mappings fail explicit compatibility validation. Generated IDs, raw ranking scores and result order are not promised identical after changing parsing/models.

Compatibility acceptance has two separate gates: (1) frozen canonical-result fixtures must reproduce the selected JSON schema, hierarchy, metadata and exact rendering, normalizing only declared volatile fields such as run IDs/timings; (2) a corpus migration/retrieval comparison checks expected and forbidden original evidence, roles, dates and relations. Private expectations run locally with external fixtures. Public CI uses synthetic fixtures only. Passing format snapshots alone cannot establish retrieval equivalence.

## 8. Validation and discovery acceptance

`inspect config` validates and explains effective settings; `inspect kinds`, `inspect relations` and API/MCP discovery expose definitions, examples, endpoint constraints, mode availability and capability limits. Relation-definition discovery is distinct from traversing evidence relationships. Dashboard configuration uses these same schemas and diagnostics.

Required implementation fixtures cover: loading a custom kind and predicate without code changes; unknown schema/adapter/predicate rejection; duplicate IDs/cyclic includes; immutable registry replay; scope-global versus unknown metadata; mode exclusion in all channels; allowed context under candidate-only filters; unresolved/cross-revision endpoints; optional lane reservations under tight budgets; localization/envelope escaping; structured feedback projection; exact-output snapshots; withdrawn evidence excluded from every renderer. Adding a pack must not modify public source files or require publishing its contents.
