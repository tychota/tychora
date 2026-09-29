"""Source submission using public source DTOs."""

# ruff: noqa: B008 - Typer declares CLI options in function defaults.

from pathlib import Path
from time import monotonic

import typer

from ._common import client, emit, fail, load_json, progress, selected_format, state


def wait_with_progress(jobs, handle):
    """Poll one bounded job while printing each durable stage exactly once."""
    from tychora.contracts.errors import TychoraError

    deadline = monotonic() + 30.0
    cursor = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TychoraError("DEADLINE_EXCEEDED", "Source job is still running.")
        status = jobs.wait(handle.id, timeout=min(0.25, remaining))
        while True:
            events = jobs.events_after(handle.id, after=cursor)
            for event in events:
                cursor = event.sequence
                progress(f"Job {handle.id} stage={event.stage} state={event.state}")
            if len(events) < 100:
                break
        if status.state in {"succeeded", "failed", "cancelled", "interrupted"}:
            return status


def register(app: typer.Typer) -> None:
    @app.command()
    def add(
        ctx: typer.Context,
        path: Path | None = typer.Argument(None),
        kind: str | None = typer.Option(None, help="Registered kind, e.g. manual@1."),
        metadata_file: Path | None = typer.Option(
            None, help="JSON metadata matching the kind schema."
        ),
        request_file: Path | None = typer.Option(
            None, help="Complete public SourceInput JSON."
        ),
        sidecar: list[Path] | None = typer.Option(
            None, help="Additional metadata sidecar."
        ),
        collection: str = typer.Option(
            "default", help="Granted collection ID or default."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Add a file, folder, or repository. Inspect kinds before choosing metadata."""
        from tychora.contracts.errors import TychoraError

        options = state(ctx)
        try:
            output_format = selected_format(options, json_output)
            if request_file:
                if path is not None or kind or metadata_file or sidecar:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "--request-file conflicts with source shorthand fields.",
                    )
                source = load_json(request_file)
            else:
                if path is None:
                    raise TychoraError(
                        "INVALID_REQUEST", "Add requires a path or --request-file."
                    )
                if not kind:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "Add requires --kind and schema-valid --metadata-file; run inspect kinds.",
                    )
                source = {
                    "type": "folder" if path.is_dir() else "file",
                    "source_key": path.name,
                    "path": str(path.resolve()),
                    "collection_id": collection,
                    "kind": kind,
                    "metadata": load_json(metadata_file) if metadata_file else {},
                    "sidecars": [str(item.resolve()) for item in sidecar or []],
                }
            with client(options) as kb:
                handle = kb.sources.add(source)
                progress(f"Accepted {handle.operation} job {handle.id}")
                status = wait_with_progress(kb.jobs, handle)
                emit(status, format=output_format)
                if status.state != "succeeded":
                    raise TychoraError(
                        status.error_code or "JOB_INTERRUPTED",
                        "Source job did not succeed.",
                    )
        except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
            fail(error)
