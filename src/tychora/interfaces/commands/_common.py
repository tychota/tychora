"""Shared command I/O and owner delegation without runtime imports at help time."""

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import typer


@dataclass(frozen=True)
class CLIState:
    workspace: Path
    profile: str | None
    offline: bool
    format: str | None


def state(ctx: typer.Context) -> CLIState:
    return ctx.obj


def resolved_config(options: CLIState):
    """Resolve explicit profile/offline intent before owner construction."""
    from tychora.contracts.errors import TychoraError

    if not (options.profile or options.offline):
        return None
    settings = options.workspace / "workspace.toml"
    if not settings.exists():
        if options.profile:
            raise TychoraError(
                "INVALID_REQUEST", "Initialize a workspace before selecting --profile."
            )
        return None  # A newly created workspace uses the built-in offline profile.
    from tychora.configuration.resolve import (
        resolve_config,
        selected_workspace_profile,
    )

    config = resolve_config(
        settings, options.profile or selected_workspace_profile(settings), {}
    )
    if options.offline and not config.values.offline:
        raise TychoraError(
            "CAPABILITY_MISSING", "Select an offline workspace profile for --offline."
        )
    return config


@contextmanager
def client(options: CLIState):
    from tychora import Tychora
    from tychora.contracts.errors import TychoraError

    config = resolved_config(options)
    try:
        kb = Tychora.open(options.workspace, config=config)
    except TychoraError as error:
        if error.code != "WORKSPACE_BUSY":
            raise
        if options.profile:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Cannot verify the busy owner's profile; retry without --profile or stop that owner.",
            ) from None
        if options.offline:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "An offline command cannot delegate to an already running owner with unknown provider state.",
            ) from None
        from tychora.interfaces.http.client import connect_local

        kb = connect_local(options.workspace)
    try:
        yield kb
    finally:
        kb.close()


def emit(value, *, format: str | None = None) -> None:
    from tychora.contracts.base import thaw_json

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    value = thaw_json(value)
    if isinstance(value, dict) and "schema_version" not in value:
        value = {"schema_version": "1", **value}
    if format in (None, "json"):
        typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, str):
        typer.echo(value)
    else:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def progress(message: str) -> None:
    print(message, file=sys.stderr)


def selected_format(options: CLIState, json_output: bool) -> str | None:
    if json_output and options.format not in (None, "json"):
        from tychora.contracts.errors import TychoraError

        raise TychoraError("INVALID_REQUEST", "--json conflicts with --format.")
    return "json" if json_output else options.format


def fail(error: Exception) -> None:
    from pydantic import ValidationError

    from tychora.contracts.errors import TychoraError

    if isinstance(error, TychoraError):
        public = error
    elif isinstance(
        error, (ValidationError, ValueError, OSError, json.JSONDecodeError)
    ):
        public = TychoraError(
            "INVALID_REQUEST",
            "Invalid command input; inspect kinds and request schemas for valid fields.",
        )
    else:
        raise error
    print(public.to_json(), file=sys.stderr)
    raise typer.Exit(public.cli_exit_code)


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Request file must contain an object")  # noqa: TRY004 - user JSON shape
    return value
