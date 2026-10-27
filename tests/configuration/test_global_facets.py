"""Global declarations bind one registered source facet at a time."""

from copy import deepcopy

import pytest

from tests.typed_json import json_object
from tychora.configuration.global_facets import binding_bytes, normalized_metadata
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.requests import AndFilter, EqFilter
from tychora.retrieval.compiler import filter_matches


def _registry(tmp_path, *, remap_alias=False):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    data = config.model_dump(mode="json")["registry"]
    second = deepcopy(data["facets"]["scope.project"])
    second.update(
        {
            "key": "scope.region",
            "alias": "region",
            "source_binding": "metadata.scope.region",
        }
    )
    data["facets"]["scope.region"] = second
    if remap_alias:
        data["facets"]["scope.project"]["alias"] = "workspace"
        data["facets"]["scope.region"]["alias"] = "project"
    config = config.model_copy(
        update={
            "registry": FrozenJsonMap(data),
            "registry_digest": digest("registry-v1", data),
        }
    )
    return Registry.from_config(config)


def test_only_declared_canonical_facet_is_global_and_child_can_narrow(tmp_path):
    registry = _registry(tmp_path)
    metadata = normalized_metadata(
        FrozenJsonMap(
            {
                "scope": {"global_facets": ["project"]},
            }
        ),
        registry,
    )
    declared, proof = binding_bytes(
        metadata, "revision", digest("metadata-v1", metadata), registry
    )
    assert declared == frozenset({"scope.project"}) and proof
    project = EqFilter(field="scope.project", value="alpha")
    region = EqFilter(field="scope.region", value="east")

    def matches(expr, source, child, include=True):
        return filter_matches(
            expr,
            registry,
            source,
            child,
            include_global=include,
            global_facets=declared,
        )

    assert matches(project, {}, {}, include=True)
    assert not matches(project, {}, {}, include=False)
    assert not matches(region, {}, {"scope.region": "east"})
    assert not matches(project, {}, {"scope.project": "beta"})
    assert matches(project, {}, {"scope.project": "alpha"})
    assert not matches(AndFilter(children=(project, region)), {}, {})
    assert matches(
        AndFilter(children=(project, region)),
        {"scope.region": "east"},
        {"scope.project": "alpha", "scope.region": "east"},
    )
    assert not matches(
        AndFilter(children=(project, region)),
        {"scope.region": "east"},
        {"scope.region": "west"},
    )


def test_alias_remap_cannot_reinterpret_retained_canonical_binding(tmp_path):
    first = _registry(tmp_path)
    metadata = normalized_metadata(
        FrozenJsonMap(
            {
                "scope": {"global_facets": ["project"]},
            }
        ),
        first,
    )
    msha = digest("metadata-v1", metadata)
    original = binding_bytes(metadata, "revision", msha, first)
    remapped = _registry(tmp_path, remap_alias=True)
    assert binding_bytes(metadata, "revision", msha, remapped) == original
    remapped_metadata = normalized_metadata(
        FrozenJsonMap(
            {
                "scope": {"global_facets": ["project"]},
            }
        ),
        remapped,
    )
    assert json_object(remapped_metadata, "scope")["global_facets"] == ("scope.region",)
    with pytest.raises(TychoraError, match="overlap"):
        normalized_metadata(
            FrozenJsonMap(
                {
                    "scope": {"global_facets": ["project", "scope.region"]},
                }
            ),
            remapped,
        )
