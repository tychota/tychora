"""Discoverable maintenance mapping over the public maintenance DTO."""

# ruff: noqa: B008 - Typer declares CLI options in function defaults.

from pathlib import Path

import typer

from ._common import client, emit, fail, progress, selected_format, state

group = typer.Typer(
    no_args_is_help=True, help="Setup, synchronize and maintain a workspace."
)


def _run(ctx: typer.Context, request: dict, *, json_output: bool = False) -> None:
    from tychora.contracts.operations import MaintenanceRequest

    options = state(ctx)
    try:
        output_format = selected_format(options, json_output)
        typed = MaintenanceRequest.from_mapping(request)
        with client(options) as kb:
            result = kb.management.run(typed)
            if result.job is not None:
                progress(f"Accepted {result.job.operation} job {result.job.id}")
            emit(result, format=output_format)
    except Exception as error:  # noqa: BLE001 - command boundary rethrows unknown exceptions
        fail(error)


def register(app: typer.Typer) -> None:
    app.add_typer(group, name="manage")


@group.command()
def setup(
    ctx: typer.Context,
    asset: list[str] = typer.Option(..., help="Manifest selection, repeatable."),
    bundle: Path | None = typer.Option(None, help="Verified local bundle directory."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Provision explicitly selected manifest assets; no implicit model downloads."""
    options = state(ctx)
    _run(
        ctx,
        {
            "operation": "setup",
            "assets": asset,
            "local_bundle": str(bundle) if bundle else None,
            "offline": options.offline or bundle is not None,
        },
        json_output=json_output,
    )


@group.command()
def sync(ctx: typer.Context, source_id: list[str] = typer.Argument(...)) -> None:
    """Synchronize registered source IDs."""
    _run(ctx, {"operation": "sync", "source_ids": source_id})


@group.command()
def remove(ctx: typer.Context, source_id: list[str] = typer.Argument(...)) -> None:
    """Remove registered source IDs."""
    _run(ctx, {"operation": "remove", "source_ids": source_id})


@group.command()
def rebuild(ctx: typer.Context, channel: list[str] = typer.Argument(...)) -> None:
    """Rebuild configured projection channels."""
    _run(ctx, {"operation": "rebuild", "channels": channel})


@group.command("export")
def export_data(ctx: typer.Context, destination: str) -> None:
    """Export a bounded workspace archive."""
    _run(ctx, {"operation": "export", "destination": destination})


@group.command("import")
def import_data(ctx: typer.Context, archive_path: str) -> None:
    """Import an explicit local archive."""
    _run(ctx, {"operation": "import", "archive_path": archive_path})


@group.command()
def health(ctx: typer.Context, verify_payloads: bool = False) -> None:
    """Inspect workspace health."""
    _run(ctx, {"operation": "health", "verify_payloads": verify_payloads})


@group.command()
def evaluate(ctx: typer.Context, dataset_id: str, split: str) -> None:
    """Run a registered evaluation dataset split."""
    _run(ctx, {"operation": "evaluate", "dataset_id": dataset_id, "split": split})


@group.command("purge-originals")
def purge_originals(
    ctx: typer.Context,
    source_id: str = typer.Argument(..., help="Withdrawn source ID."),
    revision_id: list[str] = typer.Argument(...),
    confirm: bool = False,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Preview retained-original removal; execution requires explicit confirmation."""
    _run(
        ctx,
        {
            "operation": "purge-originals",
            "source_id": source_id,
            "revision_ids": revision_id,
            "confirm_original_removal": confirm,
        },
        json_output=json_output,
    )


@group.command("purge-history")
def purge_history(ctx: typer.Context, before: str) -> None:
    """Purge history before an ISO 8601 timestamp."""
    _run(ctx, {"operation": "history-purge", "before": before})


@group.command("purge-cache")
def purge_cache(ctx: typer.Context, namespace: list[str] = typer.Argument(...)) -> None:
    """Purge named cache namespaces."""
    _run(ctx, {"operation": "cache-purge", "namespaces": namespace})


@group.command()
def migrate(ctx: typer.Context, target_version: str) -> None:
    """Migrate to an explicit schema version."""
    _run(ctx, {"operation": "migrate", "target_version": target_version})
