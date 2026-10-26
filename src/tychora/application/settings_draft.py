"""AST-preserving workspace leaf edits with shared activation preflight."""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from uuid import uuid4

import tomlkit
from pydantic import TypeAdapter, ValidationError

from tychora.configuration.models import (
    ConfigurationValues,
    EmbeddingSettings,
    ExtractionSettings,
    GraphSettings,
    RerankerSettings,
    ResourceLimits,
    RetrievalSettings,
    VectorSettings,
    VisionSettings,
)
from tychora.configuration.resolve import resolve_config, selected_workspace_profile
from tychora.contracts.administration import (
    SettingsEffectiveChange,
    SettingsField,
    SettingsFieldView,
    SettingsPatch,
    SettingsPreview,
)
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import SettingsActivation
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

from .workspace_authority import authorize_workspace

# This is an explicit leaf allowlist, never an arbitrary nested configuration editor.
_FIELDS = {"offline": ConfigurationValues.model_fields["offline"]}
for _slot in ("text", "image"):
    _FIELDS[f"index.{_slot}.backend"] = VectorSettings.model_fields["backend"]
for _slot, _model in [
    ("text", EmbeddingSettings),
    ("image", EmbeddingSettings),
    ("rerank", RerankerSettings),
    ("vision", VisionSettings),
]:
    for _name in ("provider", "model", "credential_ref"):
        _FIELDS[f"providers.{_slot}.{_name}"] = _model.model_fields[_name]
for _slot in ("text", "image"):
    for _name in (
        "dimensions",
        "max_group_tokens",
        "max_request_tokens",
        "max_assets",
        "max_multimodal_tokens",
    ):
        _FIELDS[f"providers.{_slot}.{_name}"] = EmbeddingSettings.model_fields[_name]
for _prefix, _model, _names in [
    ("extraction", ExtractionSettings, ("parser",)),
    (
        "retrieval",
        RetrievalSettings,
        ("table_continuations", "scoped_rrf_bonus", "allow_degraded"),
    ),
    (
        "retrieval.graph",
        GraphSettings,
        ("structural", "semantic", "predicates", "directions", "statuses"),
    ),
    (
        "limits",
        ResourceLimits,
        (
            "limit",
            "graph_seed_limit",
            "graph_candidate_limit",
            "graph_work_limit",
            "semantic_hops",
            "inspection_hops",
            "inspection_edges",
        ),
    ),
]:
    for _name in _names:
        _FIELDS[f"{_prefix}.{_name}"] = _model.model_fields[_name]

QUALIFIED_MODELS = {
    "text": ("voyage-context-4", "voyage-code-4", "voyage-code-3"),
    "image": ("voyage-multimodal-3.5",),
    "rerank": ("rerank-2.5",),
    # Vision accepts an explicit provider model identifier; no pinned model claims.
    "vision": (),
}


def _get(mapping, path):
    current = mapping
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _choices(path):
    if path.endswith(".model") and path.startswith("providers."):
        return QUALIFIED_MODELS[path.split(".")[1]]
    if path.endswith(".dimensions"):
        return (1024,)
    schema = TypeAdapter(_FIELDS[path].rebuild_annotation()).json_schema()
    return tuple(schema.get("enum", ()))


class SettingsDraftService:
    def __init__(self, settings):
        self.settings = settings
        self.owner = settings.owner

    def _read(self):
        try:
            fd = os.open(
                self.owner.path / "workspace.toml",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise OSError
            return raw
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Workspace settings are unavailable or exceed the limit.",
            ) from None

    def _resolve(self, raw):
        # A sibling candidate preserves relative registered root bindings. It is
        # removed before returning and never activates file/runtime state.
        path = self.owner.path / (".workspace-preview-" + str(uuid4()))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            profile = selected_workspace_profile(path)
            return resolve_config(path, profile, {})
        finally:
            path.unlink(missing_ok=True)

    def _fields(self, scope):
        authorize_workspace(self.owner, scope)
        raw = self._read()
        config = self._resolve(raw)
        overrides = tomlkit.parse(raw.decode()).unwrap().get("configuration", {})
        values = config.values.model_dump(mode="json")
        origins = config.model_dump(mode="json")["origins"]
        for origin in origins.values():
            if origin.get("layer") == "workspace":
                origin["source"] = "workspace.toml"
        fields = []
        for path, field in _FIELDS.items():
            value, overridden = _get(overrides, path)
            fields.append(
                SettingsField(
                    path=path,
                    effective_value=_get(values, path)[0],
                    override_value=value,
                    overridden=overridden,
                    origin=FrozenJsonMap(origins.get(path, {})),
                    value_schema=FrozenJsonMap(
                        TypeAdapter(field.rebuild_annotation()).json_schema()
                    ),
                    choices=_choices(path),
                )
            )
        blockers = ()
        try:
            self.settings.preflight(config)
        except TychoraError as error:
            blockers = (error.code,)
        return SettingsFieldView(
            active_profile=config.profile,
            base_workspace_sha256=sha256(raw).hexdigest(),
            fields=tuple(fields),
            blockers=blockers,
        )

    async def fields(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> SettingsFieldView:
        return await self.owner.run_write(lambda: self._fields(scope), ctx)

    def _draft(self, patch, scope):
        authorize_workspace(self.owner, scope)
        original = self._read()
        if sha256(original).hexdigest() != patch.base_workspace_sha256:
            raise TychoraError(
                "IDEMPOTENCY_CONFLICT",
                "Workspace settings changed; refresh before applying.",
            )
        document = tomlkit.parse(original.decode())
        for change in patch.changes:
            if change.path not in _FIELDS:
                raise TychoraError(
                    "INVALID_REQUEST", "Setting is outside the guided allowlist."
                )
            if change.action == "set":
                try:
                    TypeAdapter(
                        _FIELDS[change.path].rebuild_annotation()
                    ).validate_json(__import__("json").dumps(change.value), strict=True)
                except ValidationError:
                    raise TychoraError(
                        "INVALID_REQUEST", "Invalid guided setting value."
                    ) from None
                choices = _choices(change.path)
                if choices and change.value not in choices:
                    raise TychoraError(
                        "INVALID_REQUEST", "Value is not a qualified setting choice."
                    )
            table = document
            parts = ["configuration", *change.path.split(".")]
            for part in parts[:-1]:
                if part not in table:
                    table[part] = tomlkit.table()
                table = table[part]
            if change.action == "inherit":
                table.pop(parts[-1], None)
            else:
                table[parts[-1]] = change.value
        revised = tomlkit.dumps(document).encode()
        if len(revised) > 1048576:
            raise TychoraError(
                "INVALID_REQUEST", "Candidate settings exceed the limit."
            )
        candidate = self._resolve(revised)
        before = self._resolve(original).values.model_dump(mode="json")
        after = candidate.values.model_dump(mode="json")
        changes = []
        for change in patch.changes:
            old, new = _get(before, change.path)[0], _get(after, change.path)[0]
            if change.path.endswith(".credential_ref"):
                old = "[redacted]" if old is not None else None
                new = "[redacted]" if new is not None else None
            changes.append(
                SettingsEffectiveChange(path=change.path, before=old, after=new)
            )
        blockers = ()
        try:
            self.settings.preflight(candidate)
        except TychoraError as error:
            if error.code != "PROFILE_INCOMPATIBLE":
                raise
            blockers = (error.code,)
        preview = SettingsPreview(
            base_workspace_sha256=patch.base_workspace_sha256,
            candidate_workspace_sha256=sha256(revised).hexdigest(),
            changes=tuple(changes),
            activation_allowed=not blockers,
            blockers=blockers,
        )
        return revised, candidate, preview

    async def preview(
        self, patch: SettingsPatch, scope: RequestScope, ctx: ExecutionContext
    ) -> SettingsPreview:
        return (await self.owner.run_write(lambda: self._draft(patch, scope), ctx))[2]

    async def apply(
        self, patch: SettingsPatch, scope: RequestScope, ctx: ExecutionContext
    ) -> SettingsFieldView:
        self.settings.runtime.admit(activation=True)
        try:
            return await self._apply_admitted(patch, scope, ctx)
        finally:
            self.settings.runtime.release(activation=True)

    async def _apply_admitted(self, patch, scope, ctx):
        raw, candidate, preview = await self.owner.run_write(
            lambda: self._draft(patch, scope), ctx
        )
        if not preview.activation_allowed:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Existing index generations require their original configuration.",
            )
        await self.settings._activate_admitted(
            SettingsActivation(profile=candidate.profile, workspace_toml=raw.decode()),
            scope,
        )
        return await self.fields(scope, ctx)
