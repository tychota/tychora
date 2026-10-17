"""One-time dashboard launch codes over the real ASGI application."""

import pytest
from httpx import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.interfaces.http.launch import LaunchCodes

ORIGIN = "http://127.0.0.1:8765"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def launch_app(tmp_path):
    with Tychora.open(tmp_path / "knowledge") as kb:
        reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
        manager = kb.scope  # same grant, different credential
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": kb.scope, "reader": reader, "manager": manager},
                authority="127.0.0.1:8765",
                launch_token="owner-token",
            ),
        )
        yield app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)


async def _mint(client, token="owner-token", **headers):
    return await client.post(
        "/v1/session/launch-codes",
        headers={"Authorization": f"Bearer {token}", **headers},
    )


@pytest.mark.asyncio
async def test_minted_code_exchanges_once_for_the_owner_token(launch_app):
    async with _client(launch_app) as client:
        minted = await _mint(client)
        assert minted.status_code == 201, minted.text
        body = minted.json()
        code = body["code"]
        assert body["expires_in_seconds"] == 120
        assert body["url"] == f"{ORIGIN}/dashboard/#launch={code}"
        assert "?" not in body["url"]
        exchanged = await client.post(
            "/v1/session/launch",
            json={"code": code},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert exchanged.status_code == 200, exchanged.text
        assert exchanged.json() == {
            "schema_version": "1",
            "token_type": "Bearer",
            "token": "owner-token",
        }
        assert exchanged.headers["cache-control"] == "no-store"
        handshake = await client.get(
            "/v1/handshake",
            headers={"Authorization": f"Bearer {exchanged.json()['token']}"},
        )
        assert handshake.status_code == 200
        reused = await client.post("/v1/session/launch", json={"code": code})
        assert reused.status_code == 403
        assert reused.json()["message"] == "Launch link is invalid or expired."


@pytest.mark.asyncio
async def test_wrong_malformed_and_cross_origin_requests_are_rejected(launch_app):
    async with _client(launch_app) as client:
        code = (await _mint(client)).json()["code"]
        wrong = await client.post("/v1/session/launch", json={"code": "x" * 43})
        assert wrong.status_code == 403
        assert wrong.json()["message"] == "Launch link is invalid or expired."
        for headers in (
            {"Origin": "http://127.0.0.1:9999"},
            {"Origin": "https://evil.invalid"},
            {"Origin": "null"},
            {"Host": "localhost:8765"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            response = await client.post(
                "/v1/session/launch", json={"code": code}, headers=headers
            )
            assert response.status_code == 403, (headers, response.text)
        get = await client.get("/v1/session/launch", params={"code": code})
        assert get.status_code == 400
        form = await client.post("/v1/session/launch", data={"code": code})
        assert form.status_code == 400
        for payload in (b"[]", b"{}", b'{"code": 1}', b'{"code": "a", "x": 1}'):
            response = await client.post(
                "/v1/session/launch", content=payload, headers=JSON
            )
            assert response.status_code in {400, 403}, payload
        huge = await client.post(
            "/v1/session/launch",
            content=b'{"code":"' + b"a" * 2000 + b'"}',
            headers=JSON,
        )
        assert huge.status_code == 400
        # None of the rejected attempts consumed the genuine code.
        ok = await client.post("/v1/session/launch", json={"code": code})
        assert ok.status_code == 200


@pytest.mark.asyncio
async def test_minting_requires_the_launch_token_with_manage_scope(launch_app):
    async with _client(launch_app) as client:
        assert (await client.post("/v1/session/launch-codes")).status_code == 403
        assert (await _mint(client, "reader")).status_code == 403
        # A different credential cannot mint a code that yields the owner token.
        assert (await _mint(client, "manager")).status_code == 403
        narrowed = await _mint(
            client,
            **{
                "X-Tychora-Scope": launch_app.state.owner.scope.model_copy(
                    update={"capabilities": frozenset({"read"})}
                ).model_dump_json()
            },
        )
        assert narrowed.status_code == 403
        assert (await _mint(client)).status_code == 201


@pytest.mark.asyncio
async def test_launch_is_disabled_without_explicit_composition(tmp_path):
    with Tychora.open(tmp_path / "knowledge") as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with _client(app) as client:
            assert (await _mint(client, "owner")).status_code == 503
            response = await client.post("/v1/session/launch", json={"code": "a" * 43})
            assert response.status_code == 404


def test_launch_token_must_be_granted(tmp_path):
    with Tychora.open(tmp_path / "knowledge") as kb, pytest.raises(TychoraError):
        LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765", launch_token="x")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_codes_expire_and_are_single_use():
    clock = Clock()
    codes = LaunchCodes("secret-token", clock=clock)
    code = codes.mint()
    assert len(code) >= 43
    clock.now += 119
    assert codes.redeem(code) == "secret-token"
    with pytest.raises(TychoraError):
        codes.redeem(code)
    late = codes.mint()
    clock.now += 120
    with pytest.raises(TychoraError) as expired:
        codes.redeem(late)
    assert expired.value.code == "FORBIDDEN_SCOPE"
    assert codes.outstanding == 0
    with pytest.raises(TychoraError):
        LaunchCodes("secret-token", ttl=121)


def test_outstanding_codes_are_bounded_by_evicting_the_oldest():
    codes = LaunchCodes("secret-token", clock=Clock(), max_outstanding=3)
    minted = [codes.mint() for _ in range(5)]
    assert codes.outstanding == 3
    for stale in minted[:2]:
        with pytest.raises(TychoraError):
            codes.redeem(stale)
    assert codes.redeem(minted[-1]) == "secret-token"


def test_failed_attempts_are_rate_limited_within_a_window():
    clock = Clock()
    codes = LaunchCodes("secret-token", clock=clock, max_failures=3)
    good = codes.mint()
    for _ in range(3):
        with pytest.raises(TychoraError) as error:
            codes.redeem("y" * 43)
        assert error.value.code == "FORBIDDEN_SCOPE"
    with pytest.raises(TychoraError) as limited:
        codes.redeem(good)
    assert limited.value.code == "WORKSPACE_BUSY"
    clock.now += 61
    assert codes.redeem(good) == "secret-token"
