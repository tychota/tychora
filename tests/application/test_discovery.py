"""Scoped discovery over detached definitions and an opened canonical owner."""

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

from tests.code.conftest import git, make_repo
from tests.typed_json import json_array, json_integer, json_object, json_string
from tychora.application.discovery import DiscoveryService, _scoped_resolution_rates
from tychora.application.inspection import InspectionService
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import DiscoverRequest, InspectRequest, RequestScope, SourceInput
from tychora.contracts.code import Resolution
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def configured(tmp_path: Path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    return config, Registry.from_config(config)


def test_static_kinds_without_owner_have_definitions_but_no_corpus_counts(configured):
    config, registry = configured
    service = DiscoveryService(config, registry)
    result = service.discover(
        DiscoverRequest(subject="kinds"), RequestScope(principal_id="reader")
    )
    assert any(item.id == "manual@1" for item in result.definitions)
    assert result.counts == ()


def test_empty_configured_kind_remains_discoverable(configured, tmp_path):
    config, registry = configured
    reader = RequestScope(principal_id="reader")
    with WorkspaceOwner.open(tmp_path / "corpus", config, reader) as owner:
        result = DiscoveryService(config, registry, owner).discover(
            DiscoverRequest(subject="kinds"), reader
        )
    standard = next(item for item in result.definitions if item.id == "manual@1")
    assert standard.definition["description"]
    assert next(count.count for count in result.counts if count.name == "manual@1") == 0


def test_unknown_subject_names_available_subjects(configured):
    config, registry = configured
    with pytest.raises(TychoraError) as error:
        DiscoveryService(config, registry).discover(
            "unknown", RequestScope(principal_id="reader")
        )
    assert error.value.code == "INVALID_REQUEST"
    assert "kinds" in error.value.message


def test_settings_disclose_separate_parser_and_acquisition_ceilings(configured):
    config, registry = configured
    result = DiscoveryService(config, registry).discover(
        DiscoverRequest(subject="settings"), RequestScope(principal_id="reader")
    )
    limits = json_object(result.definitions[0].definition, "parser_limits")
    assert limits["native_source_bytes"] == min(
        4 * 1024 * 1024, config.values.acquisition.limits.max_file_bytes
    )
    assert limits["native_markdown_block_tokens"] == 100000
    assert limits["native_json_records"] == 4096
    assert limits["docling_conversion_bytes"] == 256 * 1024 * 1024
    assert limits["document_semantic_units_parts_separators"] == 100000
    assert (
        json_object(result.definitions[0].definition, "retrieval")[
            "table_continuations"
        ]
        == "derived"
    )


def test_capability_inventory_is_static_and_does_not_import_adapters(configured):
    config, registry = configured
    before = {name for name in sys.modules if name.startswith(("docling", "voyageai"))}
    result = DiscoveryService(config, registry).discover(
        DiscoverRequest(subject="settings"), RequestScope(principal_id="reader")
    )
    statuses = {item.name: item for item in result.capability_status}
    assert statuses["sqlite"].required and statuses["sqlite"].installed
    assert "docling" in statuses
    assert {
        name for name in sys.modules if name.startswith(("docling", "voyageai"))
    } == before


def test_resolution_rates_omit_excluded_candidate_without_hidden_count():
    def record(
        reference: str, target: str | None, supported: bool = True
    ) -> Resolution:
        return Resolution(
            reference_id=reference,
            source_id="source",
            source_unit_id="source-unit",
            revision_id="revision",
            relative_path="module.py",
            language="python",
            reference_form="call",
            spelling="function",
            owning_scope=(),
            source_scope_ref=None,
            byte_start=0,
            byte_end=8,
            char_start=0,
            char_end=8,
            line_start=1,
            line_end=1,
            adapter_fingerprint=digest("adapter"),
            supported=supported,
            state="resolved" if target else "unresolved",
            selected_target=target,
            candidates=(target,) if target else (),
            rule="synthetic",
            reasons=("synthetic",),
            truncated=False,
            total_candidate_count=1 if target else 0,
        )

    summary = _scoped_resolution_rates(
        (
            record("visible", "allowed"),
            record("excluded", "hidden"),
            record("unsupported", None, False),
        ),
        frozenset({"source-unit", "allowed"}),
        frozenset({"revision"}),
    )
    rates = json_array(summary["rates"])
    assert json_object(rates[0])["resolved"] == 1
    assert summary["unsupported_references"] == 1
    assert summary["scope_limited"] is True
    assert "hidden" not in str(summary)
    assert "omitted" not in str(summary)


@pytest.mark.asyncio
async def test_code_source_discovery_uses_retained_scoped_resolution_rates(tmp_path):
    repository = make_repo(tmp_path)
    (repository / "module.py").write_text("def original(): return 1\noriginal()\n")
    git(repository, "add", "module.py")
    git(repository, "commit", "-qm", "add call reference")
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
        '[configuration.extraction]\nparser="code"\n'
        '[configuration.index]\ngraph="disabled"\n'
        '[configuration.retrieval.graph]\nstructural="none"\nsemantic=false\npredicates=[]\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup"}),
    )

    class Counter:
        model = "local"
        tokenizer_id = "provider@1"

        def count(self, text):
            return len(text)

    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_blocking(
            lambda: owner.db.execute(
                'INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "code", "Code"),
            ),
            ExecutionContext(),
        )
        service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        await service.add(
            SourceInput.from_mapping(
                {
                    "type": "repository",
                    "path": str(repository),
                    "kind": "code_file@1",
                    "metadata": {"title": "Synthetic Repository"},
                    "source_key": "repository",
                    "collection_id": collection,
                }
            ),
            scope,
        )
        result = await __import__("asyncio").to_thread(
            DiscoveryService(config, owner.store.registry, owner).discover,
            DiscoverRequest(subject="sources"),
            scope,
        )
        revisions = json_array(result.definitions[0].definition["revisions"])
        extractions = json_array(json_object(revisions[0])["extractions"])
        extraction = json_object(extractions[0])
        extraction_id = json_string(extraction["extraction_id"])

        def hide_candidate():
            old_sha = owner.db.execute(
                "SELECT diagnostics_artifact_sha FROM extractions WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()[0]
            row = owner.db.execute(
                "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
                (old_sha,),
            ).fetchone()
            from tychora.contracts.artifacts import ArtifactRef

            with owner.artifacts.open_verified(
                ArtifactRef(
                    sha256=old_sha,
                    size_bytes=row[0],
                    media_type=row[1],
                    storage_key=row[2],
                )
            ) as stream:
                payload = json.load(stream)
            first = payload["data"]["resolutions"][0].copy()
            first.update(
                reference_id="hidden-reference",
                state="resolved",
                candidates=["hidden-unit"],
                selected_target="hidden-unit",
                total_candidate_count=1,
                truncated=False,
            )
            payload["data"]["resolutions"].append(first)
            ref = owner.artifacts.put_stream(
                BytesIO(json.dumps(payload, sort_keys=True).encode()),
                "application/json",
            )
            owner.db.execute(
                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00Z",
                ),
            )
            owner.db.execute(
                "UPDATE extractions SET diagnostics_artifact_sha=? WHERE extraction_id=?",
                (ref.sha256, extraction_id),
            )

        await owner.run_blocking(hide_candidate, ExecutionContext())
        diagnostic = await InspectionService(owner).inspect(
            InspectRequest(subject="diagnostics", id=extraction_id),
            scope,
            ExecutionContext(),
        )
    rates = json_object(extraction, "code_resolution")
    assert (
        sum(
            json_integer(json_object(item)["extracted"])
            for item in json_array(rates["rates"])
        )
        >= 1
    ), rates
    records = [item for item in diagnostic.records if item.record_type == "resolution"]
    assert records
    assert all(item.value["source_unit_id"] for item in records)
    assert (
        sum(
            json_integer(json_object(item)["extracted"])
            for item in json_array(diagnostic.records[0].value["rates"])
        )
        >= 1
    )
    assert diagnostic.records[0].value["scope_limited"] is True
    assert "hidden-reference" not in diagnostic.model_dump_json()
    assert "hidden-unit" not in diagnostic.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,required",
    [
        ("manual@1", {"title", "publisher", "edition"}),
        ("evidence@1", {"title", "author", "provenance"}),
    ],
)
async def test_kind_metadata_form_is_scoped_and_schema_derived(
    tmp_path, kind, required
):
    from tychora import Tychora

    with Tychora.open(tmp_path / "metadata") as kb:
        service = kb._workspace.services.discovery
        form = await service.kind_metadata_schema(kind, kb.scope, ExecutionContext())
        assert {field.name for field in form.fields if field.required} == required
        assert form.schema_id == kind.replace("@1", "-metadata@1")
        assert form.schemas[form.schema_id]["additionalProperties"] is False
        assert len(form.model_dump_json().encode()) <= 256 * 1024
        for forbidden in ("metadata@1", "../schemas/metadata.json", "absent@1"):
            with pytest.raises(TychoraError) as err:
                await service.kind_metadata_schema(
                    forbidden, kb.scope, ExecutionContext()
                )
            assert err.value.code == "NOT_FOUND"
        for update in (
            {"collection_ids": frozenset()},
            {"uses": frozenset()},
            {"capabilities": frozenset()},
        ):
            with pytest.raises(TychoraError) as err:
                await service.kind_metadata_schema(
                    kind, kb.scope.model_copy(update=update), ExecutionContext()
                )
            assert err.value.code == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["nodes", "bytes"])
async def test_kind_metadata_form_enforces_schema_and_response_budgets(
    tmp_path, budget
):
    from tychora import Tychora
    from tychora.configuration.models import ResolvedConfig
    from tychora.configuration.resolve import _resolved_hashes

    with Tychora.open(tmp_path / "metadata-budget") as kb:
        owner = kb._workspace.owner
        raw = owner.config.model_dump(mode="json")
        schema = raw["registry"]["schemas"]["manual-metadata@1"]
        if budget == "nodes":
            schema["properties"].update(
                {f"field{i}": {"type": "string"} for i in range(65)}
            )
        else:
            schema["description"] = "x" * (256 * 1024)
        raw.update(_resolved_hashes(raw["registry"], raw["values"]))
        config = ResolvedConfig.model_validate_json(json.dumps(raw))
        service = DiscoveryService(config, Registry.from_config(config), owner)
        with pytest.raises(TychoraError) as err:
            await service.kind_metadata_schema("manual@1", kb.scope, ExecutionContext())
        assert err.value.code == "BUDGET_TOO_SMALL"


@pytest.mark.asyncio
async def test_metadata_form_keeps_transitive_local_refs_and_boolean_constraints(
    tmp_path,
):
    from tychora import Tychora
    from tychora.configuration.models import ResolvedConfig
    from tychora.configuration.resolve import _resolved_hashes

    with Tychora.open(tmp_path / "metadata-refs") as kb:
        owner = kb._workspace.owner
        raw = owner.config.model_dump(mode="json")
        registry = raw["registry"]
        schema = registry["schemas"]["manual-metadata@1"]
        schema["properties"]["advanced"] = {"$ref": "local.json"}
        schema["properties"]["forbidden"] = False
        registry["schemas"]["local@1"] = {
            "$id": "local@1",
            "type": "string",
            "maxLength": 12,
        }
        registry["schema_reference_bindings"]["manual-metadata@1"][
            "/properties/advanced/$ref"
        ] = {"schema_id": "local@1", "fragment": ""}
        registry["schema_reference_bindings"]["local@1"] = {}
        raw.update(_resolved_hashes(registry, raw["values"]))
        config = ResolvedConfig.model_validate_json(json.dumps(raw))
        service = DiscoveryService(config, Registry.from_config(config), owner)
        form = await service.kind_metadata_schema(
            "manual@1", kb.scope, ExecutionContext()
        )
        assert set(form.schemas) == {"manual-metadata@1", "local@1"}
        assert (
            json_object(
                json_object(form.schema_reference_bindings, "manual-metadata@1"),
                "/properties/advanced/$ref",
            )["schema_id"]
            == "local@1"
        )
        fields = {field.name: field for field in form.fields}
        assert fields["advanced"].advanced
        assert fields["forbidden"].advanced and fields["forbidden"].constraints is False
        assert form.advanced


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root_options,metadata,advanced",
    [
        (
            {
                "required": ["title", "custom"],
                "additionalProperties": {"type": "string"},
            },
            {"title": "Notes", "custom": "Required value"},
            True,
        ),
        (
            {"additionalProperties": {"type": "string"}},
            {"title": "Notes", "custom": "Optional value"},
            True,
        ),
        ({"additionalProperties": True}, {"title": "Notes", "custom": 1}, True),
        ({}, {"title": "Notes", "custom": 1}, True),
        (
            {"additionalProperties": False, "enum": [{"title": "Notes"}]},
            {"title": "Notes"},
            True,
        ),
        ({"additionalProperties": False}, {"title": "Notes"}, False),
    ],
)
async def test_metadata_form_marks_unrepresented_root_semantics_advanced(
    tmp_path, root_options, metadata, advanced
):
    from tychora import Tychora
    from tychora.configuration.models import ResolvedConfig
    from tychora.configuration.resolve import _resolved_hashes

    with Tychora.open(tmp_path / "metadata-root") as kb:
        owner = kb._workspace.owner
        raw = owner.config.model_dump(mode="json")
        schema = {
            "$id": "manual-metadata@1",
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            **root_options,
        }
        raw["registry"]["schemas"]["manual-metadata@1"] = schema
        raw.update(_resolved_hashes(raw["registry"], raw["values"]))
        config = ResolvedConfig.model_validate_json(json.dumps(raw))
        registry = Registry.from_config(config)
        assert registry.validate_metadata("manual@1", metadata) == metadata
        service = DiscoveryService(config, registry, owner)
        form = await service.kind_metadata_schema(
            "manual@1", kb.scope, ExecutionContext()
        )
        assert form.advanced is advanced
        assert [field.name for field in form.fields] == ["title"]
        assert form.model_dump(mode="json")["schemas"][form.schema_id] == schema
