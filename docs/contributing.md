# Contributing

Start by identifying whether the change affects a public contract, retrieval behavior, a parser, a provider, or documentation. Keep examples synthetic and domain-neutral. Include a reproducible failure and the expected evidence or error when reporting a bug.

## Development environment

Use Python 3.12 and the locked dependencies from a checkout:

```bash
uv sync --locked --all-extras --group dev --group test-fixtures
uv run --locked python -m tools.health check --fast
```

`check --fast` runs lint, format, types, the layering contracts and the plan-reference rule in a few seconds. `uv run --locked python -m tools.health check` runs every pull-request gate; it needs the reports of a full test run (`reports/`), so it normally runs in CI. When a metric improves, run `uv run --locked python -m tools.health update` and commit `quality/baseline.json` with the change. A hand edit that makes a baseline value worse needs a one-line justification in the pull request.

The full stack contains large native and document dependencies. Tests involving parsers and token budgets require provisioned assets even when they are outside the `qualification` directory. Tests that need provisioned assets declare them with `@pytest.mark.assets(...)` and are skipped with the variable names when those are unset; qualification and release tests fail instead.

## Test assets and tests

The CI provisioning script explicitly downloads pinned public assets and verifies their hashes. It does not upload source documents. Choose separate disposable directories:

```bash
python packaging/provision_ci_assets.py \
  --cache "$PWD/.cache/test-models" \
  --workspace "$PWD/.cache/asset-setup" \
  --github-env "$PWD/.cache/test-assets.env"
set -a
. .cache/test-assets.env
set +a
uv run --locked pytest tests --ignore=tests/release -m 'not live' -q
```

The script accepts `--bundle PATH` for an already provisioned local bundle. Live provider tests are a separate opt-in; never point them at user corpora as a convenience. The code-3 tokenizer research asset is not supplied by the standard provisioning selection and may be reported as a named skip.

Use focused regression tests while editing. Before proposing a runtime change, run the relevant integration and contract suites and report the precise scope, model assets, skipped cases and failures. Installed-wheel and native-executable qualification have their own instructions in [distribution](distribution.md).

## Comments and docstrings

Comments and docstrings say what the code does and why, in present tense, for a reader who has not seen any plan or review. They state intent, units, invariants or reasons that the code cannot show. They never cite plan tasks, plan file names or review identifiers; `python -m tools.health check` fails on a reference such as "Task 7".

Two rewrites from this code base:

- `publication_manifest` said "Exact Task 7 typed retention header for a complete snapshot plan." It now says "Canonical bytes of the snapshot manifest, the typed retention header of a complete plan."
- A reranking check said "Bind every rendered span and dependency to Task19's canonical ownership." It now says "Check that every rendered span and dependency belongs to the canonical owner of its extraction."

Every public class, function and method of `tychora.api` and `tychora.clients` has a docstring, because [the API reference](api.md) documents that surface and editors show it.

## Documentation and publication

Run the [strict documentation build](documentation.md). Verify command examples and mark proposed capabilities as proposals. Use the [review assessment](review-assessment.md) for known implementation gaps; an existing test passing does not prove a specification requirement is implemented correctly.

No remote repository address is assumed by the checkout. After a GitHub repository is configured, use its issue tracker and pull requests for public discussion. Do not include credentials, local database contents, private conversations, personal filesystem paths or confidential examples in issues, commits or screenshots.

The project is MIT licensed. Dependency licenses and native artifact qualification remain separate from the project license. The recorded resolved-stack security audit is nonclean; see [security](security.md) before claiming a clean release.
