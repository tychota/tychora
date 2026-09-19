"""Typed code identity, retained parsing and static resolution contracts."""

from hashlib import sha256
from typing import Literal, Self

from pydantic import field_validator, model_validator

from tychora.ports.language import SyntaxResult

from .acquisition import CapturedFile, CaptureManifest
from .artifacts import ArtifactRef, ManifestRef
from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt, Sha256
from .code_storage import CodeFileDescriptor
from .corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from .identity import canonical_bytes, digest, normalize_source_key

CODE_RESOLVER_FINGERPRINT = digest(
    "code-resolver-v1", "qualified-import-scope-cap20"
)


class CodeRegistration(Contract):
    capture: CaptureManifest
    workspace: WorkspaceRow
    collection: CollectionRow
    source: SourceRow
    revision: RevisionRow

    @model_validator(mode="after")
    def matching_repository(self) -> Self:
        metadata = self.revision.metadata_json
        path_present = "relative_path" in metadata
        language_present = "language" in metadata
        if (
            self.source.source_type != "repository"
            or not self.capture.entries
            or self.capture.source.get("type")
            not in ("folder", "repository", "local_repository")
            or self.collection.workspace_id != self.workspace.workspace_id
            or self.source.collection_id != self.collection.collection_id
            or self.revision.source_id != self.source.source_id
            or self.capture.source_key != self.source.source_key
            or self.capture.source.get("source_key") != self.source.source_key
            or self.capture.source.get("collection_id") != self.collection.collection_id
            or self.capture.source.get("kind") != self.revision.kind_id
            or self.capture.source.get("metadata") != metadata
            or self.capture.original_sha != self.revision.original_sha
            or self.revision.metadata_sha != digest("metadata-v1", metadata)
            or path_present != language_present
        ):
            raise ValueError("Code registration differs from repository capture")
        return self


class CapturedSyntax(Contract):
    captured: CapturedFile
    syntax: SyntaxResult

    @model_validator(mode="after")
    def exact_captured_bytes(self) -> Self:
        if (
            sha256(self.syntax.file_bytes).hexdigest() != self.captured.blob_sha
            or len(self.syntax.file_bytes) != self.captured.artifact_ref.size_bytes
        ):
            raise ValueError("Syntax bytes differ from explicitly captured file")
        return self

    def descriptor(self) -> CodeFileDescriptor:
        syntax = self.syntax
        return CodeFileDescriptor(
            relative_path=self.captured.relative_path,
            artifact_ref=self.captured.artifact_ref.sha256,
            language=syntax.language,
            encoding=syntax.encoding,
            coverage=syntax.coverage,
            adapter_fingerprint=syntax.adapter_fingerprint,
            byte_length=len(syntax.file_bytes),
            char_length=len(syntax.text),
            codepoint_to_byte=syntax.codepoint_to_byte,
            byte_to_codepoint=syntax.byte_to_codepoint,
            line_starts=syntax.line_starts,
            byte_to_line=syntax.byte_to_line,
        )


def retained_syntax_data(item: CapturedSyntax) -> dict:
    return {
        "captured": item.captured.model_dump(mode="json"),
        "syntax": item.syntax.model_dump(
            mode="json",
            exclude={
                "file_bytes",
                "text",
                "codepoint_to_byte",
                "byte_to_codepoint",
                "line_starts",
                "byte_to_line",
            },
        ),
    }


def code_extraction_document(value: "CodeExtractionArtifact") -> bytes:
    files = (
        []
        if value.partition_refs
        else [retained_syntax_data(item) for item in value.files]
    )
    required = [
        ref.model_dump(mode="json")
        for ref in (value.assertions_ref, value.diagnostics_ref)
        if ref is not None
    ]
    return canonical_bytes(
        {
            "type": "extraction",
            "schema_version": "1",
            "required_artifacts": required,
            "required_manifests": [
                ref.model_dump(mode="json") for ref in value.partition_refs
            ],
            "data": {
                "registration_fingerprint": digest(
                    "code-registration-v1",
                    value.registration.model_dump(mode="json"),
                ),
                "capture_manifest_sha": value.registration.capture.manifest_ref.sha256,
                "files": files,
                "files_partitioned": bool(value.partition_refs),
                "parser_fingerprint": value.parser_fingerprint,
                "normalizer_fingerprint": value.normalizer_fingerprint,
                "resolver_fingerprint": value.resolver_fingerprint,
                "registry_digest": value.registry_digest,
                "assertions_ref": value.assertions_ref.model_dump(mode="json")
                if value.assertions_ref
                else None,
                "diagnostics_ref": value.diagnostics_ref.model_dump(mode="json")
                if value.diagnostics_ref
                else None,
            },
        }
    )


class CodeExtractionArtifact(Contract):
    registration: CodeRegistration
    files: tuple[CapturedSyntax, ...]
    raw_document_ref: ArtifactRef
    parser_fingerprint: Sha256
    normalizer_fingerprint: Sha256
    resolver_fingerprint: Sha256
    registry_digest: Sha256
    assertions_ref: ArtifactRef | None = None
    diagnostics_ref: ArtifactRef | None = None
    partition_refs: tuple[ManifestRef, ...] = ()

    @model_validator(mode="after")
    def retained_identity(self) -> Self:
        paths = [item.captured.relative_path for item in self.files]
        expected = [item.relative_path for item in self.registration.capture.entries]
        encoded = code_extraction_document(self)
        if (
            paths != expected
            or len(set(paths)) != len(paths)
            or len(self.files) > 4000
            and not self.partition_refs
            or len(self.partition_refs) > 10_000
            or len(encoded) > 16 * 1024 * 1024
            or sha256(encoded).hexdigest() != self.raw_document_ref.sha256
            or len(encoded) != self.raw_document_ref.size_bytes
        ):
            raise ValueError("Retained code extraction differs from captured files")
        return self


class CatalogDefinition(Contract):
    target_id: Identifier
    revision_id: Identifier
    relative_path: NonEmptyStr
    language: NonEmptyStr
    name: NonEmptyStr
    qualified_name: NonEmptyStr
    symbol_kind: NonEmptyStr
    native_ref: NonEmptyStr
    parent_native_ref: str | None = None

    @field_validator("relative_path")
    @classmethod
    def normalized_path(cls, value: str) -> str:
        return normalize_source_key(value)


class ImportBinding(Contract):
    relative_path: NonEmptyStr
    source_scope_ref: str | None
    local_name: NonEmptyStr
    target_revision_id: Identifier
    target_ids: tuple[Identifier, ...]

    @field_validator("relative_path")
    @classmethod
    def normalized_path(cls, value: str) -> str:
        return normalize_source_key(value)

    @field_validator("target_ids")
    @classmethod
    def unique_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("Import binding targets must be nonempty and unique")
        return value


class RevisionDependency(Contract):
    source_revision_id: Identifier
    target_revision_id: Identifier

    @model_validator(mode="after")
    def crosses_revision(self) -> Self:
        if self.source_revision_id == self.target_revision_id:
            raise ValueError("Revision dependency must cross revisions")
        return self


class SymbolCatalog(Contract):
    """One immutable revision index viewed from one explicit captured file."""

    source_id: Identifier
    revision_id: Identifier
    source_relative_path: NonEmptyStr
    source_language: NonEmptyStr
    source_adapter_fingerprint: Sha256
    definitions: tuple[CatalogDefinition, ...] = ()
    imports: tuple[ImportBinding, ...] = ()
    revision_dependencies: tuple[RevisionDependency, ...] = ()

    @field_validator("source_relative_path")
    @classmethod
    def normalized_path(cls, value: str) -> str:
        return normalize_source_key(value)

    @model_validator(mode="after")
    def consistent_revision(self) -> Self:
        if any(item.revision_id != self.revision_id for item in self.definitions):
            raise ValueError("Catalog definitions must belong to its revision")
        if any(
            item.source_revision_id != self.revision_id
            for item in self.revision_dependencies
        ):
            raise ValueError("Revision dependency source differs from catalog")
        if len({item.target_id for item in self.definitions}) != len(self.definitions):
            raise ValueError("Catalog definition target IDs must be unique")
        return self


class Resolution(Contract):
    reference_id: Identifier
    source_id: Identifier
    revision_id: Identifier
    relative_path: NonEmptyStr
    language: NonEmptyStr
    reference_form: NonEmptyStr
    spelling: NonEmptyStr
    owning_scope: tuple[str, ...]
    source_scope_ref: str | None
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: int
    line_end: int
    adapter_fingerprint: Sha256
    supported: bool
    state: Literal["resolved", "ambiguous", "unresolved"]
    selected_target: Identifier | None
    candidates: tuple[Identifier, ...]
    rule: NonEmptyStr
    reasons: tuple[NonEmptyStr, ...]
    truncated: bool
    total_candidate_count: NonNegativeInt

    @field_validator("relative_path")
    @classmethod
    def normalized_source_path(cls, value: str) -> str:
        return normalize_source_key(value)

    @model_validator(mode="after")
    def valid_state(self) -> Self:
        if (
            self.candidates != tuple(sorted(set(self.candidates)))
            or len(self.candidates) > 20
            or self.total_candidate_count < len(self.candidates)
            or self.byte_end < self.byte_start
            or self.char_end < self.char_start
            or self.line_start < 1
            or self.line_end < self.line_start
            or self.truncated != (self.total_candidate_count > len(self.candidates))
            or (self.state == "resolved")
            != (len(self.candidates) == 1 and self.selected_target is not None)
            or self.state == "ambiguous"
            and (len(self.candidates) < 2 or self.selected_target is not None)
            or self.state == "unresolved"
            and (self.candidates or self.selected_target is not None)
        ):
            raise ValueError("Resolution state and candidate cardinality differ")
        return self


class ResolutionRate(Contract):
    language: NonEmptyStr
    reference_form: NonEmptyStr
    extracted: NonNegativeInt
    resolved: NonNegativeInt
    ambiguous: NonNegativeInt
    unresolved: NonNegativeInt
    resolved_rate: float | None
    ambiguous_rate: float | None
    unresolved_rate: float | None

    @model_validator(mode="after")
    def exact_denominator(self) -> Self:
        rates = (self.resolved_rate, self.ambiguous_rate, self.unresolved_rate)
        if (
            self.resolved + self.ambiguous + self.unresolved != self.extracted
            or (self.extracted == 0) != all(value is None for value in rates)
            or self.extracted > 0
            and (
                any(value is None or not 0 <= value <= 1 for value in rates)
                or abs(sum(value for value in rates if value is not None) - 1) > 1e-12
            )
        ):
            raise ValueError("Resolution rates differ from extracted denominator")
        return self


class OwnedRange(Contract):
    owner_unit_id: Identifier
    artifact_ref: Identifier
    relative_path: NonEmptyStr
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt

    @field_validator("relative_path")
    @classmethod
    def normalized_path(cls, value: str) -> str:
        return normalize_source_key(value)

    @model_validator(mode="after")
    def nonempty_ranges(self) -> Self:
        if self.char_end <= self.char_start or self.byte_end <= self.byte_start:
            raise ValueError("Owned range must contain original source bytes")
        return self
