"""Qualified inventory and bounded local verification never provision assets."""

import json
from hashlib import sha256

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.application.setup import SetupService
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


def manifest(tmp_path, monkeypatch, payload=b"0123456789"):
    asset = {
        "id": "test-asset",
        "selection": "output-tokenizer",
        "path": "synthetic/asset",
        "size_bytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "url": "https://example.invalid/asset",
        "license": "MIT",
        "owner": "Synthetic",
        "revision": "1",
        "notice_url": "https://example.invalid/notice",
    }
    notice = {
        "selection": "output-tokenizer",
        "attribution": "Synthetic",
        "license": "MIT",
        "notice_url": "https://example.invalid/notice",
        "model_card_url": "https://example.invalid/card",
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"assets": [asset], "notices": [notice]}))
    monkeypatch.setattr("tychora.application.setup.MANIFEST", path)
    return payload


@pytest.mark.asyncio
async def test_models_http_catalog_plan_readiness_is_read_only(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch)
    cache = tmp_path / "absent"
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(cache))
    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            catalog = await client.get("/v1/models")
            assert catalog.status_code == 200, catalog.text
            rerank = next(
                p for p in catalog.json()["providers"] if p["slot"] == "rerank"
            )
            assert rerank["qualified_models"] == ["rerank-2.5"]
            assert rerank["credential_readiness"] == "not_checked"
            capability = catalog.json()["selection_capabilities"][0]
            assert capability["selection"] == "output-tokenizer"
            assert capability["required_modules"] == ["tiktoken"]
            assert capability["asset_state"] == "not_checked"
            plan = await client.post(
                "/v1/models/setup-plan", json={"selections": ["output-tokenizer"]}
            )
            assert plan.status_code == 200, plan.text
            assert plan.json()["total_size_bytes"] == 10
            for _ in range(2):
                ready = await client.get(
                    "/v1/models/readiness", params={"selection": "output-tokenizer"}
                )
                assert ready.status_code == 200, ready.text
                assert ready.json()["assets"][0]["state"] == "missing"
                assert ready.json()["checked_bytes"] == 0
                assert ready.json()["complete"] is True
                assert not cache.exists()
            for selections in (["unknown"], ["output-tokenizer"] * 2, []):
                bad = await client.post(
                    "/v1/models/setup-plan", json={"selections": selections}
                )
                assert bad.status_code == 400


def test_bounded_verification_hash_size_missing_symlink_and_budget(
    tmp_path, monkeypatch
):
    payload = manifest(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    cache.mkdir()
    service = SetupService(cache, offline=True)
    assert hasattr(service, "verify_bounded")
    plan = service.plan(("output-tokenizer",))
    target = cache / "synthetic" / "asset"
    target.parent.mkdir()
    target.write_bytes(payload)
    ready = service.verify_bounded(plan, ExecutionContext())
    assert ready.assets[0].state == "installed" and ready.checked_bytes == len(payload)
    assert ready.assets[0].verification == "current_full_hash"
    budget = service.verify_bounded(plan, ExecutionContext(), byte_budget=4)
    assert budget.checked_bytes <= 4 and not budget.complete
    assert budget.assets[0].state == "not_checked"
    assert budget.assets[0].reason == "needs_full_verification"
    target.write_bytes(b"X" * len(payload))
    corrupt = service.verify_bounded(plan, ExecutionContext())
    assert corrupt.assets[0].state == "corrupt"
    assert corrupt.assets[0].verification == "current_full_hash"
    target.unlink()
    target.symlink_to(tmp_path / "manifest.json")
    assert service.verify_bounded(plan, ExecutionContext()).assets[0].state == "corrupt"
    target.unlink()
    assert service.verify_bounded(plan, ExecutionContext()).assets[0].state == "missing"
    target.write_bytes(payload)
    expired = service.verify_bounded(plan, ExecutionContext(), seconds=0)
    assert expired.checked_bytes == 0 and not expired.complete
    assert expired.assets[0].state == "not_checked"


def test_actual_reads_stay_bounded_when_file_grows_and_deadline_interrupts(
    tmp_path, monkeypatch
):
    import os
    import time

    payload = manifest(tmp_path, monkeypatch, b"A" * (2 * 1024 * 1024))
    cache = tmp_path / "cache"
    target = cache / "synthetic" / "asset"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    service = SetupService(cache, offline=True)
    plan = service.plan(("output-tokenizer",))
    original_read = os.read
    reads = []

    def growing_read(fd, count):
        if not reads:
            with target.open("ab") as output:
                output.write(b"X" * 1024)
        block = original_read(fd, count)
        reads.append(len(block))
        return block

    monkeypatch.setattr(os, "read", growing_read)
    result = service.verify_bounded(plan, ExecutionContext(), byte_budget=len(payload))
    assert sum(reads) == result.checked_bytes == len(payload)
    assert (
        result.assets[0].state == "not_checked"
        and result.assets[0].reason == "file_changed"
    )
    target.write_bytes(payload)
    ticks = [0.0]

    def delayed_read(fd, count):
        block = original_read(fd, count)
        ticks[0] += 6
        return block

    monkeypatch.setattr(os, "read", delayed_read)
    monkeypatch.setattr(time, "monotonic", lambda: ticks[0])
    result = service.verify_bounded(plan, ExecutionContext())
    assert result.checked_bytes == 1024 * 1024
    assert result.assets[0].state == "not_checked" and not result.complete


@pytest.mark.asyncio
async def test_explicit_setup_receipt_is_historical_not_current_readiness(
    tmp_path, monkeypatch
):
    payload = manifest(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    target = cache / "synthetic" / "asset"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(cache))
    original_verify = SetupService.verify_bounded
    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            result = await client.post(
                "/v1/jobs",
                json={
                    "operation": "setup",
                    "assets": ["output-tokenizer"],
                    "offline": True,
                },
            )
            assert result.status_code == 202, result.text
            assert result.json()["job"]["state"] == "succeeded"
            monkeypatch.setattr(
                SetupService,
                "verify_bounded",
                lambda self, plan, ctx: original_verify(self, plan, ctx, byte_budget=4),
            )
            readiness = await client.get(
                "/v1/models/readiness", params={"selection": "output-tokenizer"}
            )
            assert readiness.status_code == 200, readiness.text
            body = readiness.json()
            assert body["assets"][0]["state"] == "not_checked" and not body["complete"]
            assert body["receipts"][0]["job_id"] == result.json()["job"]["id"]
            assert body["receipts"][0]["verification"] == "durable_setup_result"
            assert body["receipts"][0]["verified_at"]
            assert body["receipts"][0]["manifest_sha256"] == body["manifest_sha256"]
            target.write_bytes(b"XXXXXXXXXX")
            monkeypatch.setattr(SetupService, "verify_bounded", original_verify)
            body = (
                await client.get(
                    "/v1/models/readiness", params={"selection": "output-tokenizer"}
                )
            ).json()
            assert body["assets"][0]["state"] == "corrupt" and body["receipts"]


def test_shrinking_asset_remains_unknown_after_partial_hash(tmp_path, monkeypatch):
    import os

    payload = manifest(tmp_path, monkeypatch, b"A" * (2 * 1024 * 1024))
    cache = tmp_path / "cache"
    target = cache / "synthetic" / "asset"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    service = SetupService(cache, offline=True)
    plan = service.plan(("output-tokenizer",))
    original_read = os.read
    reads = []

    def shrinking_read(fd, count):
        block = original_read(fd, count)
        reads.append(len(block))
        if len(reads) == 1:
            with target.open("r+b") as stream:
                stream.truncate(len(block))
        return block

    monkeypatch.setattr(os, "read", shrinking_read)
    result = service.verify_bounded(plan, ExecutionContext())
    assert result.assets[0].state == "not_checked"
    assert result.assets[0].reason == "file_changed"
    assert result.assets[0].verification == "none"
    assert result.checked_bytes == sum(reads) == 1024 * 1024
    assert not result.complete and not result.selections[0].complete
