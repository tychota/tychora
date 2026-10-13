# Evidence you can inspect.

<div class="tychora-hero" markdown>

<p class="tychora-eyebrow">Local knowledge, with a clear trail</p>

Tychora registers versioned sources, retrieves evidence within a workspace's scope, and returns citations and coverage that you can inspect. It is a retrieval engine for applications and agents that need to show where their context came from.

[Get started](getting-started.md){ .md-button .md-button--primary } [See the architecture](architecture.md){ .md-button }

</div>

## From source to evidence

<div class="tychora-steps" markdown>

1. **Register a source.** Capture its identity, version, metadata, and allowed uses.
2. **Build retrieval channels.** Index the source into the channels selected by the workspace configuration.
3. **Search within scope.** Retrieve eligible evidence with source and revision lineage.
4. **Inspect the result.** Follow citations to exact evidence and review coverage or unavailable channels.

</div>

Tychora exposes the same workspace model through its [Python API](api.md), [CLI](cli.md), [loopback HTTP API](http.md), and [MCP transport](mcp.md). Start with the [offline example](getting-started.md), then explore [workspaces](workspaces.md) and [configuration](configuration.md).

!!! note "Current scope"
    Tychora returns evidence, not a generated answer. Optional extraction, indexing, and remote provider capabilities require explicit setup. See [qualification](qualification.md) for tested boundaries and [ingestion roadmap](ingestion-roadmap.md) for potential additions.
