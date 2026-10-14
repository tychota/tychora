"""An added kind cannot redefine any retained representation or policy."""

import json

import pytest

from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import _resolved_hashes, resolve_config
from tychora.contracts.errors import TychoraError


def configs(tmp_path):
    path = tmp_path / "workspace.toml"
    result = []
    for pack in ("documents", "general-documents"):
        path.write_text(f'schema_version=1\n[profiles.test]\nbuiltin="{pack}@1"\n')
        result.append(resolve_config(path, "test", {}))
    return result


def test_extension_preserves_legacy_envelopes_and_requires_only_title(tmp_path):
    old, new = configs(tmp_path)
    before, after = Registry.from_config(old), Registry.from_config(new)
    assert len(before.kinds) == 15 and len(after.kinds) == 16
    assert (
        old.packs[0].digest
        == "b3f1e7dda8caf56c3ab57e4c12374484f6ad7a6644337572697b3a9f07ddb647"
    )
    assert all(
        after.historical_definitions()[key] == value
        for key, value in before.historical_definitions().items()
    )
    assert after.validate_metadata("document@1", {"title": "Notes"}) == {
        "title": "Notes"
    }
    for metadata in (
        {},
        {"title": "Notes", "publisher": ""},
        {"title": "Notes", "invented": 1},
    ):
        with pytest.raises(TychoraError):
            after.validate_metadata("document@1", metadata)
    with pytest.raises(TychoraError):
        after.validate_metadata("manual@1", {"title": "Notes"})


def test_additive_proof_and_pinned_runtime(tmp_path):
    from tychora.configuration.additive_compatibility import (
        qualified_additive_registry_extension,
    )
    from tychora.configuration.indexed_representation import pinned_runtime_compatible

    old, new = configs(tmp_path)
    assert qualified_additive_registry_extension(old, new)
    assert pinned_runtime_compatible(old, new)
    assert not qualified_additive_registry_extension(new, old)


@pytest.mark.parametrize(
    "path,value",
    [
        (("values", "providers", "text", "representation_version"), "2"),
        (("values", "providers", "text", "provider"), "voyage"),
        (("values", "providers", "text", "max_group_tokens"), 20000),
        (("values", "providers", "text", "max_request_tokens"), 20000),
        (("values", "providers", "image", "model"), "other-model"),
        (("values", "providers", "image", "dimensions"), 512),
        (("values", "providers", "image", "max_assets"), 16),
        (("values", "providers", "image", "max_multimodal_tokens"), 90000),
        (("values", "index", "text", "ef_search"), 100),
        (
            ("registry", "schema_reference_bindings", "manual-metadata@1"),
            {"/$ref": {"schema_id": "document-metadata@1", "fragment": ""}},
        ),
        (
            ("registry", "lanes", "evidence", "kinds"),
            ["research_note@1", "evidence@1", "code_file@1", "document@1"],
        ),
        (
            ("registry", "kinds", "manual@1", "normalizer_options"),
            {"target_labels": ["Changed"]},
        ),
        (
            (
                "registry",
                "schemas",
                "manual-metadata@1",
                "properties",
                "title",
                "maxLength",
            ),
            42,
        ),
        (("registry", "relations", "supports@1", "description"), "Different meaning"),
        (("registry", "lanes", "standard", "citation_prefix"), "Z"),
        (("registry", "kinds", "manual@1", "normalizer"), "numbered_provisions@1"),
        (("registry", "kinds", "document@1", "normalizer"), "rule_records@1"),
        (
            (
                "registry",
                "definition_schema_bindings",
                "kinds",
                "manual@1",
                "metadata_schema",
            ),
            "document-metadata@1",
        ),
        (("values", "extraction", "parser"), "docling"),
        (("values", "extraction", "representation_version"), "2"),
        (("values", "providers", "text", "model"), "other-model"),
        (("values", "providers", "text", "dimensions"), 512),
        (("values", "index", "graph"), "ladybug"),
        (("values", "retrieval", "graph", "semantic"), True),
        (("values", "output", "title"), "Different title"),
    ],
)
def test_additive_proof_rejects_semantic_changes(tmp_path, path, value):
    from tychora.configuration.additive_compatibility import (
        qualified_additive_registry_extension,
    )

    old, new = configs(tmp_path)
    raw = new.model_dump(mode="json")
    node = raw
    for segment in path[:-1]:
        node = node[segment]
    assert node.get(path[-1]) != value
    node[path[-1]] = value
    raw.update(_resolved_hashes(raw["registry"], raw["values"]))
    candidate = ResolvedConfig.model_validate_json(json.dumps(raw))
    assert not qualified_additive_registry_extension(old, candidate)


def test_additive_proof_keeps_credential_rotation_and_monotonic_tightening(tmp_path):
    from tychora.configuration.additive_compatibility import (
        qualified_additive_registry_extension,
    )

    old, new = configs(tmp_path)
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    before["values"]["offline"] = False
    before["values"]["acquisition"]["roots"] = [str(tmp_path)]
    after["values"]["providers"]["text"]["credential_ref"] = "env:ROTATED"
    after["values"]["providers"]["rerank"].update(
        provider="voyage", model="rerank-2.5", credential_ref="env:RERANK"
    )

    def rebuilt(raw):
        raw.update(_resolved_hashes(raw["registry"], raw["values"]))
        return ResolvedConfig.model_validate_json(json.dumps(raw))

    assert qualified_additive_registry_extension(rebuilt(before), rebuilt(after))
    after["values"]["acquisition"]["roots"] = [str(tmp_path / "unregistered")]
    assert not qualified_additive_registry_extension(rebuilt(before), rebuilt(after))
    after["values"]["acquisition"]["roots"] = []
    before["values"]["offline"], after["values"]["offline"] = True, False
    assert not qualified_additive_registry_extension(rebuilt(before), rebuilt(after))
