# Contributing

Start by identifying whether the change affects a public contract, retrieval behavior, a parser, a provider, or documentation. Keep examples synthetic and domain-neutral. Include a reproducible failure and the expected evidence or error when reporting a bug.

## Development environment

Use Python 3.12 and the locked dependencies from a checkout:

```bash
uv sync --locked --all-extras --group dev --group test-fixtures
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check src tests
```

The full stack contains large native and document dependencies. Tests involving parsers and token budgets require provisioned assets even when they are outside the `qualification` directory. Missing assets currently fail several tests; do not interpret those failures as retrieval regressions or silently suppress them.

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

## Documentation and publication

Run the [strict documentation build](documentation.md). Verify command examples and mark proposed capabilities as proposals. Use the [review assessment](review-assessment.md) for known implementation gaps; an existing test passing does not prove a specification requirement is implemented correctly.

No remote repository address is assumed by the checkout. After a GitHub repository is configured, use its issue tracker and pull requests for public discussion. Do not include credentials, local database contents, private conversations, personal filesystem paths or confidential examples in issues, commits or screenshots.

The project is MIT licensed. Dependency licenses and native artifact qualification remain separate from the project license. The recorded resolved-stack security audit is nonclean; see [security](security.md) before claiming a clean release.
