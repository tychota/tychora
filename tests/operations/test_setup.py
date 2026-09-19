"""Setup provisioning behavior tests."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _manifest(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    from tychora.application import setup

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "notices": [
                    {
                        "selection": "fixture",
                        "attribution": "Fixture publisher",
                        "license": "MIT",
                        "notice_url": "https://example.invalid/LICENSE",
                        "model_card_url": "https://example.invalid/model-card",
                    }
                ],
                "assets": [
                    {
                        "id": "fixture",
                        "selection": "fixture",
                        "path": "fixture/asset.bin",
                        "sha256": "3fab5c181bd28a09b64397df76ae2bfaf1eac182979b5fdb7a342858004f36af",
                        "size_bytes": 6,
                        "url": "https://example.invalid/asset.bin",
                        "license": "MIT",
                        "owner": "fixture==1.0",
                        "revision": "fixture-revision",
                        "notice_url": "https://example.invalid/LICENSE",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(setup, "MANIFEST", manifest)
    bundle = tmp_path / "bundle"
    (bundle / "fixture").mkdir(parents=True)
    return manifest, bundle


def test_plan_is_read_only_and_selects_only_requested_assets(tmp_path: Path):
    from tychora.application.setup import SetupService

    cache = tmp_path / "model-cache"
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b""

    plan = SetupService(cache, offline=False, fetch=fetch).plan(("ocr",))

    assert plan.selection == ("ocr",)
    assert len(plan.assets) == 3
    assert {asset.owner for asset in plan.assets} == {"rapidocr==3.9.2"}
    assert plan.notices[0].attribution.startswith("PaddleOCR models by Baidu")
    assert not cache.exists()
    assert calls == []


def test_setup_plan_rejects_bundle_path_traversal(tmp_path: Path, monkeypatch):
    from tychora.application import setup
    from tychora.application.setup import SetupService

    manifest, _ = _manifest(tmp_path, monkeypatch)
    data = json.loads(manifest.read_text())
    data["assets"][0]["path"] = "../escape.bin"
    manifest.write_text(json.dumps(data))
    monkeypatch.setattr(setup, "MANIFEST", manifest)

    with pytest.raises(ValidationError, match="safe relative path"):
        SetupService(tmp_path / "cache", offline=True).plan(("fixture",))


@pytest.mark.qualification
def test_verified_local_tokenizer_loaders_do_not_use_sdk_download_helpers(
    tmp_path: Path,
):
    import shutil

    root_value = __import__("os").environ.get("TYCHORA_TEST_TOKENIZER_ASSETS")
    if not root_value:
        pytest.skip("requires explicitly provisioned tokenizer research assets")
    from tychora.application.setup import (
        SetupService,
        load_output_tokenizer,
        load_voyage_tokenizer,
    )

    source = Path(root_value)
    cache = tmp_path / "cache"
    service = SetupService(cache, offline=True)
    for selection in ("output-tokenizer", "voyage-context-4", "voyage-code-4"):
        for asset in service.plan((selection,)).assets:
            origin = (
                source / "cl100k_base.tiktoken"
                if selection == "output-tokenizer"
                else source / selection / Path(asset.path).name
            )
            target = cache / asset.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)

    output = load_output_tokenizer(cache)
    assert output.decode(
        output.encode("Literal <|endoftext|> source", disallowed_special=())
    ) == ("Literal <|endoftext|> source")
    context = load_voyage_tokenizer(cache, "voyage-context-4")
    code = load_voyage_tokenizer(cache, "voyage-code-4")
    assert context.decode(context.encode("bonjour source").ids) == "bonjour source"
    assert code.decode(code.encode("def source(): pass").ids) == "def source(): pass"


@pytest.mark.asyncio
async def test_setup_apply_persists_capture_checkpoint_and_result_across_reopen(
    tmp_path: Path, monkeypatch
):
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope

    _, bundle = _manifest(tmp_path, monkeypatch)
    (bundle / "fixture/asset.bin").write_bytes(b"pinned")
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    scope = RequestScope(principal_id="operator", capabilities=frozenset({"manage"}))
    workspace = tmp_path / "workspace"
    ctx = ExecutionContext()
    with WorkspaceOwner.open(workspace, config, scope) as owner:
        job_id = await owner.run_blocking(
            lambda: _run_setup(owner, scope, bundle, tmp_path / "cache"), ctx
        )
    with WorkspaceOwner.open(workspace, config, scope) as owner:
        repo = await owner.run_blocking(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ctx,
        )
        status = await owner.run_blocking(lambda: repo.get(job_id), ctx)
        checkpoint = await owner.run_blocking(
            lambda: repo.completed_step(job_id, "provision", status.request_digest),
            ctx,
        )
    assert status.state == "succeeded"
    assert status.captured_inputs is not None and status.result is not None
    assert checkpoint == status.result


@pytest.mark.asyncio
async def test_setup_rejects_missing_or_corrupt_offline_bundle_and_reuses_cache(
    tmp_path: Path, monkeypatch
):
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope

    _, bundle = _manifest(tmp_path, monkeypatch)
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    scope = RequestScope(principal_id="operator", capabilities=frozenset({"manage"}))
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        for local_bundle, expected in (
            (None, "CAPABILITY_MISSING"),
            (bundle, "INTEGRITY_ERROR"),
        ):
            with pytest.raises(TychoraError) as error:
                await owner.run_blocking(
                    lambda local_bundle=local_bundle: _apply_setup(
                        owner,
                        scope,
                        tmp_path / "cache",
                        local_bundle,
                        offline=True,
                    ),
                    ExecutionContext(),
                )
            assert error.value.code == expected
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"pinned")
        (bundle / "fixture/asset.bin").symlink_to(outside)
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: _apply_setup(
                    owner, scope, tmp_path / "cache", bundle, offline=True
                ),
                ExecutionContext(),
            )
        assert error.value.code == "INTEGRITY_ERROR"
        (bundle / "fixture/asset.bin").unlink()
        (bundle / "fixture/asset.bin").write_bytes(b"broken")
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: _apply_setup(
                    owner, scope, tmp_path / "cache", bundle, offline=True
                ),
                ExecutionContext(),
            )
        assert error.value.code == "INTEGRITY_ERROR"
        (bundle / "fixture/asset.bin").write_bytes(b"pinned")
        await owner.run_blocking(
            lambda: _apply_setup(
                owner, scope, tmp_path / "cache", bundle, offline=True
            ),
            ExecutionContext(),
        )
        calls: list[str] = []

        def forbidden_fetch(url: str) -> bytes:
            calls.append(url)
            raise AssertionError("verified cache must not fetch")

        await owner.run_blocking(
            lambda: _apply_setup(
                owner,
                scope,
                tmp_path / "cache",
                None,
                offline=False,
                fetch=forbidden_fetch,
            ),
            ExecutionContext(),
        )
        assert calls == []


@pytest.mark.asyncio
async def test_online_setup_fetches_only_allowlisted_verified_asset(
    tmp_path: Path, monkeypatch
):
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope

    _manifest(tmp_path, monkeypatch)
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    scope = RequestScope(principal_id="operator", capabilities=frozenset({"manage"}))
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b"pinned"

    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        await owner.run_blocking(
            lambda: _apply_setup(
                owner,
                scope,
                tmp_path / "cache",
                None,
                offline=False,
                fetch=fetch,
            ),
            ExecutionContext(),
        )
    assert calls == ["https://example.invalid/asset.bin"]
    assert (tmp_path / "cache/fixture/asset.bin").read_bytes() == b"pinned"


def test_setup_rejects_ancestor_swap_during_injected_fetch(tmp_path, monkeypatch):
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope

    manifest, _ = _manifest(tmp_path, monkeypatch)
    value = json.loads(manifest.read_text())
    value["assets"][0]["path"] = "fixture/nested/asset.bin"
    manifest.write_text(json.dumps(value))
    cache = tmp_path / "cache"
    parent = cache / "fixture"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    def fetch(_url):
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        return b"pinned"

    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    scope = RequestScope(principal_id="review", capabilities=frozenset({"manage"}))
    with (
        WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner,
        pytest.raises(TychoraError, match="safely"),
    ):
        owner.run_blocking_sync(
            lambda: _apply_setup(
                owner,
                scope,
                cache,
                None,
                offline=False,
                fetch=fetch,
            ),
            ExecutionContext(),
        )
    assert not (outside / "nested/asset.bin").exists()


def test_cached_setup_observes_cancellation_before_success(tmp_path, monkeypatch):
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application import setup
    from tychora.application.owner import WorkspaceOwner
    from tychora.application.setup import SetupService
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.requests import RequestScope

    _manifest(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    (cache / "fixture").mkdir(parents=True)
    (cache / "fixture/asset.bin").write_bytes(b"pinned")
    ctx = ExecutionContext()
    original_read = setup._read_verified_at

    def validate_then_cancel(root_fd, asset, context, *, retain=True):
        valid = original_read(root_fd, asset, context, retain=retain)
        if valid is not None:
            ctx.cancelled.set()
        return valid

    monkeypatch.setattr(setup, "_read_verified_at", validate_then_cancel)
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    scope = RequestScope(principal_id="review", capabilities=frozenset({"manage"}))
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:

        def apply():
            config_ref = owner.artifacts.put_stream(
                BytesIO(b'{"type":"registry","schema_version":"1"}'),
                "application/json",
            )
            jobs = JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            )
            service = SetupService(
                cache,
                offline=True,
                jobs=jobs,
                scope=scope,
                effective_config=config_ref,
                artifacts=owner.artifacts,
            )
            return service.apply(service.plan(("fixture",)), None, ctx)

        with pytest.raises(TychoraError) as error:
            owner.run_blocking_sync(apply, ExecutionContext())
        job_id = owner.run_blocking_sync(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0],
            ExecutionContext(),
        )
        status = owner.run_blocking_sync(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ).get(job_id),
            ExecutionContext(),
        )
    assert error.value.code == "CANCELLED"
    assert status.state == "failed" and status.error_code == "CANCELLED"


def test_default_setup_download_bounds_socket_and_chunk_reads_by_context(
    tmp_path, monkeypatch
):
    import time
    from urllib import request

    from tychora.application.setup import SetupService

    _manifest(tmp_path, monkeypatch)
    service = SetupService(tmp_path / "cache", offline=False)
    asset = service.plan(("fixture",)).assets[0]
    observed = []
    reads = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            reads.append(size)
            return b"pinned" if len(reads) == 1 else b""

    def open_url(_url, *, timeout):
        observed.append(timeout)
        return Response()

    monkeypatch.setattr(request, "urlopen", open_url)
    deadline = time.monotonic() + 0.5
    assert service._download(asset, ExecutionContext(deadline=deadline)) == b"pinned"
    assert 0 < observed[0] <= 0.5
    assert reads == [7, 1]

    cancelled = ExecutionContext()

    class CancellingResponse(Response):
        def read(self, size):
            cancelled.cancelled.set()
            return b"pinned"

    monkeypatch.setattr(
        request, "urlopen", lambda *_args, **_kwargs: CancellingResponse()
    )
    with pytest.raises(TychoraError) as error:
        service._download(asset, cancelled)
    assert error.value.code == "CANCELLED"


def _run_setup(owner, scope, bundle: Path, cache: Path) -> str:
    return _apply_setup(owner, scope, cache, bundle, offline=True)


def _apply_setup(
    owner,
    scope,
    cache: Path,
    bundle: Path | None,
    *,
    offline: bool,
    fetch=None,
) -> str:
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.setup import SetupService

    config_ref = owner.artifacts.put_stream(
        BytesIO(b'{"type":"registry","schema_version":"1"}'),
        "application/json",
    )
    repo = JobRepository(
        owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
    )
    service = SetupService(
        cache,
        offline=offline,
        fetch=fetch,
        jobs=repo,
        scope=scope,
        effective_config=config_ref,
        artifacts=owner.artifacts,
    )
    return service.apply(service.plan(("fixture",)), bundle, ExecutionContext()).id
