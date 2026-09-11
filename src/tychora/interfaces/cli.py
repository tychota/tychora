import typer

app = typer.Typer(
    no_args_is_help=True,
    help="Discover and retrieve versioned evidence.",
)


@app.callback()
def root() -> None:
    """Configure a workspace, add sources, search and inspect evidence."""


def main() -> None:
    app()
