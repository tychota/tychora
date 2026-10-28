"""Verified canonical directory restore with an explicit deletion suspension."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from tychora.adapters.files.archive import ArchiveReader, ArchiveWriter
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import from_backup
from tychora.adapters.sqlite.migrations import open_verified_import
from tychora.adapters.sqlite.restore_rows import (
    RestoreDatabase,
    active_snapshot_config_ref,
    artifact_registration,
    authorize_restore,
    bootstrap_scope,
    has_document_locator,
    restored_workspace_ids,
    retained_config_shas,
    retained_extractions,
    set_operational_state,
    source_unit_attributes,
    unit_locator_details,
    unit_values,
)
from tychora.application.backup import BackupManifest
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import (
    exportable_resolved_config,
    restored_runtime_config,
    validate_retained_config,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import RequestScope
from tychora.ingestion.composition import replay_composed_document
from tychora.ingestion.document_parts import validate_document_ownership
from tychora.ingestion.documents import replay_document
from tychora.ports.execution import ExecutionContext

RESTORE_SUSPENSION = ".restore-suspended.json"
RESTORE_RECEIPT = ".restore-receipt.json"
RESTORE_ACTIVATION = ".restore-activation.json"
_DEFAULT_CONFIG = (
    'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
    '[defaults]\nprofile = "local"\n'
)


@dataclass(frozen=True)
class RestoreReceipt:
    workspace_id: str
    destination: Path
    backup_manifest_sha256: str
    restored_sqlite_sha256: str
    deletion_suspended: bool = True
    opening_scope: RequestScope | None = field(default=None, repr=False)


def _unique_pairs(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise TychoraError("INTEGRITY_ERROR", "Duplicate archive JSON key")
        value[key] = member
    return value


def _read_json(path: Path, maximum: int) -> tuple[dict, bytes]:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise TychoraError("INTEGRITY_ERROR", "Archive JSON exceeds limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict) or canonical_bytes(value) != raw:
            raise ValueError("noncanonical JSON")
    except (UnicodeError, ValueError, RecursionError):
        raise TychoraError("INTEGRITY_ERROR", "Archive JSON is malformed") from None
    return value, raw


def _file_sha(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def deletion_suspended(path: Path, workspace_id: str) -> bool:
    marker = path / RESTORE_SUSPENSION
    if not marker.exists() and not marker.is_symlink():
        return False
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 4096
            ):
                raise ValueError
            raw = stream.read(4097)
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (
            value
            != {
                "type": "restore-suspension",
                "schema_version": 1,
                "workspace_id": workspace_id,
                "pending_deletion_suspended": True,
            }
            or canonical_bytes(value) != raw
        ):
            raise ValueError
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Restore deletion suspension is malformed"
        ) from None
    return True


def _active_snapshot_config(
    db: RestoreDatabase, artifacts: LocalArtifacts
) -> ResolvedConfig | None:
    ref = active_snapshot_config_ref(db)
    if ref is None:
        return None
    with artifacts.open_verified(ref) as stream:
        raw = stream.read(min(ref.size_bytes, 16 * 1024 * 1024) + 1)
    if len(raw) != ref.size_bytes:
        raise TychoraError("INTEGRITY_ERROR", "Active configuration exceeds limit")
    try:
        root = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (
            canonical_bytes(root) != raw
            or root.get("type") != "registry"
            or root.get("schema_version") != "1"
            or not isinstance(root.get("data"), dict)
        ):
            raise ValueError
        return validate_retained_config(
            ResolvedConfig.model_validate_json(canonical_bytes(root["data"]))
        )
    except (ValueError, UnicodeError, RecursionError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Active configuration is malformed"
        ) from None


def _selected_backup_config(directory: Path, manifest: dict) -> ResolvedConfig | None:
    value = manifest.get("data", {}).get("resolved_config_sha256")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Selected configuration binding is malformed"
        )
    document, raw = _read_json(directory / "resolved-config.json", 16 * 1024 * 1024)
    if (
        sha256(raw).hexdigest() != value
        or set(document) != {"type", "schema_version", "data"}
        or document["type"] != "resolved-config"
        or document["schema_version"] != 1
    ):
        raise TychoraError("INTEGRITY_ERROR", "Selected configuration binding differs")
    try:
        return validate_retained_config(
            ResolvedConfig.model_validate_json(canonical_bytes(document["data"]))
        )
    except (ValueError, TypeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Selected configuration is malformed"
        ) from None


def _validate_retained_documents(
    db: RestoreDatabase,
    artifacts: LocalArtifacts,
    selected_config: ResolvedConfig | None,
) -> None:
    """Recheck document source ownership against retained parser and SDK facts."""

    def payload(sha: str, limit: int = 256 * 1024 * 1024) -> tuple[ArtifactRef, bytes]:
        ref = artifact_registration(db, sha)
        if ref is None or ref.size_bytes > limit:
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained document dependency is unavailable"
            )
        with artifacts.open_verified(ref) as stream:
            raw = stream.read(ref.size_bytes + 1)
        if len(raw) != ref.size_bytes:
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained document dependency differs"
            )
        return ref, raw

    candidate_configs: list[ResolvedConfig] = (
        [selected_config] if selected_config is not None else []
    )
    for config_sha in sorted(retained_config_shas(db)):
        _, config_raw = payload(config_sha, 16 * 1024 * 1024)
        document = json.loads(config_raw, object_pairs_hook=_unique_pairs)
        if document.get("type") != "registry" or document.get("schema_version") != "1":
            raise TychoraError("INTEGRITY_ERROR", "Retained configuration type differs")
        candidate_configs.append(
            validate_retained_config(
                ResolvedConfig.model_validate_json(canonical_bytes(document["data"]))
            )
        )

    for (
        extraction_id,
        revision_id,
        manifest_sha,
        diagnostics_sha,
        registration_json,
    ) in retained_extractions(db):
        ref, raw = payload(manifest_sha)
        try:
            root = json.loads(raw, object_pairs_hook=_unique_pairs)
            data = root["data"]
            if root["type"] != "extraction" or root["schema_version"] != "1":
                raise ValueError
            composed_root = (
                root if data.get("representation") == "document-composition@1" else None
            )
            composed_ref, composed_raw = ref, raw
            if composed_root is not None:
                links = [
                    item
                    for item in root.get("required_manifests", [])
                    if item.get("type") == "extraction"
                    and item.get("schema_version") == "1"
                ]
                if len(links) != 1:
                    raise ValueError
                raw_sha = links[0]["artifact"]["sha256"]
                ref, raw = payload(raw_sha)
                root = json.loads(raw, object_pairs_hook=_unique_pairs)
                data = root["data"]
            source_registration = json.loads(
                registration_json, object_pairs_hook=_unique_pairs
            )
            registered_format = source_registration.get("format")
            parsed_format = data.get("registration", {}).get("format")
            document_locator = has_document_locator(db, extraction_id)
            rich_format = {"pdf", "docx", "png", "jpeg"}
            source_markers = []
            for attributes_json in source_unit_attributes(db, extraction_id):
                attributes = json.loads(
                    attributes_json, object_pairs_hook=_unique_pairs
                )
                if not isinstance(attributes, dict):
                    raise TypeError("Canonical unit attributes are malformed")
                source_markers.append(attributes.get("document_representation"))
            document_markers = {
                "docling-source-parts@1",
                "docling-image-source@1",
            }
            requires_document_mapping = (
                registered_format in rich_format
                or parsed_format in rich_format
                or document_locator
                or any(
                    isinstance(marker, str) and marker.startswith("docling-")
                    for marker in source_markers
                )
            )
            if (
                data.get("image_representation") == "scan-external-images@1"
                and composed_root is not None
            ):
                document_markers.add("docling-pdf-pixels@1")
            if requires_document_mapping and any(
                marker not in document_markers for marker in source_markers
            ):
                raise ValueError("Canonical document source marker is absent")
            if registered_format in rich_format and parsed_format != registered_format:
                raise ValueError("Registered document format differs")
            doc_ref_data = data.get("docling_json_ref")
            if doc_ref_data is None:
                if requires_document_mapping:
                    raise ValueError("Retained document source mapping is absent")
                continue
            if not requires_document_mapping:
                raise ValueError("Unregistered document source mapping")
            doc_ref = ArtifactRef.model_validate_json(canonical_bytes(doc_ref_data))
            _, sdk_json = payload(doc_ref.sha256)

            def read_declared(declared_ref):
                registered, content = payload(declared_ref.sha256)
                if registered != declared_ref:
                    raise ValueError("Declared PDF artifact differs")
                return content

            parsed = replay_document(raw, sdk_json, ref, read_declared=read_declared)
            if parsed.registration.revision.revision_id != revision_id:
                raise ValueError
            units = []
            source_units = []
            for values in unit_values(db, extraction_id):
                uid = values["unit_id"]
                values["heading_path"] = json.loads(values.pop("heading_path_json"))
                values["attributes"] = json.loads(values.pop("attributes_json"))
                values["review_state"] = values.pop("declared_review_state")
                values["citable"] = bool(values["citable"])
                values["locators"] = [
                    json.loads(details, object_pairs_hook=_unique_pairs)
                    for details in unit_locator_details(db, uid)
                ]
                units.append(EvidenceUnit.from_mapping(values))
                if values["text_origin"] == "source":
                    source_units.append(units[-1])
            native_source_units = tuple(
                unit
                for unit in source_units
                if unit.attributes.get("document_representation")
                != "docling-pdf-pixels@1"
            )
            if len(native_source_units) != len(source_units) and (
                parsed.image_representation != "scan-external-images@1"
                or composed_root is None
            ):
                raise ValueError("Undeclared PDF pixel owner")
            validate_document_ownership(native_source_units, parsed)
            if composed_root is not None:
                from tychora.ingestion.composition import DeclaredPayloads

                declared_refs = {}
                for declared in composed_root.get("required_artifacts", []):
                    declared_ref = ArtifactRef.model_validate_json(
                        canonical_bytes(declared)
                    )
                    old = declared_refs.get(declared_ref.sha256)
                    if old is not None and old != declared_ref:
                        raise ValueError("Conflicting composed inputs")
                    declared_refs[declared_ref.sha256] = declared_ref
                inputs = DeclaredPayloads(
                    declared_refs, read_declared, 512 * 1024 * 1024
                )
                if not candidate_configs:
                    raise ValueError("Composed replay configuration is unavailable")
                replayed = None
                for candidate in candidate_configs:
                    try:
                        replayed = replay_composed_document(
                            composed_raw,
                            composed_ref,
                            parsed,
                            Registry.from_config(candidate),
                            inputs,
                        )
                        break
                    except ValueError:
                        continue
                if (
                    replayed is None
                    or replayed.bundle.extraction.extraction_id != extraction_id
                ):
                    raise ValueError("Composed extraction replay differs")
                if {unit.unit_id: unit for unit in replayed.bundle.units} != {
                    unit.unit_id: unit for unit in units
                }:
                    raise ValueError("Composed canonical evidence differs")
            if diagnostics_sha is not None:
                composed = json.loads(
                    payload(manifest_sha)[1], object_pairs_hook=_unique_pairs
                )
                if diagnostics_sha not in {
                    item["sha256"] for item in composed.get("required_artifacts", [])
                }:
                    raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained document source mapping differs"
            ) from None


def load_restored_runtime_config(path: Path) -> ResolvedConfig:
    """Reopen from a durable verified activation, independent of deletion state."""
    db = open_verified_import(path / "corpus.sqlite3")
    try:
        workspace = restored_workspace_ids(db)
        if len(workspace) != 1:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE", "Restore activation marker is unavailable"
            )
        manifest, manifest_raw = _read_json(
            path / "backup-manifest.json", 16 * 1024 * 1024
        )
        restore_receipt, _ = _read_json(path / RESTORE_RECEIPT, 4096)
        manifest_sha = sha256(manifest_raw).hexdigest()
        if restore_receipt.get("backup_manifest_sha256") != manifest_sha:
            raise TychoraError(
                "INTEGRITY_ERROR", "Restore configuration receipt differs"
            )
        activation, activation_raw = _read_json(
            path / RESTORE_ACTIVATION, 16 * 1024 * 1024
        )
        if (
            sha256(activation_raw).hexdigest()
            != restore_receipt.get("activation_sha256")
            or set(activation)
            != {
                "type",
                "schema_version",
                "workspace_id",
                "backup_manifest_sha256",
                "config",
            }
            or activation["type"] != "restore-activation"
            or activation["schema_version"] != 1
            or activation["workspace_id"] != workspace[0]
            or activation["backup_manifest_sha256"] != manifest_sha
        ):
            raise TychoraError("INTEGRITY_ERROR", "Restore activation binding differs")
        try:
            activated = validate_retained_config(
                ResolvedConfig.model_validate_json(
                    canonical_bytes(activation["config"])
                )
            )
            if exportable_resolved_config(activated) != activation["config"]:
                raise ValueError
        except (ValueError, TypeError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Restore activation configuration differs"
            ) from None
        selected = _selected_backup_config(path, manifest)
        historical = _active_snapshot_config(db, LocalArtifacts(path / "artifacts"))
        bound = selected or historical
        if bound is not None and not pinned_runtime_compatible(bound, activated):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE", "Restore activation changed pinned semantics"
            )
        return restored_runtime_config(activated)
    finally:
        db.close()


def _entries(copied: frozenset[str]) -> int:
    """Count the artifact files physically copied from an archive."""
    return sum(name.startswith("artifacts/") for name in copied)


class RestoreService:
    def __init__(
        self,
        *,
        config: ResolvedConfig | None = None,
        max_entries: int = 100_000,
        max_expanded_bytes: int = 20 * 1024**3,
    ) -> None:
        self.config = validate_retained_config(config) if config is not None else None
        self.max_entries = max_entries
        self.max_expanded_bytes = max_expanded_bytes

    def import_backup(
        self,
        source: Path,
        destination: Path,
        scope: RequestScope | None,
        ctx: ExecutionContext,
    ) -> RestoreReceipt:
        source, destination = source.absolute(), destination.absolute()
        try:
            resolved_source = source.resolve(strict=True)
        except FileNotFoundError:
            raise TychoraError("NOT_FOUND", "Restore archive is unavailable.") from None
        if destination.resolve(strict=False).is_relative_to(resolved_source):
            raise TychoraError(
                "INVALID_REQUEST", "Restore destination is inside archive"
            )
        with ArchiveWriter(destination) as stage:
            copied = ArchiveReader(
                source,
                max_entries=self.max_entries,
                max_expanded_bytes=self.max_expanded_bytes,
            ).copy_to(stage.stage, ctx)
            receipt, _ = _read_json(stage.stage / "backup-receipt.json", 1024 * 1024)
            manifest, manifest_raw = _read_json(
                stage.stage / "backup-manifest.json", 16 * 1024 * 1024
            )
            manifest_sha = sha256(manifest_raw).hexdigest()
            if receipt != {
                "type": "backup-receipt",
                "schema_version": 1,
                "workspace_id": manifest.get("data", {}).get("workspace_id"),
                "sqlite_sha256": _file_sha(stage.stage / "corpus.sqlite3"),
                "manifest_sha256": manifest_sha,
                "complete": True,
            }:
                raise TychoraError("INTEGRITY_ERROR", "Backup receipt differs")
            db = open_verified_import(stage.stage / "corpus.sqlite3")
            try:
                copied_artifacts = LocalArtifacts(stage.stage / "artifacts")
                inventory = from_backup(
                    db, artifacts=copied_artifacts, archive_entries=_entries(copied)
                )
                for artifact in inventory.artifacts:
                    ctx.checkpoint()
                    with copied_artifacts.open_verified(artifact):
                        pass
                selected = _selected_backup_config(stage.stage, manifest)
                _validate_retained_documents(
                    db, LocalArtifacts(stage.stage / "artifacts"), selected
                )
                opening_scope = scope if scope is not None else bootstrap_scope(db)
                authorize_restore(db, opening_scope)
                retained = _active_snapshot_config(
                    db, LocalArtifacts(stage.stage / "artifacts")
                )
                bound = selected or retained
                if self.config is None and bound is None:
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Restore requires an explicit destination configuration",
                    )
                if (
                    self.config is not None
                    and bound is not None
                    and not pinned_runtime_compatible(bound, self.config)
                ):
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Destination configuration differs from pinned snapshot",
                    )
                activated = self.config or bound
                assert activated is not None
                activation_data = exportable_resolved_config(activated)
                expected = BackupManifest(
                    workspace_id=inventory.workspace_id,
                    artifacts=inventory.artifacts,
                    payload_states=inventory.payload_states,
                    policy_epoch=inventory.policy_epoch,
                    lexical_statistics_epoch=inventory.lexical_statistics_epoch,
                    active_snapshot_id=inventory.active_snapshot_id,
                    snapshot_ids=inventory.snapshot_ids,
                    tombstones=inventory.tombstones,
                    config_shas=inventory.config_shas,
                    resolved_config_sha256=manifest.get("data", {}).get(
                        "resolved_config_sha256"
                    ),
                ).as_header()
                if (
                    manifest != expected
                    or receipt["workspace_id"] != inventory.workspace_id
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR",
                        "Backup manifest differs from canonical inventory",
                    )
                expected_files = {
                    "corpus.sqlite3",
                    "backup-manifest.json",
                    "backup-receipt.json",
                    *(f"artifacts/{ref.sha256}" for ref in inventory.artifacts),
                }
                if selected is not None:
                    expected_files.add("resolved-config.json")
                if copied != expected_files:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Archive payload closure differs"
                    )
            finally:
                db.close()
            ctx.checkpoint()
            set_operational_state(stage.stage / "corpus.sqlite3")
            restored_sha = _file_sha(stage.stage / "corpus.sqlite3")
            activation_sha = stage.write_json(
                RESTORE_ACTIVATION,
                {
                    "type": "restore-activation",
                    "schema_version": 1,
                    "workspace_id": inventory.workspace_id,
                    "backup_manifest_sha256": manifest_sha,
                    "config": activation_data,
                },
            )
            stage.write_json(
                RESTORE_SUSPENSION,
                {
                    "type": "restore-suspension",
                    "schema_version": 1,
                    "workspace_id": inventory.workspace_id,
                    "pending_deletion_suspended": True,
                },
            )
            stage.write_json(
                RESTORE_RECEIPT,
                {
                    "type": "restore-receipt",
                    "schema_version": 1,
                    "workspace_id": inventory.workspace_id,
                    "backup_manifest_sha256": manifest_sha,
                    "restored_sqlite_sha256": restored_sha,
                    "activation_sha256": activation_sha,
                    "deletion_suspended": True,
                    "complete": True,
                },
            )
            ctx.checkpoint()
            stage.finish()
            return RestoreReceipt(
                inventory.workspace_id,
                destination,
                manifest_sha,
                restored_sha,
                opening_scope=opening_scope,
            )
