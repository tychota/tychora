"""Discover registered definitions and inspect scoped records."""

from pathlib import Path

import typer

from ._common import client, emit, fail, selected_format, state

_DISCOVERY = {
    "kinds",
    "facets",
    "relations",
    "sources",
    "profiles",
    "languages",
    "settings",
    "runs",
    "models",
}
_RECORDS = {"evidence", "assets", "revisions", "jobs", "runs", "diagnostics"}


def register(app: typer.Typer) -> None:
    @app.command()
    def inspect(
        ctx: typer.Context,
        subject: str = typer.Argument(
            help="Kinds, facets, relations, sources, capabilities, config, evidence, assets, run, job, or diagnostics."
        ),
        id: str | None = typer.Argument(None),
        original: bool = typer.Option(
            False, help="Inspect a retained original, when authorized."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Inspect kinds, source definitions, evidence and original bindings."""
        from tychora.contracts.errors import TychoraError

        options = state(ctx)
        try:
            output_format = selected_format(options, json_output)
            known = (
                (subject == "config" and id is None)
                or (subject in _DISCOVERY | {"capabilities"} and id is None)
                or (subject in _RECORDS | {"job", "run", "sources"} and id is not None)
            )
            if not known:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Unknown inspect subject or missing ID; run inspect kinds for definitions.",
                )
            if subject == "config":
                if id is not None:
                    raise TychoraError(
                        "INVALID_REQUEST", "Config inspection takes no ID."
                    )
                from tychora.configuration.resolve import (
                    resolve_config,
                    selected_workspace_profile,
                )

                path = Path(options.workspace) / "workspace.toml"
                result = resolve_config(
                    path, options.profile or selected_workspace_profile(path), {}
                ).inspect()
            else:
                with client(options) as kb:
                    if subject == "capabilities":
                        subject = "settings"
                    if subject in _DISCOVERY and id is None:
                        result = kb.discover({"subject": subject})
                    elif subject in _RECORDS and id is not None:
                        result = kb.inspect(
                            {
                                "subject": subject,
                                "id": id,
                                "original": original,
                                "view": "original" if original else "summary",
                            }
                        )
                    elif subject == "job" and id is not None:
                        result = kb.jobs.get(id)
                    elif subject == "run" and id is not None:
                        result = kb.inspect({"subject": "runs", "id": id})
                    elif subject == "sources" and id is not None:
                        result = kb.sources.revisions(id)
                    else:
                        raise TychoraError(
                            "INVALID_REQUEST",
                            "Unknown inspect subject or missing ID; run inspect kinds for definitions.",
                        )
            emit(result, format=output_format)
        except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
            fail(error)
