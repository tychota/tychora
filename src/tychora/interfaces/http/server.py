"""One-owner, one-worker loopback HTTP process."""

from __future__ import annotations

import argparse
import asyncio
import socket
from pathlib import Path

import uvicorn

from tychora import Tychora

from .app import create_app
from .auth import LocalAuth


async def serve(path: Path, *, port: int = 0, token_counter=None) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("Port is out of range")
    with Tychora.open(path, token_counter=token_counter) as kb:
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
            auth = LocalAuth({token: owner.scope}, authority=authority)
            app = create_app(owner, services, auth)
            owner._runtime.publish_endpoint(authority)
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=sock.getsockname()[1],
                workers=1,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            try:
                await server.serve(sockets=[sock])
            finally:
                await app.state.runtime.services.source_jobs.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local Tychora workspace")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(serve(args.workspace, port=args.port))


if __name__ == "__main__":
    main()
