"""Question, passage and image query entry points."""

# ruff: noqa: B008 - Typer declares CLI options in function defaults.

from pathlib import Path
from typing import Any

import typer

from ._common import client, emit, fail, load_json, selected_format, state


def register(app: typer.Typer) -> None:
    @app.command()
    def search(
        ctx: typer.Context,
        question: str | None = typer.Argument(None),
        request_file: Path | None = typer.Option(
            None, help="Complete public QueryRequest JSON."
        ),
        passage: str | None = typer.Option(None, help="Search by passage text."),
        image: str | None = typer.Option(
            None, help="Search by registered image asset ID."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Search evidence; use a request file for filters, limits and budgets."""
        from tychora.contracts.errors import TychoraError

        options = state(ctx)
        try:
            selected = selected_format(options, json_output)
            if (
                sum(
                    value is not None
                    for value in (question, request_file, passage, image)
                )
                != 1
            ):
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Provide one question, --passage, --image, or --request-file.",
                )
            request: dict[str, Any]
            if request_file:
                request = load_json(request_file)
            elif passage is not None:
                request = {"input": {"type": "passage", "text": passage}}
            elif image is not None:
                request = {"input": {"type": "image", "asset_ref": image}}
            else:
                request = {"question": question}
            if options.profile:
                request.setdefault("profile", options.profile)
            with client(options) as kb:
                result = kb.search(request)
                if selected in {"markdown", "agent"}:
                    from tychora import OutputProfile, format_result

                    typer.echo(
                        format_result(result, OutputProfile(renderer=f"{selected}@1"))
                    )
                else:
                    emit(result, format="json")
        except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
            fail(error)
