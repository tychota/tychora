"""Explicit local workspace creation."""

# ruff: noqa: B008 - Typer declares CLI options in function defaults.

from pathlib import Path

import typer

from ._common import emit, fail, selected_format, state


def register(app: typer.Typer) -> None:
    @app.command()
    def init(
        ctx: typer.Context,
        provider: str = typer.Option(
            "local", help="Select local or configured provider profile."
        ),
        config: Path | None = typer.Option(
            None, help="Existing workspace TOML to install."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Create a local workspace; no model assets are downloaded."""
        from tychora import Tychora
        from tychora.contracts.errors import TychoraError

        options = state(ctx)
        try:
            output_format = selected_format(options, json_output)
            if provider not in {"local", "configured"}:
                raise TychoraError(
                    "INVALID_REQUEST", "Provider must be local or configured."
                )
            if provider == "configured" and config is None:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Configured providers require --config workspace.toml.",
                )
            if config is not None:
                from tychora.configuration.resolve import (
                    resolve_config,
                    selected_workspace_profile,
                )

                resolved = resolve_config(
                    config, options.profile or selected_workspace_profile(config), {}
                )
                options.workspace.mkdir(parents=True, exist_ok=True)
                (options.workspace / "workspace.toml").write_bytes(config.read_bytes())
                with Tychora.open(options.workspace, config=resolved):
                    pass
            else:
                with Tychora.open(options.workspace):
                    pass
            emit(
                {
                    "schema_version": "1",
                    "workspace": str(options.workspace),
                    "provider": provider,
                },
                format=output_format,
            )
        except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
            fail(error)
