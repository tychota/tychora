"""Strict guided workspace administration contracts."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from tychora.contracts.base import Contract, FrozenJsonMap, NonEmptyStr, Sha256


class SettingsFieldChange(Contract):
    path: NonEmptyStr
    action: Literal["set", "inherit"]
    value: JsonValue | None = None

    @model_validator(mode="after")
    def explicit_value(self) -> Self:
        if self.action == "inherit" and "value" in self.model_fields_set:
            raise ValueError("inherit forbids a value")
        if self.action == "set" and self.value is None:
            raise ValueError("set requires a non-null value")
        return self


class SettingsPatch(Contract):
    base_workspace_sha256: Sha256
    changes: Annotated[
        tuple[SettingsFieldChange, ...], Field(min_length=1, max_length=32)
    ]

    @model_validator(mode="after")
    def unique_paths(self) -> Self:
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ValueError("Duplicate settings paths")
        if len(self.model_dump_json().encode()) > 1048576:
            raise ValueError("Settings patch exceeds request limit")
        return self


class SettingsField(Contract):
    path: str
    effective_value: JsonValue
    override_value: JsonValue = None
    overridden: bool
    origin: FrozenJsonMap
    value_schema: FrozenJsonMap
    choices: tuple[JsonValue, ...] = ()


class SettingsFieldView(Contract):
    active_profile: str
    base_workspace_sha256: Sha256
    fields: tuple[SettingsField, ...]
    blockers: tuple[str, ...] = ()
    credential_readiness: Literal["not_checked"] = "not_checked"


class SettingsEffectiveChange(Contract):
    path: str
    before: JsonValue
    after: JsonValue


class SettingsPreview(Contract):
    base_workspace_sha256: Sha256
    candidate_workspace_sha256: Sha256
    changes: tuple[SettingsEffectiveChange, ...]
    activation_allowed: bool
    blockers: tuple[str, ...]
    rebuild_queued: Literal[False] = False


class ModelSelections(Contract):
    selections: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.selections)) != len(self.selections):
            raise ValueError("Duplicate selection")
        return self


class AssetReadiness(Contract):
    asset_id: str
    selection: str
    sha256: Sha256
    size_bytes: int
    state: Literal["installed", "missing", "corrupt", "not_checked"]
    checked_bytes: int = 0
    verification: Literal["current_full_hash", "none"] = "none"
    reason: str | None = None


class SelectionReadiness(Contract):
    selection: str
    state: Literal["installed", "missing", "corrupt", "not_checked"]
    complete: bool


class VerificationReceipt(Contract):
    job_id: str
    manifest_sha256: Sha256
    verified_at: str
    asset_ids: tuple[str, ...]
    verification: Literal["durable_setup_result"] = "durable_setup_result"


class ModelReadiness(Contract):
    manifest_sha256: Sha256
    assets: tuple[AssetReadiness, ...]
    selections: tuple[SelectionReadiness, ...]
    total_size_bytes: int
    checked_bytes: int
    complete: bool
    receipts: tuple[VerificationReceipt, ...] = ()
    receipt_search_complete: bool = True
    receipt_checked_bytes: int = 0


class ProviderInventory(Contract):
    slot: Literal["text", "image", "rerank", "vision"]
    configured_provider: str
    configured_model: str | None
    qualified_models: tuple[str, ...]
    model_qualification: Literal[
        "qualified", "unsupported", "explicit_identifier", "disabled"
    ]
    package_available: bool
    credential_readiness: Literal["not_checked"] = "not_checked"
    upload_allowed_collection_ids: tuple[str, ...]
    asset_state: Literal["not_checked"] = "not_checked"


class ModelSelectionCapability(Contract):
    selection: str
    required_modules: tuple[str, ...]
    packages_available: bool
    asset_state: Literal["not_checked"] = "not_checked"


class ModelCatalog(Contract):
    providers: tuple[ProviderInventory, ...]
    selections: tuple[str, ...]
    selection_capabilities: tuple[ModelSelectionCapability, ...]
    manifest_sha256: Sha256
    offline: bool
    pdf_ocr_policy: Literal["per_intake"] = "per_intake"
