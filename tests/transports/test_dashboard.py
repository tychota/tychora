"""Dashboard files are public only inside the strict local HTTP boundary."""

import pytest
from httpx import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


@pytest.fixture
def dashboard_app(tmp_path):
    root = tmp_path / "bundle"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<html><script src="/dashboard/assets/app.js"></script></html>'
    )
    (root / "assets" / "app.js").write_text("console.log('dashboard');")
    (tmp_path / "private.txt").write_text("workspace secret")
    (root / "assets" / "outside.js").symlink_to(tmp_path / "private.txt")
    (root / "assets" / "loop.js").symlink_to(root / "assets" / "loop.js")
    with Tychora.open(tmp_path / "knowledge") as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        app.state.dashboard_root = root
        yield app


@pytest.mark.asyncio
async def test_public_shell_assets_and_exact_application_routes(dashboard_app):
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        for path in (
            "/dashboard",
            "/dashboard/",
            "/dashboard/search",
            "/dashboard/collections",
            "/dashboard/collections/collection-1",
            "/dashboard/documents",
            "/dashboard/documents/add",
            "/dashboard/documents/source-1",
            "/dashboard/documents/source-1/revisions/revision-1",
            "/dashboard/explore",
            "/dashboard/models",
            "/dashboard/sources",
            "/dashboard/sources/source-1",
            "/dashboard/sources/source-1/revisions/revision-1",
            "/dashboard/jobs",
            "/dashboard/jobs/job-1",
            "/dashboard/evaluations",
            "/dashboard/evaluations/evaluation-1",
            "/dashboard/activity",
            "/dashboard/runs/run-1",
            "/dashboard/settings",
        ):
            shell = await client.get(path)
            assert shell.status_code == 200, (path, shell.text)
            assert shell.headers["cache-control"] == "no-store"
            assert shell.headers["x-content-type-options"] == "nosniff"
            assert shell.headers["referrer-policy"] == "no-referrer"
            assert shell.headers["x-frame-options"] == "DENY"
            csp = shell.headers["content-security-policy"]
            assert "default-src 'none'" in csp
            assert "script-src 'self'" in csp
            assert "connect-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "unsafe-inline" not in csp
        asset = await client.get("/dashboard/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('dashboard');"
        assert asset.headers["cache-control"] == "no-store"
        for path in ("/dashboard/", "/dashboard/assets/app.js"):
            head = await client.head(path)
            assert head.status_code == 200
            assert head.content == b""


@pytest.mark.asyncio
async def test_public_requests_still_require_unambiguous_local_authority(dashboard_app):
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        for headers, status in (
            ({"Host": "evil.invalid"}, 403),
            ({"Host": "localhost:8765"}, 403),
            ({"Host": "127.0.0.1:8766"}, 403),
            ({"Origin": "https://evil.invalid"}, 403),
            ([("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:8765")], 400),
            (
                [
                    ("Origin", "http://127.0.0.1:8765"),
                    ("Origin", "http://127.0.0.1:8765"),
                ],
                400,
            ),
            (
                [("Authorization", "Bearer owner"), ("Authorization", "Bearer owner")],
                400,
            ),
            ([("Content-Length", "0"), ("Content-Length", "0")], 400),
        ):
            response = await client.get("/dashboard/", headers=headers)
            assert response.status_code == status, response.text
        assert (
            await client.get("/dashboard/", headers={"Origin": "http://127.0.0.1:8765"})
        ).status_code == 200


@pytest.mark.asyncio
async def test_dashboard_admission_cannot_serve_arbitrary_or_escaping_files(
    dashboard_app,
):
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        for path in (
            "/dashboard/assets/absent.js",
            "/dashboard/assets/outside.js",
            "/dashboard/assets/loop.js",
        ):
            response = await client.get(path)
            assert response.status_code == 404
            assert "workspace secret" not in response.text
        for path in (
            "/dashboard/private.txt",
            "/dashboard/unknown",
            "/dashboard/index.html",
            "/dashboard/assets/%2e%2e/private.txt",
            "/dashboard/assets/%2e%2e/index.html",
            "/dashboard/assets/app.js/extra",
            "/dashboard/sources/x/extra",
            "/dashboard/collections/x/extra",
            "/dashboard/documents/x/extra",
            "/dashboard/models/x",
            "/dashboard/explore/x",
        ):
            response = await client.get(path)
            assert response.status_code in {403, 404}
            assert "workspace secret" not in response.text
            authenticated = await client.get(
                path, headers={"Authorization": "Bearer owner"}
            )
            assert authenticated.status_code == 404
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            response = await client.request(method, "/dashboard/assets/app.js")
            assert response.status_code == 403
            authorized = await client.request(
                method,
                "/dashboard/assets/app.js",
                headers={"Authorization": "Bearer owner"},
            )
            assert authorized.status_code in {400, 405}
            assert "console.log" not in authorized.text


@pytest.mark.asyncio
async def test_api_and_mcp_remain_authenticated_and_uncacheable(dashboard_app):
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        for path in ("/v1/handshake", "/v1/sources", "/mcp/"):
            denied = await client.get(path)
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "no-store"
        handshake = await client.get(
            "/v1/handshake", headers={"Authorization": "Bearer owner"}
        )
        assert handshake.status_code == 200
        assert handshake.headers["cache-control"] == "no-store"
        invalid = await client.post(
            "/v1/queries", json={}, headers={"Authorization": "Bearer owner"}
        )
        assert invalid.status_code == 400
        assert invalid.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_absent_bundle_is_clear_and_does_not_prevent_api_construction(
    dashboard_app, tmp_path
):
    dashboard_app.state.dashboard_root = tmp_path / "not-built"
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        shell = await client.get("/dashboard/")
        assert shell.status_code == 503
        assert "npm" in shell.text and "build" in shell.text
        assert shell.headers["cache-control"] == "no-store"
        api = await client.get(
            "/v1/handshake", headers={"Authorization": "Bearer owner"}
        )
        assert api.status_code == 200


@pytest.mark.asyncio
async def test_asset_symlink_cannot_serve_dashboard_sibling_file(dashboard_app):
    root = dashboard_app.state.dashboard_root
    (root / "private.txt").write_text("non-asset sibling secret")
    (root / "assets" / "sibling.js").symlink_to("../private.txt")
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.get("/dashboard/assets/sibling.js")
        assert response.status_code == 404
        assert "non-asset sibling secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("outside_package", [False, True])
async def test_symlinked_assets_directory_cannot_escape_asset_boundary(
    dashboard_app, tmp_path, outside_package
):
    root = dashboard_app.state.dashboard_root
    (root / "assets").rename(root / "original-assets")
    target = (tmp_path if outside_package else root) / "private-assets"
    target.mkdir()
    (target / "private.js").write_text("non-asset directory secret")
    (root / "assets").symlink_to(target, target_is_directory=True)
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.get("/dashboard/assets/private.js")
        assert response.status_code == 404
        assert "non-asset directory secret" not in response.text
