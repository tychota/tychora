"""Narrow public hosting for the packaged dashboard, without workspace data."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse

from tychora.contracts.errors import TychoraError

DASHBOARD_ROOT = Path(__file__).parents[2] / "resources" / "dashboard"
_ID = r"[A-Za-z0-9_-]{1,128}"
_PAGES = re.compile(
    rf"/dashboard/(?:|search|sources|sources/{_ID}/revisions/{_ID}|"
    rf"jobs(?:/{_ID})?|evaluations(?:/{_ID})?|activity|runs/{_ID}|settings)"
)
_ASSETS = re.compile(
    r"/dashboard/assets/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def is_dashboard_static_request(scope: dict) -> bool:
    """Only known app route shapes or safe asset names may omit a credential."""
    return scope.get("method") in {"GET", "HEAD"} and bool(
        _PAGES.fullmatch(scope.get("path", ""))
        or _ASSETS.fullmatch(scope.get("path", ""))
    )


def _contained_file(root: Path, relative: str) -> Path | None:
    try:
        root = root.resolve()
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except (OSError, RuntimeError):
        # Missing, inaccessible, or cyclic paths are never a public file.
        pass
    return None


def register_dashboard(app: FastAPI) -> None:
    app.state.dashboard_root = DASHBOARD_ROOT

    @app.api_route("/dashboard/{path:path}", methods=["GET", "HEAD"])
    async def dashboard(request: Request, path: str):
        if not is_dashboard_static_request(request.scope):
            raise TychoraError("NOT_FOUND", "Dashboard resource is unavailable.")
        root = Path(request.app.state.dashboard_root)
        if _PAGES.fullmatch(request.scope["path"]):
            file = _contained_file(root, "index.html")
            if file is None:
                return PlainTextResponse(
                    "Dashboard bundle is unavailable. From dashboard/ run npm ci and npm run build.",
                    status_code=503,
                    headers=SECURITY_HEADERS,
                )
        else:
            file = _contained_file(root, path)
            if file is None:
                return PlainTextResponse(
                    "Dashboard asset is unavailable.",
                    status_code=404,
                    headers=SECURITY_HEADERS,
                )
        return FileResponse(file, headers=SECURITY_HEADERS)
