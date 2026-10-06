---
name: add-sources
description: Use when adding a file, folder, repository, or inline text to a Tychora workspace and the source kind, metadata, acquisition path, or grant needs validation.
---

# Add a source

Use an existing authorized workspace to discover `kinds`, their metadata schemas, visible sources, settings and collection scope before choosing a `SourceInput`. `inspect kinds` opens the owner and may create a missing workspace; in read-only preparation, confirm the workspace exists or inspect pack files and CLI help without opening it. A pack name or a user's label such as “policy” is not proof of kind, authority, approval, or a registered capability. Ask for the missing classification and only the fields required by its discovered schema rather than guessing. Do not impose manual/standard publisher and edition fields on feedback; its schema has different required fields and permits explicit nulls for some unknown values. Keep feedback, templates, standards and regulations in their distinct discovered roles.

A CLI file add requires the real path beneath an absolute configured acquisition root, a registered kind, schema-valid metadata and a granted collection:

```bash
tychora --workspace ./knowledge inspect kinds --json
tychora --workspace ./knowledge add ./manual.md --kind manual@1 --metadata-file ./manual.metadata.json --json
```

For inline text, submit a complete public `SourceInput` through `add --request-file source.json` or `kb.sources.add(...)`; no filesystem acquisition root is needed for that text. The `collection_id: "default"` mapping alias resolves only if the caller has that collection. A provider setting or credential does not authorize upload. Do not widen a collection or caller scope to make an add succeed; report the missing grant or request the owner-authorized consent operation separately.

`add` returns a durable job. Inspect or wait for its terminal state and publication receipt, then discover source/revision lineage and inspect returned evidence before claiming it is searchable. Preserve source, revision, unit, citation and original/derived status; a derived record or reviewer feedback is not an approved rule. A failed or incomplete optional stage must be reported as such.
