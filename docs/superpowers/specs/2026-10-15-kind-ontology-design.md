# Kind ontology and presets (sub-project 5)

Date: 2026-10-15. Status: design, awaiting review. Programme: `2026-10-15-tychora-v2-program-design.md` (called "the programme" below). Flow review: `docs/superpowers/reviews/2026-10-15-ingestion-query-flow-review.md`.

## 1. Intent and success criteria

Tychora gets one small, deliberate ontology of document kinds and five presets built on it: `general` (the default), `software`, `regulatory-normative`, `regulatory-administrative` and `knowledge`. The two regulatory presets share a non-selectable base pack, `regulatory@1`. Kinds name what a document *is* (genre). Facets carry the other axes: authority, origin, confidentiality, jurisdiction and bibliographic type. Each kind also declares its context level, the structural unit that chunking uses to group chunks for contextual embedding. A preset is a builtin profile pack. It chooses the kinds offered, their friendly labels, lanes, modes, relations, identifier patterns and per-preset chunking and enrichment values. The tree is SKOS-style, single-parent and written in TOML. There is no OWL reasoning (programme §6). An example domain pack, `quality-improvement`, shows how an owner builds a custom ontology from a methodology.

| # | Criterion | Measured by | Programme link |
| --- | --- | --- | --- |
| K1 | The five builtin presets resolve. Every concrete kind in each has an effective role, lane, normalizer and closed metadata schema. | `tests/configuration/test_presets.py` resolves each preset and asserts the effective table. | 4 |
| K2 | Metadata schemas inherit through `allOf`. No property definition is repeated across packaged schema files. | jscpd over `src/tychora/profiles/**/*.json` reports 0 clones. Today 16 files repeat about 150 identical lines each. | 5 |
| K3 | The resolved registry artifact of the `software` preset is at most 50% of today's `code@1` artifact (561 KiB, flow review D7). | A size assertion in the preset test. | 1, 5 |
| K4 | A filter or mode requirement that names an abstract or parent kind matches every descendant. Lane resolution is O(1) per candidate. | Unit tests. Engine-core benchmark trace of the lane stage. | 1 |
| K5 | The `general` preset requires only `title`, so the dashboard can add a PDF with one prefilled field. | Effective schema test. Dashboard e2e (sub-project 6). | 4 |
| K6 | No kind identifier is hard-coded in `src/tychora/**/*.py`. | A CI grep gate for `"[a-z_]+@[0-9]+"` kind literals outside `profiles/`. | 5 |
| K7 | After a rebuild with the migration map, retrieval quality on the evaluation set is equal or better. | The existing evaluation machinery on the pinned corpus. | 3 |
| K8 | Every concrete kind in every preset and in the example pack resolves an effective context level, and chunking reads it from the registry rather than from preset-specific code. | Preset golden tests include the context level column. A chunking v2 test groups a legislation fixture by article and a code fixture by file. | 3, 5 |
| K9 | The `quality-improvement` example pack resolves against the shipped `general@1` and ingests its synthetic fixture corpus. | `tests/configuration/test_example_packs.py`, run in CI. | 5 |

## 2. Scope and non-goals

In scope:
- the kind definition format v2: `broader`, `abstract`, external mappings, facet defaults, and inheritance with override;
- metadata schema inheritance;
- the registered facets and their binding to access;
- identifier schemes as pack data;
- per-kind context level as kind data consumed by chunking;
- the five presets, the shared `regulatory@1` base pack and their skills;
- localized preset labels;
- the example domain pack `quality-improvement` under `examples/profiles/`;
- the BibTeX import mapping;
- a SKOS JSON-LD export;
- the migration map;
- removal of the legacy packs.

Non-goals:
- OWL, multiple inheritance, or inference beyond transitive `broader`;
- an ontology editor;
- package or dependency units (deferred, see §8);
- connectors for official sources;
- the chunking algorithm (sub-project 2) and the enrichment pipeline (sub-project 4). This spec only sets their per-preset values;
- dashboard UI (sub-project 6);
- per-source generation policy and review gating changes (engine core).

## 3. Current state

- **Packs.** Four builtin packs exist: `documents@1`, `general-documents@1`, `code@1` and `verified@1` (`src/tychora/profiles/manifest.json`). `documents@1` defines 15 flat kinds, 14 relations, 6 lanes and 3 modes. New workspaces get `general-documents@1` (`api.py:36`). That pack adds `document@1` by re-listing the evidence lane (`profiles/general-documents/profile.toml`).
- **Lanes are bound by exact kind ids.** Each lane lists versioned kinds. Any kind listed in two lanes is rejected (`configuration/registry.py:89-102,465-475`). Every kind file also declares `default_lane`, but no Python code reads it (dead data).
- **Schemas are duplicated.** Each `*-metadata.json` copies the same 19 properties with `additionalProperties:false` and changes only `required` (e.g. `profiles/documents/schemas/law-metadata.json`). The validator is `jsonschema` 4.26.0 `Draft202012Validator` (`uv.lock`, `configuration/schemas.py:10,173`). It supports `unevaluatedProperties`. The bounded keyword allowlist does not admit `allOf`, `unevaluatedProperties`, `if`, `then`, `anyOf` or `const`. That allowlist and its walker are copied twice: `configuration/schemas.py:24-46,95-147` and `configuration/resolve.py:166-250`.
- **Facets** are namespaced, typed and scalar. Values come from `metadata.*`, `file.*` or `record.*` (`registry.py:544-585`, `application/sources.py:1601-1633`). Policy-named fields cannot be relevance hints (`registry.py:344-364,573-575`). No facet exists for kind, issuer, binding level or confidentiality.
- **Access** is numeric, from 0 to 3. It is stored as `access_floor` on collections and sources and as `access_rank` on revisions (`contracts/corpus.py:20,33,47,62`). Floors only rise (`adapters/sqlite/corpus.py:248-258`). Ingestion fails if a floor exceeds the grant's `access_ceiling` (`application/owner.py:963`). No metadata field feeds the floor.
- **Hard-coded kinds**: `code_file@1` in `registry.py:648-649`, `retrieval/response.py:688` and `application/discovery.py:557,612`; `document@1` in `application/collections.py:78`.
- **Hard-coded identifiers**: the namespaces (`retrieval/lexical_query.py:19-27`) and the English-only `Article N` pattern (`lexical_query.py:58-62`).
- **Lane and role lookup** dumps the whole configuration per call (`retrieval/segments.py:56-79`). It also issues one SQL query per fused candidate (`application/query.py:1036`). See flow review D7 and §4.3–4.4.
- **`ProfileDocument.preset`** is a second dependency mechanism next to `extends` (`configuration/models.py:232`, `resolve.py:339-340`).

What is wrong: one flat list mixes genre with authority, origin and container type. Applying the "missing kinds" from the administrative-compliance use case would put binding doctrine in the *standard* lane. Schema changes require editing 16 files. Kind semantics leak into Python.

## 4. Design

### 4.1 Packs and presets

The builtin packs become `core@1`, `general@1`, `software@1`, `regulatory@1`, `regulatory-normative@1`, `regulatory-administrative@1` and `knowledge@1`. `general@1`, `software@1`, `knowledge@1` and `regulatory@1` extend `builtin:core@1`. `regulatory-normative@1` and `regulatory-administrative@1` extend `builtin:regulatory@1`, and through it `core@1`. All dependencies are digest-pinned through `manifest.json` (unchanged mechanism, `resolve.py:297-330`).

A pack is a selectable preset if and only if it has a `[preset]` table. Five packs have one. `core@1` and `regulatory@1` have none: they cannot be chosen for a workspace and exist only to be extended. What `regulatory@1` holds is listed in §4.7.

`core@1` holds:
- the tree;
- the base schemas;
- the nine existing roles;
- the six lanes (authority A, standard S, feedback F, evidence E, example X, maintenance M);
- the core relations, facets and identifier schemes;
- the modes `compose`, `audit` and `lookup`;
- the query profiles.

The string field `ProfileDocument.preset` is removed. A new `[preset]` table describes a selectable preset:

```toml
[preset]
label = "General documents"
description = "Any PDF, Word, Markdown or image; title is the only required field."
default_kind = "document@1"            # replaces collections.py:78
offered_kinds = ["document@1", "guidance@1", "record@1", "discussion@1", "example@1", "visual@1"]
offered_modes = ["compose", "audit", "lookup"]   # optional; defaults to every registered mode
skill = "skills/SKILL.md"              # contained, part of the pack digest
access_labels = ["public", "internal", "confidential", "restricted"]  # rank 0..3
[preset.kind_labels]                   # friendly labels in the pack's base language, presentation only
"guidance@1" = "Guide or manual"
[preset.locales.fr.kind_labels]        # localized labels; same tables, keyed by BCP 47 tag
"guidance@1" = "Guide ou manuel"
[preset.facet_defaults."procedure@1"]  # defaults for core kinds this preset offers
"doc.origin" = "internal"
```

**Localized labels.** `[preset.locales.<tag>]` may contain `label`, `description`, `kind_labels`, `lane_labels`, `mode_labels` and `access_labels`, with the same shapes as the base tables. The existing `[output] locale` setting (a BCP 47 tag, default `en`) selects the table. A missing entry falls back to the base table. Labels never enter digests of kind definitions, only the pack digest, so changing a label never changes revision identity. Every builtin preset ships English base labels. `regulatory-administrative@1` also ships a complete `fr` table, because its source material is mostly in French; other locales can be added as pack data.

`offered_kinds` controls what discovery and the dashboard present first. The API still accepts any concrete kind in the resolved registry. `verified@1` is removed. Its three mode overrides become a documented workspace override (`[configuration.modes.compose] original_review_states = ["accepted"]`), which `resolve.py:278-281,444-448` already supports.

### 4.2 Kind definition format v2

```toml
schema_version = 2
id = "administrative_doctrine@1"
key = "administrative_doctrine"
broader = "guidance@1"          # exactly one versioned parent; only work@1 has none
abstract = false                # abstract kinds cannot be ingested
label = "Administrative doctrine"
description = "Published interpretation by a tax authority; opposable to that authority."
metadata_schema = "schemas/administrative_doctrine-metadata.json"
examples = ["Find the doctrine paragraph on eligible staff costs."]
role = "primary_authority"      # optional: inherited from broader when absent
context_level = "depth:1"       # optional: inherited from broader when absent; see "Context level"
# also inheritable: normalizer, normalizer_options, record_schema, allowed_uses
[facet_defaults]
"doc.issuer_type" = "tax_authority"
"doc.binding_level" = "opposable"
[mappings]                      # SKOS mapping properties; absolute URIs, at most 8 each
closeMatch = []
broadMatch = ["https://schema.org/CreativeWork"]
```

`default_lane` is removed. **Lanes** keep their `kinds` list, which may now name any kind, abstract ones included. A kind's lane is the lane that lists its nearest ancestor-or-self. A child listed in another lane overrides its parent. For example, `standard@1` is listed in *standard* while its parent `normative_text@1` is listed in *authority*.

**Inheritance.** `role`, `normalizer`, `normalizer_options`, `record_schema`, `allowed_uses` and `context_level` are inherited from the nearest ancestor that sets them. A child may override `role`, `normalizer` and `context_level`. `allowed_uses` may only narrow (it must be a subset). `facet_defaults` merge from root to leaf, and the most specific value wins. The precedence at ingestion is: explicit metadata, then preset `facet_defaults`, then kind `facet_defaults`. Defaults are written into the metadata before `metadata_sha`, so revision identity reflects them.

**Validation** (`Registry._validate_definitions`, raising `INVALID_REQUEST`):
1. Exactly one root, `work@1`. Every other kind has one `broader` that resolves to a kind in the same resolved registry. There are no cycles and the depth is at most 8.
2. A concrete kind resolves an effective role, normalizer, lane, context level and closed metadata schema. An abstract kind needs only a lane. A context level of `file` requires the effective normalizer `code_symbols`, and `code_symbols` requires `file`.
3. A child's metadata schema extends its parent's (§4.3). This is checked on the resolved `$ref` binding, not by name.
4. `facet_defaults` keys are registered `metadata.*` facets. Each value passes both the facet type and the kind's schema.
5. Mappings are absolute `http(s)` URIs. Nothing is fetched.
6. Existing rules stay: versioned identity, immutable definitions across packs, digest-pinned dependencies and conflicting definitions rejected (`resolve.py:131-135,389-394`).

The registry computes three maps once at construction: `ancestors[kind]` (a tuple from self to root), `descendants[kind]` (a frozenset) and `effective[kind]` (the flattened definition). It exposes `lane_of(kind)`, `role_of(kind)`, `context_level_of(kind)`, `is_a(kind, ancestor)` and `expand(kinds)`. These replace `segments._lane` and `segments._role` and the per-candidate SQL in `query._candidate_lanes`. The historical kind envelope (`registry.py:171-207`) stores the *effective* kind plus the ancestor ids and their digests, so a retained revision keeps its meaning if a parent later changes. `_resolved_hashes` (`resolve.py:556-569`) fingerprints the effective lane bindings rather than the raw lane lists.

**Subsumption everywhere kinds are named:**
- the new reserved facet `doc.kind` (`source_binding = "revision.kind"`, operators `eq` and `in`, scope `all_units`);
- mode requirements with `target = "kind"`;
- relation `subject_kinds` and `object_kinds`;
- the reserved code relations, which now name `source_code@1`.

`compile_filter` expands each named kind to `descendants` at compile time. The eligibility layer therefore still sees a plain `in` over kind ids, with no tree walk at query time (K4). Code that branched on `code_file@1` branches on the effective normalizer `code_symbols@1` instead. That is the behaviour it actually depends on (K6).

**Context level.** `context_level` names the structural unit that chunking v2 uses as the context group for the contextual text family (`voyage-context-4` embeds the chunks of one group together; model-defaults spec). It is kind data because the right unit depends on the genre: a law is read article by article, a book chapter by chapter, a code base file by file. The field and its vocabulary are defined once by engine core (§4.10 of its design: a named section level, `depth:N` or `document`, resolved by `section_key`); this spec declares values per kind. The values used by the builtin presets are:

| Value | Context unit | Typical kinds |
| --- | --- | --- |
| `depth:1` | One top-level section (the chapter): the units whose `section_path` shares its first heading below the title. Units before the first heading form one group. Default, set on `work@1`. | document, guidance, scholarly work (books and long articles) |
| `article` | One numbered provision of the enacting terms, and each annex as a whole. Recitals and the preamble form one group. | legislation, normative texts with numbered paragraphs |
| `clause` | One top-level numbered clause with all its subclauses (clause 7 holds 7.1, 7.1.2 and so on), and each annex as a whole. Front matter forms one group. | standards, internal standards |
| `file` | The whole source file. | source code |
| `document` | The whole document. | short records, discussions, examples, visuals, templates |

Chunking v2 consumes the value through `registry.context_level_of(kind)`; it owns how the groups are built, how a group that exceeds the context model's per-group input budget is split, and the evaluation of these choices. Article, annex and clause boundaries come from the normalizer's provision and rule records and from the Docling heading hierarchy with the preset's `numbering_schemes` (Docling enrichment spec §4.6). When a document of an `article` or `clause` kind yields no such boundaries, engine core's resolver falls back to `depth:1` and chunking records a `CONTEXT_LEVEL_FALLBACK` diagnostic on the revision. The effective context level enters the chunking `plan_fingerprint`, so changing it re-projects chunks and never reinterprets old ones.

The rule for adding a narrower kind (§4.7) counts a different context level as a reason, because it changes how the document is embedded.

### 4.3 Metadata schema inheritance

Each schema file has one open part and one closed root. The pattern below was verified with `jsonschema` 4.26.0: parent fields are accepted, child-only fields are accepted, unknown fields are rejected and parent requirements still apply.

```json
{ "$id": "law-metadata@1", "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": { "open": {
      "allOf": [ { "$ref": "normative_text-metadata.json#/$defs/open" } ],
      "required": ["jurisdiction", "issuer", "publication_date"] } },
  "$ref": "#/$defs/open", "unevaluatedProperties": false }
```

Why the open part is needed: a closed parent cannot be extended. Its `unevaluatedProperties` only sees its own subtree. Children therefore reference the parent's `$defs/open`, and only the bound root is closed. A child can add properties and requirements but can never loosen the parent.

The base schema is `metadata@1` in `core@1`. Its properties are:
- `title` (required);
- `authors` and `editors` (arrays, bounded to 4096);
- `publisher`, `issuer`, `reference`, `edition`;
- `language` (BCP 47 string);
- `jurisdiction`;
- `publication_date`, `effective_start`, `effective_end` (with the existing half-open date semantics, `docs/configuration.md:62`);
- `source_url`, `provenance`, `approval`, `scope`;
- the facet-bound enums `issuer_type`, `binding_level`, `origin`, `confidentiality` and `legal_force`.

Each concrete kind binds a closed schema. A core kind without a delta binds `<kind>-metadata@1`, which is a two-line wrapper.

The keyword allowlist gains `allOf`, `anyOf`, `if`, `then`, `const` and `unevaluatedProperties`. These are all local, bounded and walked with the same budget. The two copies of the allowlist and walker merge into one module, `configuration/schema_subset.py`, which both resolution and `LocalSchemas` use.

### 4.4 Facets and access

| Key (alias) | Binding | Values | Hint | Notes |
| --- | --- | --- | --- | --- |
| `doc.kind` (`kind`) | `revision.kind` | kind id, subsumption | no | reserved |
| `doc.jurisdiction` (`jurisdiction`) | `metadata.jurisdiction` | string (ISO 3166 or `EU`) | yes | |
| `doc.issuer_type` (`issuer_type`) | `metadata.issuer_type` | `legislature`, `court`, `public_body`, `tax_authority`, `standards_body`, `conformity_assessment_body`, `intergovernmental_body`, `organisation`, `individual` | yes | |
| `doc.binding_level` (`binding_level`) | `metadata.binding_level` | `binding`, `opposable`, `persuasive`, `informative` | yes | shown in output beside the role (parity: legal-force tiering) |
| `doc.origin` (`origin`) | `metadata.origin` | `internal`, `external` | yes | |
| `doc.confidentiality` (`confidentiality`) | `metadata.confidentiality` | `public`, `internal`, `confidential`, `restricted` | **no** | policy field |
| `doc.legal_force` (`legal_force`) | `metadata.legal_force` | `in_force`, `partially_in_force`, `not_in_force` | yes | maps to `schema:LegalForceStatus` members |
| `doc.language` (`document_language`) | `metadata.language` | BCP 47 | yes | the alias avoids `code.language` |
| `bib.entry_type` (`entry_type`) | `metadata.entry_type` | §4.7 enum | yes | knowledge preset |
| `std.harmonised` (`harmonised`) | `metadata.harmonised` | boolean | yes | regulatory-normative preset; set on harmonised standards |
| `code.test` (`test`) | `file.is_test` | boolean | yes | reserved code facet; set from per-language path rules |

The effective period stays in revision columns, not facets. `as_of` and `cutoff` already use those columns.

**Confidentiality and access.** `confidentiality` joins `_POLICY_PARTS`, so it can never be a relevance hint. At registration its rank (in the order of the table) raises the *source* `access_floor` through the existing raise-only path. The effective floor is `max(collection floor, declared floor, rank)`. The existing ceiling check then refuses ingestion above the grant. The facet filters but never grants access (`registry.py:258`). `access_labels` gives the four ranks names per preset. They are presentation only.

Provider upload consent stays per collection. Registration refuses a source of rank 2 or higher into a collection whose policy grants any upload provider, unless the collection policy sets `max_upload_rank`. That field is owned by engine core; see §8.

### 4.5 Identifier schemes

Identifier namespaces move from Python to registry data. A scheme looks like this:

```toml
[identifiers.case_number]
namespace = "case"
pattern = '(?i)\b(?:n[o°]|req\.)\s*([0-9]{2}-[0-9]{2}\.[0-9]{3}|[0-9]{5,7})'
normalize = "{1}"         # normalization template over capture groups
kinds = ["ruling@1"]      # subsumption; units of these kinds index matches
```

Fields:
- `namespace`, `pattern` and `normalize`, as in the example;
- `kinds`, matched by subsumption;
- role, decided by the chunking side: `defines` in a heading or record identifier, `mentions` elsewhere (chunking v2 §4.9).

Validation:
- each pattern is at most 256 characters;
- backreferences and nested unbounded quantifiers are rejected at load;
- at most 32 schemes per registry.

At ingestion, chunking v2 runs these schemes (`identifiers.py`). At query time, `extract_identifiers` runs the same compiled set over the question. This replaces the hard-coded namespaces and the English-only `Article` pattern. The builtin namespaces `provision`, `rule` and `code.*` remain as core schemes.

### 4.6 Relations

New relations, each with the existing required fields (`registry.py:586-639`):
- `implements@1`: code → `normative_text` or `design_record`; methods human or model; default status `proposed`;
- `amends@1` and `transposes@1`: legislation → legislation;
- `interprets@1`: guidance or ruling → `normative_text`;
- `tests@1`: a test symbol → a symbol; parser-inferred when a `code.test` file's function calls the symbol;
- `changes@1`: a symbol unit in revision N → the same-identity unit in revision N−1, emitted natively for modified symbols.

A commit is therefore a **revision**, not a kind. Its hash is already captured (`ingestion/acquire.py:380-386`).

Relation definitions gain the same `[mappings]` table:

| Relation | Mapping (verified) |
| --- | --- |
| `cites` | exactMatch `cito:cites` |
| `supports` | exactMatch `cito:supports` |
| `contradicts` | closeMatch `cito:disagreesWith` |
| `reviews` | exactMatch `cito:reviews` |
| `supersedes` | closeMatch `dcterms:replaces` |
| `amends` | exactMatch `schema:legislationAmends`, `eli:amends` |
| `transposes` | exactMatch `schema:legislationTransposes`, `eli:transposes` |
| `interprets` | broadMatch `cito:discusses` |
| `implements`, `tests`, `changes` | none |

### 4.7 The core tree and the five presets

Core kinds live under the abstract root `work@1`, which closeMatches `schema:CreativeWork` and `fabio:Work`. The rule for adding a narrower kind: it must change the lane, role, required metadata, normalizer or context level. Otherwise the difference is a facet.

| Core kind | Role | Normalizer | Lane | Context level | Required delta | Mappings |
| --- | --- | --- | --- | --- | --- | --- |
| `document@1` | supporting_evidence | structured_sections | E | depth:1 | — | broadMatch `dcmitype:Text` |
| `normative_text@1` | normative_reference | numbered_provisions | A | article | `issuer` | broadMatch `schema:CreativeWork` |
| `ruling@1` | primary_authority | structured_sections | A | depth:1 | `issuer`, `reference`, `publication_date` | broadMatch `schema:CreativeWork` |
| `guidance@1` | explanatory_reference | structured_sections | S | depth:1 | `publisher` | closeMatch `schema:TechArticle` |
| `procedure@1` | normative_reference | structured_sections | S | depth:1 | — | closeMatch `schema:HowTo`, `fabio:StandardOperatingProcedure` |
| `template@1` | output_convention | template_slots | S | document | — | broadMatch `schema:DigitalDocument` |
| `scholarly_work@1` | supporting_evidence | structured_sections | E | depth:1 | `authors` | closeMatch `fabio:ScholarlyWork` |
| `record@1` | supporting_evidence | structured_sections (+ record schema) | E | document | `provenance` | broadMatch `schema:CreativeWork` |
| `design_record@1` | maintenance_context | structured_sections | M | document | — | broadMatch `schema:CreativeWork` |
| `discussion@1` | review_observation | feedback_records | F | document | `authors` | closeMatch `schema:Comment` |
| `example@1` | illustrative_material | structured_sections | X | document | `approval` | broadMatch `schema:CreativeWork` |
| `source_code@1` | implementation_evidence | code_symbols | E | file | — | exactMatch `schema:SoftwareSourceCode`; closeMatch `dcmitype:Software` |
| `visual@1` | supporting_evidence | structured_sections (figure unit) | E | document | — | closeMatch `schema:ImageObject`, `dcmitype:StillImage` |

`law`/`regulation` collapse into one kind, `legislation@1`, with a `legislation_type` field (closeMatch `schema:Legislation` and `eli:LegalResource`). The two kinds differed in nothing that the rule above accepts. A regulation such as the EU Medical Device Regulation is `legislation` with `legislation_type = "regulation"`. `legislation@1` lives in `regulatory@1`.

**The shared regulatory base pack.** `regulatory@1` holds everything that both regulatory presets need and that has the same meaning in both:
- the kind `legislation@1` (normative_text; role primary_authority; context level `article`; requires `jurisdiction`, `publication_date` and `legislation_type`; defaults `issuer_type = legislature`, `binding_level = binding`);
- lane bindings: authority A lists `legislation@1` and `ruling@1`; standard S lists `guidance@1`, `procedure@1` and `template@1`;
- the relations `amends`, `transposes`, `interprets`, `implements`, `supersedes`, `cites`, `limits`, `supports`, `contradicts` and `reviews`;
- the identifier schemes for articles (English and French forms), annexes, ELI paths and ECLI;
- the Docling `numbering_schemes` for legal article and annex numbering (Docling enrichment spec §4.6);
- the shared skill rules, `skills/REGULATORY.md`.

Each regulatory preset adds its own kinds, lane overrides, modes, labels, identifier schemes and chunking values. Nothing else is shared: medical-device conformity and administrative compliance use different authorities, evidence and workflows, and a kind that only one of them needs stays in that preset.

| Preset | Narrower kinds (broader) and differences | Lanes and modes | Relations | Chunking v2 (soft min–max / hard) | Enrichment tier |
| --- | --- | --- | --- | --- | --- |
| **general** (default for new workspaces, replacing `api.py:36`) | none; offers document, guidance, record, discussion, example and visual | core lanes; compose, audit, lookup | cites, supports, contradicts, derived_from, illustrates | 300–1,500 / 2,000 | standard |
| **software** | `spec@1` (normative_text; lane S; structured_sections; context level `depth:1`); `adr@1` (design_record; requires `status` ∈ proposed/accepted/rejected/deprecated/superseded and `decision_date`); `issue@1` and `pull_request@1` (discussion; structured_sections; require `number`, `state`, `url`); guidance labelled "Documentation"; visual labelled "Diagram" | evidence carries code; compose, audit, lookup | contains, precedes, calls, imports, inherits, implements, tests, changes, reviews (PR → code), supersedes (ADR → ADR), cites, illustrates | documents 300–1,500 / 2,000; code keeps symbol units | standard + code enrichment |
| **regulatory-normative** (extends regulatory; medical devices, standards and norms) | from the base: `legislation@1`, including EU regulations and directives; `standard@1` (normative_text; lane S; rule_records; context level `clause`; requires `issuer` and `edition`; optional `harmonised` and `harmonisation_reference`, the official-journal reference that lists it; defaults standards_body/persuasive); guidance labelled "Guidance document" (guidance from a regulator or a coordination body on applying a regulation); `technical_documentation@1` (record; context level `depth:1`; requires `product` and `version`; defaults internal/confidential); `test_report@1` (record; requires `product`, `test_date` and `outcome` ∈ pass/fail/inconclusive); `audit_finding@1` (discussion; requires `target`, `issuer` and `severity` ∈ major/minor/observation; defaults conformity_assessment_body/confidential), covering notified-body and internal audit findings; ruling, procedure and template as in core | A: legislation, ruling; S: standard, guidance, procedure, template; E: technical documentation, test report; F: audit finding; compose, audit, lookup | base set; `implements` (technical documentation → legislation or standard), `supports` (test report → technical documentation), `reviews` (audit finding → technical documentation or procedure) | 200–1,200 / 2,000; the article or clause is the boundary | standard |
| **regulatory-administrative** (extends regulatory; tax and administrative compliance; complete `fr` labels and `access_labels`) | from the base: `legislation@1`; `administrative_doctrine@1` (guidance → lane **A**, role primary_authority, requires `reference` and `publication_date`, defaults tax_authority/opposable); `official_guide@1` (guidance → lane **A**, requires `edition`, defaults public_body/informative); `official_template@1` (template → lane **A**, requires `issuer` and `edition`, defaults origin external); `methodology_reference@1` (normative_text → lane **A**, context level `article` over numbered paragraphs, defaults intergovernmental_body/persuasive), for international methodology references; `internal_standard@1` (normative_text → S, rule_records, context level `clause`, defaults internal/internal); `rationale@1` (design_record; `allowed_uses` audit, lookup); `expert_review@1` (discussion; requires `target` and `decision`; defaults confidential/internal); `negative_example@1` (example → lane **M**; `allowed_uses` audit, lookup); procedure labelled "Playbook"; ruling described as an individual decision, not a general rule | `research` (allowed A, required A); `draft` (all but M); `audit` (all); `repair` (all, required S and F); `lookup` (all). Core `compose` is not offered: `draft` replaces it. | base set; `reviews` (expert review → the reviewed document or internal standard) | 200–1,200 / 2,000; the article is the boundary | standard + Docling OCR on embedded template images |
| **knowledge** (books, articles, state of the art) | none: `scholarly_work` carries `bib.entry_type` and per-type requirements (context level `depth:1`, so a book is grouped by chapter and a short article forms one group under its title); `record` labelled "Note or dataset"; guidance, discussion and visual | core lanes; compose, audit, lookup | cites, supports, contradicts, reviews, derived_from, illustrates | 400–1,500 / 2,000 | standard + formula + code enrichment |

`offered_modes` in the `[preset]` table lists the modes a preset presents. It defaults to every registered mode, and `regulatory-administrative@1` sets it to the five modes above.

**Identifier schemes per preset:**
- base (`regulatory@1`, used by both regulatory presets): articles in English and French forms, annexes, ELI paths, ECLI;
- regulatory-normative: those, plus standard designations (publisher prefix, number, optional part and edition year, such as `EN ISO 1234-2:2020`) and clause numbers scoped to `standard@1`;
- regulatory-administrative: those, plus doctrine paragraph references (`§ N`), case numbers, configurable doctrine-reference prefixes and internal rule prefixes;
- knowledge: DOI, ISBN and citation keys;
- software: `code.*` plus `#N` issue numbers scoped to issue and pull_request.

The ECLI pattern follows the five colon-separated parts with an ordinal of at most 25 characters (verified on e-justice). The court-code length limit of 7 characters is **unverified**.

The chunking column shows `soft_min`–`soft_max` / `hard_max`. The values are the initial values of chunking v2 §4.7, which has one row per preset and one for code files. `section_depth`, `target`, `τ` and `W` come from the matching chunking v2 row. Chunking v2 owns the parameter names and their validation on the evaluation set (programme criterion 3). Context grouping is not a preset value: it comes from each kind's context level (§4.2).

The enrichment column names the tiers of the Docling enrichment spec (§4.1). "Code enrichment" and "formula" mean T1. T0 is always on. T2 Haiku descriptions follow consent in every preset. Both regulatory presets inherit Docling `numbering_schemes` for legal article and annex numbering from `regulatory@1`; the other presets leave it null.

Presets set these values as `[chunking]` and `[enrichment]` tables in `ConfigurationValues`.

**Query profiles** (`[query_profiles]` in core): `balanced` uses the defaults. `precision` sets `limit` 5, `max_tokens` 4,000 and `graph_seed_limit` 2. `comprehensive` sets `limit` 16, `max_tokens` 12,000 and `graph_seed_limit` 5. They are selected like `output_profile`, by one more permitted request override in `resolve.py:270,478-488`, and they may only lower configured ceilings. They are cheap because they reuse that path.

**Preset skills.** Each preset ships `skills/SKILL.md` in its pack. Discovery returns it next to the preset, and it is packaged like the existing skills. For the two regulatory presets, discovery also returns `regulatory@1`'s `skills/REGULATORY.md`, which states the shared rules:
- weigh evidence by `binding_level`, then by role;
- distinguish publication dates from in-force dates, and pass `as_of` whenever the question concerns a date or a period;
- treat a ruling as case-specific;
- cite locators (article, annex, clause or paragraph);
- stop and report when a required lane is missing.

The regulatory-normative skill adds:
- separate what the legislation requires from how a standard or guidance document proposes to meet it;
- a harmonised standard gives a presumption of conformity only in the edition named in its `harmonisation_reference`; report any other edition as such;
- an audit finding is an observation about its target, never a requirement;
- a test report supports the product and version it names, not later versions.

The regulatory-administrative skill adds:
- pass `as_of` for the tax or reporting year in question;
- never put internal, feedback or maintenance lanes into an external document;
- use negative examples only to audit or repair, never as a model to draft from.

**BibTeX and BibLaTeX.** The `entry_type` enum follows BibLaTeX 3.22a regular types: article, book, inbook, incollection, collection, proceedings, inproceedings, booklet, manual, report, thesis, unpublished, online, dataset, software, patent, periodical, misc. Required fields per type are encoded as `if`/`then` blocks in `scholarly_work-knowledge-metadata@1`, following BibLaTeX §2.1.1 (verified in the PDF). `year/date` maps to `date`, and `author/editor` to `anyOf`. Examples:
- article: authors, title, `journaltitle`, date;
- inbook and inproceedings: authors, `booktitle`, date;
- incollection: authors, editors, `booktitle`, date;
- collection and periodical: editors, date;
- proceedings: date;
- report and thesis: authors, `type`, `institution`, date;
- patent: authors, `number`, date;
- online: authors or editors, date, and one of `doi`, `eprint` or `url`;
- book and unpublished: authors, date;
- booklet, manual, dataset, software and misc: authors or editors, date.

Classic BibTeX requirements differ. For example, BibTeX `book` also requires `publisher` (verified in `btxdoc`). The importer therefore validates against the BibLaTeX rules and reports classic-only gaps as warnings.

The import `tychora add --bibtex refs.bib` maps:
- aliases: `mastersthesis` and `phdthesis` → thesis with a default `type`; `techreport` → report; `conference` → inproceedings; `electronic` and `www` → online; `mv*`, `supp*` and `*reference` → their base type;
- fields: `author` → `authors` (split on ` and `); `journal`/`journaltitle`/`booktitle` → `container_title`; `school` → `institution`; the citation key → identifier `bib:<key>`.

Only entries whose `file` field resolves inside an acquisition root are added. The others are reported and not added, because Tychora needs original bytes. Parsing uses the `bibtexparser` stable 1.x line in a `bibtex` extra. Whether 2.x is stable is **unverified**.

Entry-type mappings, all verified:

| Entry type | Mapping |
| --- | --- |
| article | `schema:ScholarlyArticle` |
| book | `schema:Book` |
| thesis | `schema:Thesis` |
| report | `schema:Report`, `fabio:TechnicalReport` |
| inproceedings | `fabio:ConferencePaper` |
| proceedings | `fabio:ConferenceProceedings` |
| manual | `fabio:InstructionManual` |
| patent | `fabio:Patent` |
| dataset | `schema:Dataset` |
| unpublished | `schema:Manuscript` |
| online | `schema:WebPage` |

### 4.8 Visual kind

`visual@1` takes PNG and JPEG through the existing document path (`application/sources.py:1590-1597`). It produces one figure unit whose title and caption metadata are the lexical text.

- **Online with collection consent:** the image is embedded in the image vector family (`voyage-multimodal-3.5` per the model-defaults spec). Claude Haiku descriptions run as the background enrichment phase. They are derived evidence with provenance, never original text.
- **Offline:** the image is still searchable by title, caption and facets.

A `visual.type` facet (`diagram`, `architecture`, `drawing`, `photo`, `chart`, `scan`) can be filtered and hinted.

There is no separate images preset, because every preset offers `visual@1`. The model-defaults spec requires `providers.image` in the online tier of any preset whose collection holds visual sources.

### 4.9 SKOS export

`tychora inspect kinds --format skos-jsonld` emits one `skos:ConceptScheme` with these terms: `skos:Concept`, `skos:prefLabel` (the preset label and locale), `skos:definition`, `skos:broader`, `skos:inScheme`, `skos:exactMatch`, `skos:closeMatch` and `skos:broadMatch`. All of these are verified in the W3C SKOS RDF. It is a pure function of the registry (about 80 lines) with no dependency. It is included because it is the cheapest way to let OWL or SKOS tools consume the scheme without Tychora reasoning over it.

### 4.10 Dashboard implications

Discovery of kinds returns, per offered kind:
- the friendly label;
- the ancestor chain;
- the effective `required` list, read from the closed schema;
- `facet_defaults`;
- the context level;
- the access labels.

The Add-document flow shows `offered_kinds` with friendly labels and asks only for required fields. Defaults are prefilled and optional fields stay collapsed. The dashboard spec owns the UI.

### 4.11 Example domain pack: quality-improvement

`examples/profiles/quality-improvement/` is a worked example of building a custom ontology from a methodology book. It is not a builtin pack and not a preset an installer selects; an owner copies it into a workspace and binds it like the existing `workbench` example (`[profiles.quality-improvement] path = "profiles/quality-improvement"`). It extends `builtin:general@1`. The pack and its README use their own generic wording of a problem-solving cycle on a production line: observe a defect, analyse its cause, find the weak point in the process, apply a countermeasure, verify it, then standardise. It quotes no book, reproduces no figure or form from one, and its fixtures are synthetic.

**Kinds.** Each kind passes the narrower-kind rule of §4.7; anything that does not is a facet.

| Kind (broader) | Lane | Context level | Required delta | Why it is a kind |
| --- | --- | --- | --- | --- |
| `part@1` (record) | E | document | `part_number` | required identifier; the anchor of every occurrence |
| `defect_occurrence@1` (record) | E | document | `part_number`, `observed_on`, `detection_stage` | required fields; one record per observed occurrence |
| `analysis@1` (record) | E | depth:1 | `target`, `method` (`five_whys`, `cause_and_effect`, `fault_tree`, `other`) | required fields; long analyses are read section by section |
| `weak_point@1` (design_record) | M | document | `process_step` | a finding about the process, kept in the maintenance lane |
| `countermeasure@1` (design_record) | M | document | `status` ∈ proposed/implemented/verified/abandoned, `process_step` | required lifecycle field |
| `standard_work@1` (procedure) | S | depth:1 | `process_step`, `revision` | becomes the rule to follow once a countermeasure is verified |

Facets, not kinds: `qi.process_step` (string, hint), `qi.severity` (`critical`, `major`, `minor`, hint) and `qi.detection_stage` (`incoming`, `in_process`, `final`, `field`, hint).

**Relations**, each with subject and object kinds matched by subsumption:
- `causes@1`: weak_point or defect_occurrence → defect_occurrence; asserted by an analysis, methods human or model, default status `proposed`;
- `occurs_on@1`: defect_occurrence → part;
- `addresses@1`: countermeasure → weak_point or defect_occurrence;
- `verifies@1`: defect_occurrence or analysis → countermeasure; evidence that the countermeasure worked or failed, with a required `outcome` attribute (`effective`, `ineffective`);
- `standardises@1`: standard_work → countermeasure.

**Identifier schemes:** part numbers and defect-occurrence numbers, both with owner-configurable prefixes.

**Modes:** `investigate` (allowed E and M, required E), `audit` (all lanes) and `lookup` (all lanes).

**Skill.** `skills/SKILL.md` states the method's reasoning order: start from the occurrence, never propose a countermeasure without a `causes` chain to a weak point, treat a countermeasure as unverified until a `verifies` assertion with outcome `effective` exists, and cite the standard work revision in force.

**How to build such a pack** (the README walks through these steps on this example):
1. List the artefacts the method produces and the questions people ask of them.
2. Map each artefact to the nearest core kind as its `broader`.
3. Apply the narrower-kind rule; turn every other difference into a facet.
4. Name the method's causal and lifecycle links as relations with subject and object kinds.
5. Choose context levels from how each artefact is read.
6. Write the skill as the method's reasoning order and its stop conditions.
7. Add a synthetic fixture corpus and a resolution test (K9).

The existing `workbench` example depends on the deleted `documents@1` pack and is ported to extend `builtin:general@1` in the same change.

## 5. Migration and compatibility

The format changes (kind `schema_version = 2`, new packs) and old workspaces are rebuilt (programme §2). The legacy packs `documents`, `general-documents`, `code` and `verified` are **deleted**, and no aliases are kept in packs.

`manage rebuild` (engine core) applies `resources/migrations/kinds-v1-to-v2.toml` once, as data. If the target is not present in the selected preset, rebuild walks the target's `broader` chain until it finds a kind that is. The map:

| Old kind | New kind |
| --- | --- |
| law, regulation | legislation |
| decision | ruling |
| standard | standard |
| guidance, manual | guidance |
| template | template |
| procedure | procedure |
| feedback | discussion |
| evidence, research_note | record |
| example | example |
| rationale | design_record |
| architecture_decision | adr |
| code_file | source_code |
| document | document |

Metadata conversion: `author` → `[author]` and `deciding_body` → `issuer`. Revisions that fail the new schema are listed in the rebuild report and skipped. They are not guessed. Old workspace files naming `general-documents@1` fail resolution with a message that names `general@1`.

## 6. Testing and verification

- **Registry unit tests:**
  - root uniqueness, two parents, unknown parent, cycle and depth;
  - an abstract kind at `sources.add` or as a collection default;
  - nearest-ancestor lanes and child overrides;
  - widening `allowed_uses`;
  - a `facet_defaults` value outside the enum;
  - a child schema that does not extend its parent;
  - identifier patterns with backreferences or nested quantifiers.
- **Schema tests.** For every preset kind, a corpus of valid and invalid instances. A Hypothesis property: any instance a child accepts, its parent's open schema also accepts.
- **Preset golden tests.** Each preset resolves, and the effective kind table matches a checked-in snapshot (K1). The registry size is checked (K3). Discovery output is checked.
- **Facets and access.** Confidentiality raises the floor and is refused above the ceiling. Filtering by `confidentiality` never widens access. `doc.kind in [normative_text@1]` returns legislation and standard units (K4).
- **Context level.** Inheritance and child override; `file` without `code_symbols` is rejected; an `article` kind whose document yields no provisions falls back to `depth:1` with `CONTEXT_LEVEL_FALLBACK`; the effective level is in the golden table (K8).
- **Shared base pack.** `regulatory@1` cannot be selected as a preset; both regulatory presets resolve `legislation@1` with the same digest; neither sees the other's kinds.
- **Localized labels.** The `fr` table of `regulatory-administrative@1` covers every offered kind, lane, mode and access rank; a missing entry falls back to the base label; changing a label leaves kind digests unchanged.
- **Example pack.** `quality-improvement` resolves against `general@1`, its synthetic corpus ingests, and a `causes` → `addresses` → `verifies` chain is returned by graph expansion (K9). The ported `workbench` example resolves.
- **Identifiers.** The same scheme extracts the same ids at ingestion and at query time. There are French article, case-number and standard-designation fixtures.
- **BibTeX.** One fixture per entry type and per alias, plus classic-only gap warnings.
- **Migration.** Each old kind maps to an existing kind in each preset. A rebuild of the fixture workspace passes the evaluation set at or above the baseline (K7).
- **SKOS.** The JSON-LD parses, and every `broader` target is a concept in the scheme.
- **Gates.** jscpd shows no schema clones (K2). The kind-literal grep finds nothing (K6).

## 7. Code-health ratchet

This sub-project must leave the code better as follows:
- one schema-subset walker instead of two;
- 16 duplicated schemas become deltas;
- the dead `default_lane` field and the duplicate `preset` dependency field are removed;
- five hard-coded kind literals and the hard-coded identifier namespaces are removed;
- `segments._lane` and `segments._role`, and the per-candidate lane SQL, are replaced by O(1) registry maps;
- `_validate_definitions` (270 lines, `registry.py:385-653`) is split into one validator per definition category, each under 60 lines and each with its own tests.

## 8. Risks and open questions

1. **Upload of confidential material.** Should registration refuse rank ≥ 2 into an upload-consented collection by default? Proposed default: yes, with `max_upload_rank` on the collection policy. Engine core owns the policy field. This reproduces the external-clearance rule of the administrative-compliance use case.
2. **"Images preset" wording.** Decided by the product owner: there is no separate images preset (§4.8). Model defaults, Docling enrichment and chunking v2 now attach their image settings to `visual@1` and to the online tier of every preset.
3. **Package and dependency units.** They need a manifest normalizer and new extraction semantics. Proposed default: defer to a later language-pack sub-project.
4. **Facets on `issue@1` and `pull_request@1`.** Should `state` be a facet? Proposed default: yes, `issue.state` with no hint. It is cheap and it is filterable.
5. **Pattern safety.** Owner-authored identifier patterns run on the owner thread. Proposed default: the static check plus input bounds. Use the `regex` module with timeouts only if fuzzing finds a pathological pattern that passes the check.

## 9. Flow-review findings

Closed:
- D7, partly: lane and role lookups no longer dump the configuration (`segments.py:56-79`). The registry hot-path serialization itself is engine core's.
- §4.3 per-candidate lane SELECT (`query.py:1036`).
- §4.4 `segments._lane`/`_role`.

Deferred to engine core:
- C4: the stop-word mechanism. Presets supply `doc.language` so the mechanism can pick a list.
- Q6: strict defaults.

## Appendix: vocabulary verification (2026-10-15)

- **schema.org:** checked in the current release JSON-LD. Verified: `CreativeWork`, `Legislation`, `LegalForceStatus` (with `InForce`, `PartiallyInForce`, `NotInForce`), `legislationAmends`, `legislationTransposes`, `TechArticle`, `HowTo`, `DigitalDocument`, `Comment`, `SoftwareSourceCode`, `ImageObject`, `ScholarlyArticle`, `Book`, `Thesis`, `Report`, `Dataset`, `Manuscript`, `WebPage`.
- **DCMI:** checked in the Type and Terms Turtle. Verified: `dcmitype:Text`, `StillImage`, `Software`, `Dataset`; `dcterms:replaces`.
- **FaBiO:** checked in the RDF. Verified: `Work`, `ScholarlyWork`, `StandardOperatingProcedure`, `TechnicalReport`, `ConferencePaper`, `ConferenceProceedings`, `InstructionManual`, `Patent`. `fabio:Template` does **not** exist; the research note's claim was wrong, and it is not used here.
- **CiTO:** checked in the HTML. Verified: `cites`, `supports`, `disagreesWith`, `reviews`, `discusses`.
- **SKOS:** checked in the W3C RDF. All terms in §4.9 verified.
- **ELI:** `LegalResource`, `amends` and `transposes` verified only against the ELI 2.0 XML-schema documentation (data2type). The OWL file could not be retrieved.
- **ECLI:** structure verified on e-justice. The court-code length is unverified.
- **BibTeX and BibLaTeX:** BibLaTeX 3.22a §2.1.1–2.1.2 and BibTeX `btxdoc` verified. bibtex.com lists the 14 BibTeX types but no required fields.
