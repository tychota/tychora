# Documentation

The public site is built with Material for MkDocs. Its source is the Markdown in `docs/`, and `mkdocs.yml` defines the navigation. The `docs/superpowers/` planning archive is excluded from the published site because it records historical proposals rather than the current supported behavior. Keep current guides tied to implemented, tested interfaces and link to [qualification](qualification.md) when a capability has limits.

## Build locally

Documentation tooling has its own lockfile in `packaging/docs/`, separate from Tychora's runtime dependencies. From the repository root:

```bash
uv run --project packaging/docs --locked mkdocs build --strict
uv run --project packaging/docs --locked mkdocs serve
```

The first command writes the site to `site/`; the second serves it locally for review. The build runs on pull requests and pushes. The `Documentation` GitHub Actions workflow publishes only when explicitly started through **Run workflow** with **deploy** selected. Set the repository's Pages source to **GitHub Actions** before that first deployment. For a deployment build, the workflow gets the site's actual base URL from GitHub Pages and passes it to MkDocs, including any repository subpath. Local and review builds keep relative links.

## Edit a guide

Choose the page closest to the reader's task. Put a tested command or API example near the behavior it explains, and distinguish a configured capability from one that is actually available in a given installation. Describe returned evidence, source lineage, and coverage in user-facing terms. For contributor workflow and checks, see [Contributing](contributing.md).

For a new guide, add its Markdown file to `docs/` and place it in `mkdocs.yml` navigation. Link to other site pages by their `.md` paths. Mermaid diagrams use fenced `mermaid` blocks. Keep images in `docs/assets/` and use descriptive alternative text. Fonts use the system stack and no analytics are configured. Material's built-in diagram integration loads Mermaid from its upstream CDN on pages containing diagrams; the site does not claim fully offline diagram rendering. Run the strict build and inspect the rendered page at a narrow and a wide viewport before proposing a change.

The warm ivory, forest green, and copper palette makes source trails easy to scan while keeping long technical pages quiet. Material provides the responsive navigation, search, and code display; the custom stylesheet only adjusts the brand and landing page. The emblem depicts a source page with a connected evidence point, and the landing illustration is decorative. Both are local assets.

## What makes these docs useful

Readers arrive with different tasks: trying the product, operating an existing workspace, understanding a design choice, or checking an exact interface. Keep those routes distinct. A first-run page should lead to one observable result; an API reference should describe inputs, outputs and errors; an explanation should justify a trade-off. Link between them rather than making each page teach everything.

Our practical checks are: a working copy/paste example; explicit prerequisites; searchable task-based navigation; current limitations next to affected features; stable names; meaningful diagrams; and a strict build that catches broken links. An attractive landing page cannot compensate for a command that does not work. Treat examples and documentation fixes as normal contributions.

The sources below informed this structure. They are references, not text copied into the site:

| Source | Applied lesson |
| --- | --- |
| [Google Open Source reference](https://opensource.google/documentation/reference) | Review what leaves a private working context; public documentation needs deliberate publication boundaries |
| [The Good Docs Project templates](https://www.thegooddocsproject.dev/template) | Give installation, quickstart, how-to, reference and troubleshooting distinct purposes |
| [Write the Docs guide](https://www.writethedocs.org/guide/) and [beginner guidance](https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/) | Explain the problem, provide a short working example, and make contribution/support paths findable |
| [UC Berkeley documentation guide](https://guides.lib.berkeley.edu/how-to-write-good-documentation) | Include installation, examples, API documentation, licensing and reproducibility information |
| [2023 HN documentation discussion](https://news.ycombinator.com/item?id=34820382) | Use the discussion as a source of examples, not as evidence that one theme guarantees quality |
| [Django documentation](https://docs.djangoproject.com/en/5.2/), [PostgreSQL manual](https://www.postgresql.org/docs/current/index.html), [Divio documentation system](https://docs.divio.com/documentation-system/) | Separate introductory learning, task guides, reference and explanation; connect conceptual and implementation detail |
| [MkDocs](https://www.mkdocs.org/) and [Material](https://squidfunk.github.io/mkdocs-material/) | Keep editable Markdown, search and responsive navigation with a reproducible static build |

These references were checked on 2026-10-13. The site describes the alpha implementation; the excluded planning archive is not a substitute for current reference material.

## Accuracy audit

Run this checklist once at the end of every sub-project, and whenever a page claims a number:

1. Every claim with a number (tests, sizes, versions, durations) is measured again or removed.
2. Every "not implemented", "missing" or "planned" statement is checked against the code.
3. Every file and line reference resolves.
4. Every command block runs. `tests/docs/test_examples.py` executes the README and example recipes and checks the command blocks of `HANDOFF.md` and `docs/contributing.md` statically (paths exist, tools are installed, markers are registered); run the blocks by hand as well.
5. A page that names the commit it describes is updated to the current commit, or the pin is removed.
