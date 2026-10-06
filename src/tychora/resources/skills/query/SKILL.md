---
name: query
description: Use when searching a Tychora workspace for cited evidence, especially with unfamiliar profile kinds, filters, partial coverage, or restricted results.
---

# Query evidence

In an existing authorized workspace, discover the selected profile's kinds, facets, relations, settings and visible source coverage before constructing a filtered `QueryRequest`. `search --request-file` accepts the public request when filters, mode, dates, scope or budgets matter; validate those fields against discovery rather than hardcoding a full kind enum. Owner-opening discovery can create a missing workspace, so read-only preparation without an existing target stops at static files/help.

```bash
tychora --workspace ./knowledge inspect kinds --json
tychora --workspace ./knowledge inspect facets --json
tychora --workspace ./knowledge search "verification" --json
```

Treat `QueryResult` as evidence, not an answer: retain run/snapshot, source/revision/unit IDs, citation locators, segment kind/role, `coverage`, gaps, and truncation. Inspect relevant exact evidence and revision/source lineage before a substantive claim. Keep a readable derived unit's derivation and original-removed status distinct; an unavailable original cannot be quoted or implied to remain retrievable. Do not promote feedback or a derived proxy into an approved rule.

A partial or unavailable channel limits what this run found; it does not reveal hidden record counts or authorize a broader scope. More channels are not a proof of completeness: a universal statement needs a defined population, time and authorized scope, plus inspectable supporting records/checks for every member and no unresolved source/eligibility gaps. A semantic channel is neither required nor sufficient by itself. If the supplied evidence cannot support the claim, state the narrower cited finding and the precise missing evidence. Never invent a citation, missing provider output, or a restricted record from a coworker's recollection.
