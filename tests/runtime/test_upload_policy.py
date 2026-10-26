import json
from contextlib import contextmanager

import pytest

from tests.helpers.corpus import COLLECTION, commit_with_facts, reader, seed_bundle
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


@contextmanager
def prepared(tmp_path, *, offline=False):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    config = config.model_copy(
        update={"values": config.values.model_copy(update={"offline": offline})}
    )
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=3,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"synthetic"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        yield owner, scope


def install(owner):
    bundle = seed_bundle()
    for ref in bundle.artifacts:
        with reader().open_verified(ref) as data:
            owner.artifacts.put_stream(data, ref.media_type)
    created = owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0]
    bundle = bundle.model_copy(
        update={
            "workspace": bundle.workspace.model_copy(
                update={"workspace_id": owner.workspace_id, "created_at": created}
            ),
            "collection": bundle.collection.model_copy(
                update={
                    "workspace_id": owner.workspace_id,
                    "policy_json": FrozenJsonMap(
                        {"schema_version": 1, "upload_providers": ["synthetic"]}
                    ),
                }
            ),
        }
    )
    commit_with_facts(owner.store, bundle)
    return bundle


@pytest.mark.asyncio
async def test_upload_rechecks_canonical_grants_and_epoch(tmp_path):
    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_write(lambda: install(owner), ExecutionContext())
        deps = frozenset({bundle.units[0].unit_id, bundle.revision.revision_id})
        await owner.run_write(
            lambda: owner.upload_policy.authorize(scope, "synthetic", deps, 0),
            ExecutionContext(),
        )
        await owner.run_write(
            lambda: owner.store.withdraw_source(bundle.source.source_id),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as exc:
            await owner.run_write(
                lambda: owner.upload_policy.authorize(scope, "synthetic", deps, 0),
                ExecutionContext(),
            )
        assert exc.value.code == "SCOPE_CHANGED"
        assert exc.value.retryable
        with pytest.raises(TychoraError) as exc:
            await owner.run_write(
                lambda: owner.upload_policy.authorize(scope, "synthetic", deps, 1),
                ExecutionContext(),
            )
        assert exc.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "offline",
        "owner_grant",
        "request_grant",
        "collection_grant",
        "malformed_grant",
        "duplicate_fields",
        "access_floor",
        "uses",
        "unknown",
        "raw_sha",
        "empty",
        "ambiguous",
        "foreign",
        "dangling_workspace",
        "malformed_kind",
    ],
)
async def test_upload_denies_missing_or_invalid_permission(tmp_path, change):
    with prepared(tmp_path, offline=change == "offline") as (owner, scope):
        bundle = await owner.run_write(lambda: install(owner), ExecutionContext())
        deps = frozenset({bundle.units[0].unit_id})
        request = scope

        def mutate():
            if change == "collection_grant":
                owner.db.execute("UPDATE collections SET policy_json='{}'")
            if change == "malformed_grant":
                owner.db.execute(
                    "UPDATE collections SET policy_json=?",
                    (
                        json.dumps(
                            {
                                "schema_version": 1,
                                "upload_providers": ["synthetic"],
                                "extra": True,
                            }
                        ),
                    ),
                )
            if change == "duplicate_fields":
                owner.db.execute(
                    "UPDATE collections SET policy_json=?",
                    (
                        '{"schema_version":1,"upload_providers":[],"upload_providers":["synthetic"]}',
                    ),
                )
            if change == "access_floor":
                owner.db.execute("UPDATE sources SET access_floor=3")
            if change == "malformed_kind":
                owner.db.execute("UPDATE kinds SET definition_json='1'")
            if change == "uses":
                owner.db.execute("UPDATE revisions SET allowed_uses_json='[]'")
            if change == "ambiguous":
                owner.db.execute(
                    "UPDATE evidence_units SET unit_id=?",
                    (bundle.revision.revision_id,),
                )
            if change == "dangling_workspace":
                owner.db.commit()
                owner.db.execute("PRAGMA foreign_keys=OFF")
                owner.db.execute("UPDATE collections SET workspace_id='missing'")
                owner.db.commit()
            if change == "foreign":
                owner.db.execute(
                    "INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES ('foreign',2,'2024')"
                )
                owner.db.execute("UPDATE collections SET workspace_id='foreign'")

        if change == "ambiguous":
            # Mutate an otherwise unused asset identity to overlap a revision.
            def mutate():
                owner.db.execute(
                    "INSERT INTO assets (asset_id,extraction_id,artifact_sha,asset_type,native_ref,locator_json,declared_review_state) VALUES (?,?,?,'image','synthetic','{}','accepted')",
                    (
                        bundle.units[0].unit_id,
                        bundle.extraction.extraction_id,
                        bundle.artifacts[0].sha256,
                    ),
                )

        await owner.run_write(mutate, ExecutionContext())
        if change == "owner_grant":
            request = scope.model_copy(
                update={"upload_providers": frozenset({"ungranted"})}
            )
        if change == "request_grant":
            request = scope.model_copy(update={"upload_providers": frozenset()})
        if change == "access_floor":
            request = scope.model_copy(update={"access_ceiling": 0})
        if change == "unknown":
            deps = frozenset({"unknown"})
        if change == "raw_sha":
            deps = frozenset({bundle.artifacts[0].sha256})
        if change == "empty":
            deps = frozenset()
        with pytest.raises(TychoraError) as exc:
            await owner.run_write(
                lambda: owner.upload_policy.authorize(
                    request,
                    "ungranted" if change == "owner_grant" else "synthetic",
                    deps,
                    0,
                ),
                ExecutionContext(),
            )
        if change == "foreign":
            # Two workspace rows make the epoch read itself ambiguous.
            assert exc.value.code == "INTEGRITY_ERROR"
            assert exc.value.reason == "canonical.row_invalid"
        else:
            assert exc.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_provider_concurrency_and_queued_reauthorization(tmp_path):
    import asyncio
    import threading

    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_write(lambda: install(owner), ExecutionContext())
        release = threading.Event()
        lock = threading.Lock()
        started = []

        def provider_call():
            with lock:
                started.append(True)
            assert release.wait(5)
            return "synthetic-result"

        deps = frozenset({bundle.units[0].unit_id})
        tasks = [
            asyncio.create_task(
                owner.run_provider(
                    "synthetic",
                    provider_call,
                    ExecutionContext(),
                    scope=scope,
                    dependencies=deps,
                    policy_epoch=0,
                )
            )
            for _ in range(3)
        ]
        try:
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(started) == 2
            await owner.run_write(
                lambda: owner.store.withdraw_source(bundle.source.source_id),
                ExecutionContext(),
            )
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert results[:2] == ["synthetic-result", "synthetic-result"]
            assert isinstance(results[2], TychoraError)
            assert results[2].code == "SCOPE_CHANGED"
            assert len(started) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "cycle", "hidden"])
async def test_proxy_upload_requires_all_registered_dependencies(tmp_path, case):
    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_write(lambda: install(owner), ExecutionContext())
        unit = bundle.units[0].unit_id

        def proxy():
            owner.db.execute(
                "INSERT INTO assets (asset_id,extraction_id,artifact_sha,asset_type,native_ref,locator_json,declared_review_state) VALUES ('synthetic-asset',?,?,'image','synthetic','{}','accepted')",
                (bundle.extraction.extraction_id, bundle.artifacts[0].sha256),
            )
            owner.db.execute(
                "INSERT INTO asset_proxies (proxy_unit_id,asset_id,origin_type,derivation_sha) VALUES (?,'synthetic-asset','caption_copy',?)",
                (unit, bundle.artifacts[0].sha256),
            )
            if case != "missing":
                owner.db.execute(
                    "INSERT INTO evidence_units SELECT 'support',extraction_id,NULL,99,unit_type,'support','support',heading_path_json,text,text_sha,text_origin,citable,declared_review_state,attributes_json FROM evidence_units WHERE unit_id=?",
                    (unit,),
                )
                owner.db.execute(
                    "INSERT INTO asset_proxy_dependencies VALUES (?,'support')", (unit,)
                )
                if case == "cycle":
                    owner.db.execute(
                        "INSERT INTO asset_proxies (proxy_unit_id,asset_id,origin_type,derivation_sha) VALUES ('support','synthetic-asset','caption_copy',?)",
                        (bundle.artifacts[0].sha256,),
                    )
                    owner.db.execute(
                        "INSERT INTO asset_proxy_dependencies VALUES ('support',?)",
                        (unit,),
                    )
                else:
                    # A support in another canonical collection must not inherit proxy access.
                    owner.db.execute(
                        "INSERT INTO collections VALUES ('hidden',?,'hidden','Hidden',0,'{}')",
                        (owner.workspace_id,),
                    )
                    owner.db.execute(
                        "INSERT INTO sources SELECT 'hidden-source','hidden','hidden.md',source_type,registration_json,0,NULL,NULL,created_at FROM sources LIMIT 1"
                    )
                    owner.db.execute(
                        "INSERT INTO revisions SELECT 'hidden-revision','hidden-source',kind_id,original_sha,edition_key,metadata_sha,title,published_on,effective_from,effective_until,access_rank,declared_review_state,allowed_uses_json,metadata_json,captured_at FROM revisions LIMIT 1"
                    )
                    owner.db.execute(
                        "INSERT INTO extractions SELECT 'hidden-extraction','hidden-revision',parser_fingerprint,normalizer_fingerprint,document_artifact_sha,assertions_artifact_sha,diagnostics_artifact_sha,created_at FROM extractions LIMIT 1"
                    )
                    owner.db.execute(
                        "UPDATE evidence_units SET extraction_id='hidden-extraction' WHERE unit_id='support'"
                    )

        await owner.run_write(proxy, ExecutionContext())
        with pytest.raises(TychoraError) as exc:
            await owner.run_write(
                lambda: owner.upload_policy.authorize(
                    scope, "synthetic", frozenset({unit}), 0
                ),
                ExecutionContext(),
            )
        assert exc.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_reopening_rejects_changed_retained_kind(tmp_path):
    with prepared(tmp_path) as (owner, scope):
        await owner.run_write(lambda: install(owner), ExecutionContext())
        config, root = owner.config, owner.path
        await owner.run_write(
            lambda: owner.db.execute("UPDATE kinds SET definition_json='{}'"),
            ExecutionContext(),
        )
    with pytest.raises(TychoraError), WorkspaceOwner.open(root, config, scope):
        pytest.fail("Changed retained definition accepted")
