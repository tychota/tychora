# Generic workbench pack

This data-only external pack depends on the version-qualified bundled `documents@1` pack. The dependency supplies the regulation, standard, template and feedback kinds, their schemas, and distinct authority, standard and feedback roles and lanes. This pack adds `clarifies@1`, declares a versioned `legacy-standard@1` definition and historical lane mapping, explicitly binds `scope.project` to source metadata, and selects hierarchical evidence output. A recorded clarification requires actual typed endpoints and support; naming the predicate never creates a relation or promotes feedback into a rule.

Copy this entire directory to `knowledge/profiles/workbench`, then put a separate workspace configuration at `knowledge/workspace.toml`:

```toml
schema_version = 1
[profiles.workbench]
path = "profiles/workbench"
[defaults]
profile = "workbench"
```

Run `tychora --workspace ./knowledge --offline init --provider configured --config ./knowledge/workspace.toml`. Then use `inspect kinds --json`, `inspect facets --json`, and `inspect relations --json` to see the resolved registry. With an absolute acquisition root configured in `[configuration.acquisition]`, a source with schema-valid `metadata.scope.project` is filterable. A missing project value matches a project filter only when its source metadata explicitly declares `scope.global_facets = ["project"]`, the registry allows it, and the query requests `include_global`. A child value can narrow the source declaration. This does not change scope, dates, review state, or source selection.

The historical lane mapping assigns retained `legacy-standard@1` evidence to the standard presentation lane and preserves its original versioned schema. The example pack also registers that versioned definition so it can validate in a fresh workspace; production upgrades must retain exact old definitions and schema digests rather than inventing replacements. The default output groups cited evidence by lane; grouping does not grant authority or supply an answer.
