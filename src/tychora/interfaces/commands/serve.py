"""One-owner loopback HTTP and MCP host."""

from pathlib import Path

import typer

from ._common import fail, resolved_config, state


def register(app: typer.Typer) -> None:
    @app.command()
    def serve(
        ctx: typer.Context,
        stdio: bool = typer.Option(
            False,
            help="Serve MCP over protocol-only stdout with private loopback delegation.",
        ),
        port: int = typer.Option(
            0, help="Loopback HTTP port; zero chooses a free port."
        ),
    ) -> None:
        """Serve local HTTP plus MCP, or MCP stdio with a private control endpoint."""
        import asyncio

        options = state(ctx)
        try:
            config = resolved_config(options)
            if stdio:
                from tychora.interfaces.mcp.server import serve_stdio

                asyncio.run(serve_stdio(Path(options.workspace), config=config))
            else:
                from tychora.interfaces.http.server import serve as serve_http

                asyncio.run(
                    serve_http(Path(options.workspace), port=port, config=config)
                )
        except KeyboardInterrupt:
            raise typer.Exit(130) from None
        except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
            fail(error)
