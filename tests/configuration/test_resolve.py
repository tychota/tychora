import hashlib
import json
import shutil
import tomllib
from pathlib import Path

import pytest

from tychora.configuration.resolve import merge_values
from tychora.contracts.errors import TychoraError


def test_explicit_false_zero_and_empty_list_survive():
    result = merge_values(
        {"graph": True, "bonus": 1, "lanes": ["a"]},
        {"graph": False, "bonus": 0, "lanes": []},
    )
    assert result == {"graph": False, "bonus": 0, "lanes": []}


@pytest.fixture
def workspace(tmp_path):
    packs = Path(__file__).parents[1] / "fixtures" / "packs"
    shutil.copytree(packs, tmp_path / "packs")
    # Independent digest construction over the complete explicitly included manifest.
    base = tmp_path / "packs/base"
    files = {
        str(path.relative_to(base)): (
            json.loads(path.read_text())
            if path.suffix == ".json"
            else tomllib.loads(path.read_text())
        )
        for path in base.rglob("*")
        if path.is_file()
    }
    encoded = json.dumps(
        ["profile-pack-v1", files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    sha = hashlib.sha256(encoded).hexdigest()
    child = tmp_path / "packs/child/profile.toml"
    assert tomllib.loads(child.read_text())["extends"][0]["digest"] == sha
    file = tmp_path / "tychora.toml"
    file.write_text("""schema_version = 1
[profiles.base]
path = "packs/base"
[profiles.child]
path = "packs/child"
[defaults]
profile = "child"
[configuration.output]
title = "Workspace evidence"
""")
    return file


def test_resolution_order_origins_and_complete_immutable_artifact(workspace):
    from tychora.configuration.resolve import resolve_config

    config = resolve_config(workspace, "child", {"limits": {"max_tokens": 2000}})
    assert config.values.output.title == "Workspace evidence"
    assert config.values.output.include_empty_lanes is False
    assert config.values.limits.max_tokens == 2000
    assert config.registry["lanes"]["reference"]["label"] == "Selected references"
    assert (
        config.registry["kinds"]["note@1"]["description"]
        == "An attributed reference note."
    )
    assert config.registry["schemas"]["note-metadata@1"]["required"] == ("author",)
    assert [pack.id for pack in config.packs] == ["fixture-base", "fixture-child"]
    assert config.origins["limits.max_tokens"]["layer"] == "request"
    assert config.origins["output.title"]["layer"] == "workspace"
    assert config.origins["lanes.reference.label"]["layer"] == "pack:fixture-child@1"
    assert len(config.config_sha) == len(config.registry_digest) == 64
    with pytest.raises(TypeError):
        config.registry["kinds"]["note@1"]["label"] = "changed"
    assert config == resolve_config(
        workspace, "child", {"limits": {"max_tokens": 2000}}
    )


def test_dependency_cycle_is_reported(workspace):
    from tychora.configuration.resolve import resolve_config

    base = workspace.parent / "packs/base/profile.toml"
    base.write_text(
        base.read_text().replace(
            'version = "1"',
            'version = "1"\nextends = [{id="fixture-child", version="1", digest="'
            + "0" * 64
            + '"}]',
        )
    )
    with pytest.raises(TychoraError, match="cycle"):
        resolve_config(workspace, "child", {})


def test_immutable_versioned_definition_collision(workspace):
    from tychora.configuration.resolve import resolve_config

    root = workspace.parent / "packs/child"
    shutil.copytree(root.parent / "base/schemas", root / "schemas")
    (root / "note.toml").write_text(
        (root.parent / "base/kinds/note.toml")
        .read_text()
        .replace("An attributed reference note.", "Different meaning.")
    )
    file = root / "profile.toml"
    file.write_text(
        file.read_text().replace(
            'version = "1"\n', 'version = "1"\nkind_definitions = ["note.toml"]\n', 1
        )
    )
    with pytest.raises(TychoraError, match="immutable"):
        resolve_config(workspace, "child", {})


@pytest.mark.parametrize("symlink", [False, True])
def test_includes_cannot_escape_registered_root(workspace, symlink):
    from tychora.configuration.resolve import resolve_config

    root = workspace.parent / "packs/child"
    if symlink:
        (root / "outside.toml").symlink_to(root.parent / "base/kinds/note.toml")
        include = "outside.toml"
    else:
        include = "../base/kinds/note.toml"
    file = root / "profile.toml"
    file.write_text(
        file.read_text().replace(
            'version = "1"\n', f'version = "1"\nkind_definitions = ["{include}"]\n', 1
        )
    )
    with pytest.raises(TychoraError, match="contained"):
        resolve_config(workspace, "child", {})


def test_false_filter_override_is_conjoined_with_configured_filter(workspace):
    from tychora.configuration.resolve import resolve_config

    workspace.write_text(
        workspace.read_text()
        + """
[configuration.filters]
op = "eq"
field = "scope.project"
value = "demo"
"""
    )
    config = resolve_config(
        workspace,
        "child",
        {"filters": {"op": "eq", "field": "metadata.archived", "value": False}},
    )
    assert config.values.filters.model_dump(mode="json") == {
        "op": "and",
        "children": [
            {"op": "eq", "field": "scope.project", "value": "demo"},
            {"op": "eq", "field": "metadata.archived", "value": False},
        ],
    }


@pytest.mark.parametrize(
    "layer, text, override",
    [
        ("workspace", "\n[configuration.retrieval]\nunknown_option = true\n", {}),
        ("workspace", "\n[configuration]\nunknown_option = true\n", {}),
        ("pack", "\n[output]\nunknown_option = true\n", {}),
        ("request", "", {"offline": False}),
        ("request", "", {"limits": []}),
        ("request", "", {"limits": None}),
        ("request", "", {"profiles": {"other": {"path": "/private/data"}}}),
    ],
)
def test_unknown_or_forbidden_options_fail_safely(workspace, layer, text, override):
    from tychora.configuration.resolve import resolve_config

    if layer == "workspace":
        # Existing output table stays valid when opening its parent table later.
        workspace.write_text(workspace.read_text() + text)
    elif layer == "pack":
        file = workspace.parent / "packs/child/profile.toml"
        file.write_text(file.read_text() + "\nunknown_option = true\n")
    with pytest.raises(TychoraError) as error:
        resolve_config(workspace, "child", override)
    assert error.value.code == "INVALID_REQUEST"
    assert str(workspace.parent) not in str(error.value)


@pytest.mark.parametrize(
    "configuration",
    [
        '[configuration.index.text]\nbackend = "chroma"',
        '[configuration.providers.text]\nprovider = "voyage"',
        '[configuration.index.image]\nbackend = "chroma"\n[configuration.providers.text]\nprovider = "voyage"\nmodel = "voyage-context-4"',
    ],
)
def test_invalid_provider_index_combinations(workspace, configuration):
    from tychora.configuration.resolve import resolve_config

    workspace.write_text(workspace.read_text() + "\n" + configuration)
    with pytest.raises(TychoraError, match="provider|model|offline"):
        resolve_config(workspace, "child", {})


def test_missing_registered_root_binding(workspace):
    from tychora.configuration.resolve import resolve_config

    with pytest.raises(TychoraError, match="registered"):
        resolve_config(workspace, "unregistered", {})


@pytest.mark.parametrize(
    "limits", [{"max_tokens": 3001}, {"limit": 9}, {"semantic_hops": 3}]
)
def test_request_cannot_raise_profile_ceilings(workspace, limits):
    from tychora.configuration.resolve import resolve_config

    with pytest.raises(TychoraError, match="ceiling"):
        resolve_config(workspace, "child", {"limits": limits})


@pytest.mark.parametrize("change", ["digest", "unresolved", "duplicate_pack"])
def test_dependency_identity_is_verified(workspace, change):
    from tychora.configuration.resolve import resolve_config

    if change == "duplicate_pack":
        shutil.copytree(
            workspace.parent / "packs/base", workspace.parent / "packs/duplicate"
        )
        file = workspace.parent / "packs/duplicate/profile.toml"
        file.write_text(
            file.read_text().replace("Reference evidence", "Conflicting pack")
        )
        workspace.write_text(
            workspace.read_text() + '\n[profiles.duplicate]\npath="packs/duplicate"\n'
        )
    else:
        file = workspace.parent / "packs/child/profile.toml"
        text = file.read_text()
        if change == "digest":
            original = tomllib.loads(text)["extends"][0]["digest"]
            text = text.replace(original, "0" * 64)
        else:
            text = text.replace("fixture-base", "absent-base")
        file.write_text(text)
    with pytest.raises(TychoraError, match="digest|unresolved|Conflicting"):
        resolve_config(workspace, "child", {})


@pytest.mark.parametrize(
    "change", ["lane", "rendering", "parsing", "embedding", "mode"]
)
def test_stage_fingerprints_track_their_semantic_inputs(workspace, change):
    from tychora.configuration.resolve import resolve_config

    before = resolve_config(workspace, "base", {})
    file = workspace.parent / "packs/base/profile.toml"
    text = file.read_text()
    if change == "lane":
        text = text.replace('label = "References"', 'label = "Source references"')
    elif change == "rendering":
        text = text.replace("include_empty_lanes = true", "include_empty_lanes = false")
    elif change == "parsing":
        text += '\n[extraction]\nrepresentation_version = "2"\n'
    elif change == "embedding":
        text += '\n[providers.text]\nrepresentation_version = "2"\n'
    else:
        text = text.replace("accepted_only", "original_only")
    file.write_text(text)
    after = resolve_config(workspace, "base", {})
    assert after.config_sha != before.config_sha
    assert after.query_fingerprint != before.query_fingerprint
    assert (after.extraction_fingerprint != before.extraction_fingerprint) is (
        change == "parsing"
    )
    assert (after.index_fingerprint != before.index_fingerprint) is (
        change == "embedding"
    )
    assert (after.eligibility_fingerprint != before.eligibility_fingerprint) is (
        change == "mode"
    )
    assert (after.output_fingerprint != before.output_fingerprint) is (
        change in {"lane", "rendering"}
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"$id": "note-metadata@1", "$ref": "https://example.invalid/schema.json"},
        {"$id": "note-metadata@1", "$ref": "missing.json"},
        {"$id": "note-metadata@1", "type": "string", "pattern": ".*"},
        {"$id": "note-metadata@1", "type": "array", "items": {"type": "string"}},
        {"$id": "note-metadata@1", "type": "unknown"},
        {"$id": "note-metadata@1", "$ref": "#"},
        {
            "$id": "note-metadata@1",
            "type": "object",
            "$defs": {"nested": {"type": "string"}},
            "$ref": "#/$defs/absent",
        },
    ],
)
def test_schema_subset_and_references_are_bounded_local_and_resolved(workspace, schema):
    from tychora.configuration.resolve import resolve_config

    file = workspace.parent / "packs/base/schemas/note.json"
    file.write_text(json.dumps(schema))
    with pytest.raises(TychoraError, match="schema"):
        resolve_config(workspace, "base", {})


@pytest.mark.parametrize("selection", ["preset", "binding", "shorthand"])
def test_builtin_preset_precedes_dependencies_and_uses_bundled_digest(
    workspace, monkeypatch, selection
):
    from tychora.configuration import resolve

    package = workspace.parent / "package"
    builtin = package / "profiles/preset"
    shutil.copytree(workspace.parent / "packs/base", builtin)
    file = builtin / "profile.toml"
    file.write_text(
        file.read_text()
        .replace("fixture-base", "preset")
        .replace("Reference evidence", "Preset evidence")
    )
    files = {
        str(path.relative_to(builtin)): (
            json.loads(path.read_text())
            if path.suffix == ".json"
            else tomllib.loads(path.read_text())
        )
        for path in builtin.rglob("*")
        if path.is_file()
    }
    encoded = json.dumps(
        ["profile-pack-v1", files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    sha = hashlib.sha256(encoded).hexdigest()
    (package / "profiles/manifest.json").write_text(
        json.dumps({"preset@1": {"path": "preset", "digest": sha}})
    )
    monkeypatch.setattr("importlib.resources.files", lambda package_name: package)
    child = workspace.parent / "packs/child/profile.toml"
    if selection == "binding":
        workspace.write_text(
            workspace.read_text() + '\n[profiles.predefined]\nbuiltin="preset@1"\n'
        )
        selected = "predefined"
    elif selection == "shorthand":
        child.write_text(
            child.read_text().replace("extends = [", 'extends = ["builtin:preset@1", ')
        )
        selected = "child"
    else:
        child.write_text(
            child.read_text().replace(
                'version = "1"\n', 'version = "1"\npreset = "builtin:preset@1"\n', 1
            )
        )
        selected = "child"
    config = resolve.resolve_config(workspace, selected, {})
    expected = (
        ["preset"]
        if selection == "binding"
        else ["preset", "fixture-base", "fixture-child"]
    )
    assert [pack.id for pack in config.packs] == expected
    assert config.packs[0].digest == sha
    if selection != "binding":
        assert config.origins["limits.max_tokens"]["layer"] == "pack:fixture-child@1"
    assert config.values.output.title == "Workspace evidence"


def test_inspection_redacts_private_paths_and_secret_references(workspace):
    from tychora.configuration.resolve import resolve_config

    workspace.write_text(
        workspace.read_text()
        + '\n[configuration.providers.text]\ncredential_ref="env:PRIVATE_PROVIDER_KEY"\n'
    )
    config = resolve_config(workspace, "child", {})
    public = config.inspect()
    private = config.inspect(owner_authorized=True)
    public_json = json.dumps(dict(public), default=dict)
    assert str(workspace.parent) not in public_json
    assert "PRIVATE_PROVIDER_KEY" not in public_json
    assert private["origins"]["output.title"]["source"] == str(workspace.resolve())
    assert public["values"]["output"]["title"] == "Workspace evidence"
    assert public["config_sha"] == config.config_sha


@pytest.mark.parametrize("override", [{"mode": "absent"}, {"output_profile": "absent"}])
def test_selected_names_must_exist_in_resolved_profile(workspace, override):
    from tychora.configuration.resolve import resolve_config

    with pytest.raises(TychoraError, match="mode|output"):
        resolve_config(workspace, "child", override)


def test_requested_output_profile_and_default_mode_are_resolved(workspace):
    from tychora.configuration.resolve import resolve_config

    file = workspace.parent / "packs/child/profile.toml"
    file.write_text(
        file.read_text()
        + '\n[output_profiles.compact]\ninclude_empty_lanes=false\ntitle="Compact evidence"\n'
    )
    config = resolve_config(
        workspace,
        "child",
        {"output_profile": "compact", "limits": {"graph_work_limit": 0}},
    )
    assert config.values.mode == "lookup"
    assert config.values.output.title == "Compact evidence"
    assert config.values.limits.graph_work_limit == 0


def test_public_mapping_boundary_accepts_immutable_primitive_mappings(workspace):
    from types import MappingProxyType

    from tychora.configuration.resolve import resolve_config

    overrides = MappingProxyType({"limits": MappingProxyType({"graph_work_limit": 0})})
    assert (
        resolve_config(workspace, "child", overrides).values.limits.graph_work_limit
        == 0
    )


@pytest.mark.parametrize("change", ["unversioned", "duplicate"])
def test_definition_identity_is_checked_within_each_pack(workspace, change):
    from tychora.configuration.resolve import resolve_config

    root = workspace.parent / "packs/base"
    definition = root / "kinds/note.toml"
    if change == "unversioned":
        definition.write_text(definition.read_text().replace("note@1", "note"))
    else:
        (root / "kinds/other.toml").write_text(
            definition.read_text().replace(
                "An attributed reference note.", "Conflicting text."
            )
        )
        file = root / "profile.toml"
        file.write_text(
            file.read_text().replace(
                '["kinds/note.toml"]', '["kinds/note.toml", "kinds/other.toml"]'
            )
        )
    with pytest.raises(TychoraError, match="versioned|immutable"):
        resolve_config(workspace, "base", {})


@pytest.mark.parametrize("change", ["file_size", "schema_depth"])
def test_local_configuration_loading_is_resource_bounded(workspace, change):
    from tychora.configuration.resolve import resolve_config

    root = workspace.parent / "packs/base"
    if change == "file_size":
        file = root / "profile.toml"
        file.write_text(file.read_text() + "\n#" + "x" * 1048576)
    else:
        schema = {"type": "string"}
        for _ in range(34):
            schema = {"type": "object", "properties": {"nested": schema}}
        schema["$id"] = "note-metadata@1"
        (root / "schemas/note.json").write_text(json.dumps(schema))
    with pytest.raises(TychoraError, match="bounded|schema"):
        resolve_config(workspace, "base", {})


def test_schema_file_bindings_survive_registry_replay(workspace):
    from tychora.configuration.models import ResolvedConfig
    from tychora.configuration.resolve import resolve_config

    config = resolve_config(workspace, "child", {})
    replay = ResolvedConfig.model_validate_json(config.model_dump_json())
    assert (
        replay.registry["schema_bindings"]["fixture-base@1"]["schemas/note.json"]
        == "note-metadata@1"
    )
    assert replay == config


def test_kind_schema_paths_are_explicit_contained_includes(workspace):
    from tychora.configuration.resolve import resolve_config

    file = workspace.parent / "packs/base/profile.toml"
    file.write_text(
        file.read_text().replace('schema_definitions = ["schemas/note.json"]\n', "")
    )
    config = resolve_config(workspace, "base", {})
    assert config.registry["schemas"]["note-metadata@1"]["type"] == "object"
    assert (
        config.registry["schema_bindings"]["fixture-base@1"]["schemas/note.json"]
        == "note-metadata@1"
    )


def test_offline_retains_model_index_identity_without_constructing_clients(
    workspace, monkeypatch
):
    import builtins

    from tychora.configuration.resolve import resolve_config

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"voyageai", "chromadb", "ladybug", "tiktoken"}:
            raise AssertionError(
                "Configuration inspection must not import runtime clients"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    configuration = """
[configuration]
offline = false
[configuration.providers.text]
provider = "voyage"
model = "voyage-context-4"
[configuration.index.text]
backend = "chroma"
"""
    workspace.write_text(workspace.read_text() + configuration)
    online = resolve_config(workspace, "child", {})
    workspace.write_text(
        workspace.read_text().replace("offline = false", "offline = true")
    )
    offline = resolve_config(workspace, "child", {})
    assert offline.values.providers.text.model == "voyage-context-4"
    assert offline.values.index.text.backend == "chroma"
    assert offline.index_fingerprint == online.index_fingerprint
    assert offline.query_fingerprint != online.query_fingerprint
    assert offline.values.offline is True


def test_definition_schema_bindings_are_explicit_and_immutable(workspace):
    from tychora.configuration.resolve import resolve_config

    config = resolve_config(workspace, "child", {})
    assert (
        config.registry["definition_schema_bindings"]["kinds"]["note@1"][
            "metadata_schema"
        ]
        == "note-metadata@1"
    )
    root = workspace.parent / "packs/child"
    shutil.copytree(root.parent / "base/schemas", root / "schemas")
    (root / "schemas/note.json").write_text(
        (root / "schemas/note.json")
        .read_text()
        .replace("note-metadata@1", "other-metadata@1")
    )
    (root / "note.toml").write_text((root.parent / "base/kinds/note.toml").read_text())
    file = root / "profile.toml"
    file.write_text(
        file.read_text().replace(
            'version = "1"\n', 'version = "1"\nkind_definitions = ["note.toml"]\n', 1
        )
    )
    with pytest.raises(TychoraError, match="immutable"):
        resolve_config(workspace, "child", {})


def test_pack_identity_only_change_does_not_change_extraction(workspace):
    from tychora.configuration.resolve import resolve_config

    before = resolve_config(workspace, "base", {})
    file = workspace.parent / "packs/base/profile.toml"
    file.write_text(
        file.read_text().replace('id = "fixture-base"', 'id = "renamed-base"')
    )
    after = resolve_config(workspace, "base", {})
    assert after.extraction_fingerprint == before.extraction_fingerprint


@pytest.mark.parametrize(
    "binding", ['path="packs/base"\nbuiltin="absent@1"', "", 'builtin="absent@1"']
)
def test_builtin_binding_is_explicit_exclusive_and_registered(workspace, binding):
    from tychora.configuration.resolve import resolve_config

    workspace.write_text(
        workspace.read_text() + "\n[profiles.invalid]\n" + binding + "\n"
    )
    with pytest.raises(TychoraError) as error:
        resolve_config(workspace, "invalid", {})
    assert error.value.code == "INVALID_REQUEST"


def test_kind_use_restrictions_change_eligibility_identity(workspace):
    from tychora.configuration.resolve import resolve_config

    before = resolve_config(workspace, "base", {})
    file = workspace.parent / "packs/base/kinds/note.toml"
    file.write_text(
        file.read_text().replace('allowed_uses = ["lookup"]', "allowed_uses = []")
    )
    after = resolve_config(workspace, "base", {})
    assert after.eligibility_fingerprint != before.eligibility_fingerprint


def test_graph_search_work_and_inspection_budgets_are_independent(workspace):
    from tychora.configuration.resolve import resolve_config

    config = resolve_config(
        workspace,
        "child",
        {
            "limits": {
                "graph_work_limit": 1000,
                "graph_candidate_limit": 16,
                "inspection_edges": 80,
            }
        },
    )
    limits = config.values.limits
    assert limits.graph_seed_limit == 5
    assert limits.graph_candidate_limit == 16
    assert limits.graph_work_limit == 1000
    assert limits.semantic_hops == 1
    assert limits.inspection_hops == 2
    assert limits.inspection_edges == 80


@pytest.mark.parametrize(
    "name, ceiling",
    [
        ("graph_seed_limit", 5),
        ("graph_candidate_limit", 32),
        ("graph_work_limit", 2000),
        ("semantic_hops", 1),
        ("inspection_hops", 2),
        ("inspection_edges", 100),
    ],
)
def test_each_graph_budget_can_disable_work_but_cannot_raise_its_ceiling(
    workspace, name, ceiling
):
    from tychora.configuration.resolve import resolve_config

    assert (
        getattr(resolve_config(workspace, "child", {}).values.limits, name) == ceiling
    )
    assert (
        getattr(
            resolve_config(workspace, "child", {"limits": {name: 0}}).values.limits,
            name,
        )
        == 0
    )
    with pytest.raises(TychoraError, match="ceiling"):
        resolve_config(workspace, "child", {"limits": {name: ceiling + 1}})


def test_graph_request_respects_lower_workspace_ceiling(workspace):
    from tychora.configuration.resolve import resolve_config

    workspace.write_text(
        workspace.read_text() + "\n[configuration.limits]\ngraph_work_limit = 100\n"
    )
    config = resolve_config(workspace, "child", {"limits": {"graph_work_limit": 50}})
    assert config.values.limits.graph_work_limit == 50
    assert config.values.limits.graph_candidate_limit == 32
    assert config.values.limits.inspection_edges == 100
    with pytest.raises(TychoraError, match="ceiling"):
        resolve_config(workspace, "child", {"limits": {"graph_work_limit": 101}})
