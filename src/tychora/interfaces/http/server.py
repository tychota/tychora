"""One-owner, one-worker loopback HTTP process."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from tychora import Tychora

from .app import create_app
from .auth import LocalAuth
from .launch import launch_url

if TYPE_CHECKING:
    from tychora.configuration.models import ResolvedConfig


async def serve(
    path: Path,
    *,
    port: int = 0,
    token_counter=None,
    config: ResolvedConfig | None = None,
    announce: bool = True,
) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("Port is out of range")
    with Tychora.open(path, token_counter=token_counter, config=config) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
            authority = f"127.0.0.1:{sock.getsockname()[1]}"
            token = owner._runtime.record["token"]
            if not isinstance(token, str):
                raise TypeError("Owner runtime token is invalid")
            auth = LocalAuth(
                {token: owner.scope},
                authority=authority,
                managed_owner_token=token,
                launch_token=token,
            )
            app = create_app(owner, services, auth)
            owner._runtime.publish_endpoint(authority)
            if announce and auth.launch_codes is not None:
                # The one-time code sits in the URL fragment: it never reaches
                # request lines, logs or Referer headers.
                print(
                    "Tychora dashboard (one-time link, expires in "
                    f"{int(auth.launch_codes.ttl)} s): "
                    f"{launch_url(auth.origin, auth.launch_codes.mint())}\n"
                    "Need another? Run: tychora --workspace "
                    f"{shlex.quote(str(path))} manage dashboard-link",
                    flush=True,
                )
            uvicorn_config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=sock.getsockname()[1],
                workers=1,
                log_level="warning",
            )
            server = uvicorn.Server(uvicorn_config)
            try:
                await server.serve(sockets=[sock])
            finally:
                try:
                    await app.state.runtime.services.source_jobs.aclose()
                finally:
                    await app.state.runtime.aclose_retired()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local Tychora workspace")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(serve(args.workspace, port=args.port))


if __name__ == "__main__":
    main()
