"""Small public command adapter. Runtime imports occur only inside commands."""

# ruff: noqa: B008 - Typer declares CLI options in function defaults.

from pathlib import Path

import typer

from .commands import add, init, inspect, manage, search, serve
from .commands._common import CLIState

app = typer.Typer(
    no_args_is_help=True,
    help="Discover and retrieve versioned evidence.",
    epilog=(
        "Examples: tychora init --provider local; tychora inspect kinds --json; "
        "tychora add guide.pdf --kind manual@1 --metadata-file guide.metadata.json; "
        "tychora add --request-file code-source.json; "
        "tychora search --request-file exact-query.json; "
        "tychora inspect revisions REVISION_ID --original --json"
    ),
)


@app.callback()
def root(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path("."), help="Local workspace directory."),
    profile: str | None = typer.Option(None, help="Configured profile name."),
    offline: bool = typer.Option(False, help="Disable external provider use."),
    format: str | None = typer.Option(None, help="Output: json, markdown, or agent."),
    json_output: bool = typer.Option(False, "--json", help="Canonical JSON on stdout."),
) -> None:
    """Configure a workspace, add sources, search and inspect evidence."""
    if json_output and format not in (None, "json"):
        raise typer.BadParameter("--json conflicts with --format")
    if format not in (None, "json", "markdown", "agent"):
        raise typer.BadParameter("Format must be json, markdown, or agent")
    ctx.obj = CLIState(workspace, profile, offline, "json" if json_output else format)


init.register(app)
add.register(app)
search.register(app)
inspect.register(app)
serve.register(app)
manage.register(app)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
