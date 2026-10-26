"""Docling workspace fixtures shared by unit and qualification tests.

Registered for every test through `pytest_plugins` in tests/conftest.py. Fixtures
that parse with the real Docling bundle read it through asset_path, so tests using
them must carry @pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS").
"""

import base64
import io
import json
import shutil
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.fixtures.documents.create_record_fixtures import (
    create_record_document,
    record_content,
)
from tests.helpers.assets import asset_path
from tychora.adapters.docling.parser import INVENTORY, DoclingParser
from tychora.application.owner import StagedOutput, WorkspaceOwner
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.extraction import DocumentRegistration
from tychora.contracts.requests import RequestScope
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.normalize import normalize
from tychora.ports.execution import ExecutionContext


@pytest.fixture(params=["manual@1", "document@1"])
def document_kind(request):
    return request.param


@pytest.fixture(params=["pdf", "docx"])
def execution(request, tmp_path, document_kind):
    path = tmp_path / "workspace.toml"
    pack = tmp_path / "document-pack"
    shutil.copytree(str(files("tychora") / "profiles/documents"), pack)
    with (pack / "profile.toml").open("a") as out:
        out.write('\n[extraction]\nparser="docling"\n')
    path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="general-documents@1"\n[configuration.extraction]\nparser="docling"\n'
        if document_kind == "document@1"
        else 'schema_version=1\n[profiles.test]\npath="document-pack"\n'
    )
    with path.open("a") as out:
        out.write(
            "\n[configuration.acquisition]\nroots="
            + json.dumps(
                [str((Path(__file__).parents[1] / "fixtures/documents").resolve())]
            )
            + "\n"
        )
    config = resolve_config(path, "test", {})
    with WorkspaceOwner.open(
        tmp_path / "data", config, RequestScope(principal_id="fixture")
    ) as owner:
        yield SimpleNamespace(
            config=config, ctx=ExecutionContext(), owner=owner, format=request.param
        )


@pytest.fixture
def rich_capture(execution, document_kind):
    owner = execution.owner
    metadata = json.loads(
        (
            Path(__file__).parents[1] / "configuration/fixtures/generic-records.json"
        ).read_text()
    )["manual@1"]["metadata"]
    if document_kind == "document@1":
        metadata = {"title": metadata["title"]}
    collection = CollectionRow(
        collection_id="00000000-0000-4000-8000-000000000010",
        workspace_id=owner.workspace_id,
        slug="synthetic",
        title="Synthetic documents",
    )
    source_key = "rich." + execution.format
    source_path = Path(__file__).parents[1] / "fixtures/documents" / source_key
    captured = owner.run_write_sync(
        lambda: Acquirer(owner.artifacts, execution.config).capture(
            SourceInput.model_validate(
                {
                    "type": "file",
                    "path": str(source_path),
                    "kind": document_kind,
                    "metadata": metadata,
                    "collection_id": UUID(collection.collection_id),
                    "source_key": source_key,
                }
            ),
            execution.ctx,
        ),
        execution.ctx,
    )
    sid = identity.source_id(UUID(collection.collection_id), source_key)
    msha = identity.digest("metadata-v1", metadata)
    revision = RevisionRow(
        revision_id=identity.revision_id(sid, captured.original_sha, "1", msha),
        source_id=sid,
        kind_id=document_kind,
        original_sha=captured.original_sha,
        edition_key="1",
        metadata_sha=msha,
        title=metadata["title"],
        metadata_json=FrozenJsonMap(metadata),
        captured_at=captured.captured_at.isoformat(),
        allowed_uses_json=("lookup",),
    )
    execution.registration = DocumentRegistration(
        capture=captured,
        workspace=WorkspaceRow(
            workspace_id=owner.workspace_id,
            created_at=owner.run_write_sync(
                lambda: owner.db.execute(
                    "SELECT created_at FROM workspaces"
                ).fetchone()[0],
                execution.ctx,
            ),
        ),
        collection=collection,
        source=SourceRow(
            source_id=sid,
            collection_id=collection.collection_id,
            source_key=source_key,
            created_at=captured.captured_at.isoformat(),
        ),
        revision=revision,
        format=execution.format,
    )
    return captured.original_ref


def picture_item(captions=()):
    """A Docling picture item carrying the rich-figure fixture as its image."""
    pixels = (Path(__file__).parent / "documents/rich-figure.png").read_bytes()
    return {
        "self_ref": "#/pictures/0",
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "picture",
        "prov": [],
        "captions": [{"$ref": ref} for ref in captions],
        "image": {
            "uri": "data:image/png;base64," + base64.b64encode(pixels).decode(),
            "size": {"width": 640.0, "height": 320.0},
            "dpi": 72,
            "mimetype": "image/png",
        },
    }


def retain_export(execution, rich_capture, export):
    """Retain a synthetic Docling export as the parser would, without the models.

    Returns the retained extraction artifact and the registry it was bound to.
    """
    registry = Registry.from_config(execution.config)
    kind = registry.kinds["manual@1"]
    assert isinstance(kind, FrozenJsonMap)
    parser = DoclingParser(
        execution.owner,
        execution.registration,
        Path("/nonexistent-explicit-model-bundle"),
    )
    payload = identity.canonical_bytes(export)
    artifact = execution.owner.run_write_sync(
        lambda: parser._retain(
            (
                (
                    StagedOutput("document.json", "application/json", len(payload)),
                    io.BytesIO(payload),
                ),
            ),
            rich_capture,
            execution.config,
            registry,
            kind,
            json.loads(INVENTORY.read_text()),
        ),
        execution.ctx,
    )
    return artifact, registry


@pytest.fixture
def docling_parser(execution, rich_capture):
    bundle = asset_path("TYCHORA_TEST_DOCLING_MODELS")
    return DoclingParser(execution.owner, execution.registration, bundle)


@pytest.fixture(scope="module")
def document_record_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("document-records")
    cache = {}
    with ExitStack() as stack:

        def make(
            kind,
            format,
            *,
            custom=False,
            page_break_at=None,
            source_builder=None,
            commit=True,
            online=False,
            scope=None,
        ):
            key = (
                kind,
                format,
                custom,
                page_break_at,
                source_builder,
                commit,
                online,
                None if scope is None else scope.model_dump_json(),
            )
            if key in cache:
                return SimpleNamespace(
                    **(vars(cache[key]) | {"ctx": ExecutionContext()})
                )
            folder = root / str(len(list(root.iterdir())))
            folder.mkdir()
            pack = folder / "pack"
            shutil.copytree(
                Path(__file__).parents[2] / "src/tychora/profiles/documents", pack
            )
            content = record_content(kind)
            registered_kind = "feedback@1" if kind.startswith("feedback") else kind
            if custom:
                target = pack / "kinds/standard.toml"
                target.write_text(
                    target.read_text()
                    .replace(
                        'identifier_prefixes = ["STD-"]',
                        'identifier_prefixes = ["LOCAL-"]',
                    )
                    .replace(
                        'requirement_labels = ["Requirement"]',
                        'requirement_labels = ["Exigence"]',
                    )
                    .replace(
                        'proof_labels = ["Expected evidence"]',
                        'proof_labels = ["Preuve attendue"]',
                    )
                    + 'foundation_labels = ["Foundation"]\n'
                )
                content = [
                    (
                        level,
                        text.replace("STD-", "LOCAL-")
                        .replace("Requirement:", "Exigence:")
                        .replace("Expected evidence:", "Preuve attendue:"),
                    )
                    for level, text in content
                ]
            if format != "markdown":
                with (pack / "profile.toml").open("a") as out:
                    out.write('\n[extraction]\nparser="docling"\n')
            source = folder / ("source." + format)
            if source_builder is None:
                create_record_document(source, content, page_break_at=page_break_at)
            else:
                source_builder(source, content)
            config_path = folder / "workspace.toml"
            config_path.write_text(
                'schema_version=1\n[profiles.test]\npath="pack"\n[configuration.acquisition]\nroots='
                + json.dumps([str(folder)])
                + "\n"
            )
            config = resolve_config(config_path, "test", {})
            if online:
                config = config.model_copy(
                    update={
                        "values": config.values.model_copy(update={"offline": False})
                    }
                )
            owner = stack.enter_context(
                WorkspaceOwner.open(
                    folder / "data",
                    config,
                    scope or RequestScope(principal_id="fixture"),
                )
            )
            ctx = ExecutionContext()
            metadata = json.loads(
                (
                    Path(__file__).parents[1]
                    / "configuration/fixtures/generic-records.json"
                ).read_text()
            )[registered_kind]["metadata"]
            if registered_kind == "feedback@1":
                metadata.update(author=None, target=None, decision=None)
            collection = CollectionRow(
                collection_id="00000000-0000-4000-8000-000000000010",
                workspace_id=owner.workspace_id,
                slug="synthetic",
                title="Synthetic documents",
            )
            captured = owner.run_write_sync(
                lambda: Acquirer(owner.artifacts, config).capture(
                    SourceInput.model_validate(
                        {
                            **(
                                {
                                    "type": "text",
                                    "text": source.read_text(),
                                    "format": "markdown",
                                    "encoding": "utf-8",
                                }
                                if format == "markdown"
                                else {"type": "file", "path": str(source)}
                            ),
                            "kind": registered_kind,
                            "metadata": metadata,
                            "collection_id": UUID(collection.collection_id),
                            "source_key": source.name,
                        }
                    ),
                    ctx,
                ),
                ctx,
            )
            sid = identity.source_id(UUID(collection.collection_id), source.name)
            msha = identity.digest("metadata-v1", metadata)
            revision = RevisionRow(
                revision_id=identity.revision_id(sid, captured.original_sha, "1", msha),
                source_id=sid,
                kind_id=registered_kind,
                original_sha=captured.original_sha,
                edition_key="1",
                metadata_sha=msha,
                title=metadata["title"],
                metadata_json=FrozenJsonMap(metadata),
                captured_at=captured.captured_at.isoformat(),
                allowed_uses_json=("lookup",),
            )
            fields = {
                "capture": captured,
                "workspace": WorkspaceRow(
                    workspace_id=owner.workspace_id,
                    created_at=owner.run_write_sync(
                        lambda: owner.db.execute(
                            "SELECT created_at FROM workspaces"
                        ).fetchone()[0],
                        ctx,
                    ),
                ),
                "collection": collection,
                "source": SourceRow(
                    source_id=sid,
                    collection_id=collection.collection_id,
                    source_key=source.name,
                    created_at=captured.captured_at.isoformat(),
                ),
                "revision": revision,
                "format": format,
            }
            if format == "markdown":
                from tychora.contracts.extraction import NativeRegistration
                from tychora.ingestion.native import NativeParser

                native_registration = NativeRegistration.model_validate(fields)
                parsed = owner.run_write_sync(
                    lambda: NativeParser(owner.artifacts, native_registration).extract(
                        captured.original_ref, config, ctx
                    ),
                    ctx,
                )
            else:
                registration = DocumentRegistration.model_validate(fields)
                model_path = asset_path("TYCHORA_TEST_DOCLING_MODELS")
                parsed = DoclingParser(owner, registration, model_path).extract(
                    captured.original_ref, config, ctx
                )
            registry = Registry.from_config(config)
            bundle = (
                normalize(parsed, revision.revision_id, registry) if commit else None
            )
            if bundle is not None:
                owner.run_write_sync(lambda: owner.store.commit_extraction(bundle), ctx)
            result = SimpleNamespace(
                parsed=parsed,
                bundle=bundle,
                store=owner.store,
                registry=registry,
                owner=owner,
                ctx=ctx,
                source=source,
                content=content,
            )
            cache[key] = result
            return result

        yield make
