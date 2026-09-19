"""Strict ingestion-only vision response validation."""

import json
from base64 import b64encode
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from tychora.application.owner import WorkspaceOwner
from tychora.configuration.schemas import LocalSchemas
from tychora.contracts.base import Contract, FrozenJsonMap, NonEmptyStr
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.vision import (
    CapturedRegionAuthorization,
    DerivedObservation,
    RegionInput,
    vision_cache_key,
    vision_prompt_identity,
)


class _Response(Contract):
    target_id: str | None
    literal_transcription: str | None
    interpretation: FrozenJsonMap | None
    uncertainties: Annotated[tuple[NonEmptyStr, ...], Field(max_length=100)]


_PROMPT_TEMPLATE = (
    "Perform only the selected ingestion task: {task}. "
    "Preserve literal transcription separately from interpretation. "
    "Candidate context: {candidate_context}"
)
_RESPONSE_SCHEMA_ENVELOPE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "target_id",
        "literal_transcription",
        "interpretation",
        "uncertainties",
    ],
    "properties": {
        "literal_transcription": {"type": ["string", "null"]},
        "uncertainties": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1},
        },
    },
}
_DEFAULT_RENDERER_SEMANTICS: JsonValue = {
    "request_format": "anthropic-messages-json-schema-v1",
    "prompt_template": _PROMPT_TEMPLATE,
    "response_schema_envelope": _RESPONSE_SCHEMA_ENVELOPE,
}


def _task_validator(task_schema: JsonValue) -> tuple[str, LocalSchemas]:
    if not isinstance(task_schema, dict) or "$id" in task_schema:
        raise TychoraError("INVALID_REQUEST", "Vision task schema is unsupported")
    schema_id = "vision-task-schema@" + digest("vision-task-schema-v1", task_schema)
    schema = {"$id": schema_id, **task_schema}
    return schema_id, LocalSchemas({schema_id: schema}, {schema_id: {}})


def response_schema(task_schema: JsonValue, allowed: frozenset[str]) -> dict[str, Any]:
    _task_validator(task_schema)
    targets: dict[str, Any] = {"type": "null"}
    if allowed:
        targets = {"anyOf": [{"type": "string", "enum": sorted(allowed)}, targets]}
    return {
        **_RESPONSE_SCHEMA_ENVELOPE,
        "properties": {
            **_RESPONSE_SCHEMA_ENVELOPE["properties"],
            "target_id": targets,
            "interpretation": {"anyOf": [task_schema, {"type": "null"}]},
        },
    }


class AnthropicVisionProvider:
    def __init__(
        self,
        client: Any,
        *,
        model: str,
        prompt_schema_version: str,
        renderer_semantics: JsonValue = _DEFAULT_RENDERER_SEMANTICS,
    ):
        if not model.strip() or not prompt_schema_version.strip():
            raise TychoraError(
                "CAPABILITY_MISSING", "Vision model and prompt schema are required"
            )
        self.client = client
        self.model = model
        self.prompt_schema_version = prompt_schema_version
        self.prompt_renderer_identity = digest(
            "vision-renderer-semantics-v1", renderer_semantics
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def interpret(
        self, region: RegionInput, task_schema: JsonValue, ctx: ExecutionContext
    ) -> DerivedObservation:
        ctx.checkpoint()
        schema = response_schema(task_schema, region.allowed_candidate_target_ids)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": region.media_type,
                                    "data": b64encode(region.image).decode("ascii"),
                                },
                            },
                            {
                                "type": "text",
                                "text": _PROMPT_TEMPLATE.format(
                                    task=region.task,
                                    candidate_context=canonical_bytes(
                                        [
                                            item.model_dump(mode="json")
                                            for item in region.candidate_context
                                        ]
                                    ).decode("utf-8"),
                                ),
                            },
                        ],
                    }
                ],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            ctx.checkpoint()
            if (
                getattr(response, "stop_reason", None) != "end_turn"
                or len(response.content) != 1
                or getattr(response.content[0], "type", None) != "text"
            ):
                raise ValueError
            return parse_response(
                response.content[0].text,
                region,
                self.model,
                self.prompt_schema_version,
                task_schema,
                prompt_renderer_identity=self.prompt_renderer_identity,
            )
        except TychoraError:
            raise
        except Exception:  # noqa: BLE001 - provider failures must be sanitized
            raise TychoraError(
                "PROVIDER_FAILURE", "Vision provider request failed.", retryable=True
            ) from None


def create_provider(
    *,
    offline: bool,
    capabilities: frozenset[str],
    model: str,
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> AnthropicVisionProvider:
    if offline or "vision" not in capabilities or not model.strip() or not api_key:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Explicit online vision capability, model and credential are required",
        )
    if client_factory is None:
        import anthropic
        import httpx2

        http = httpx2.Client(
            timeout=httpx2.Timeout(30.0), trust_env=False, follow_redirects=False
        )
        client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=30.0,
            http_client=http,
        )
    else:
        client = client_factory(
            api_key=api_key,
            max_retries=0,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        )
    return AnthropicVisionProvider(
        client, model=model, prompt_schema_version="vision-response-v1"
    )


async def dispatch_captured_vision(
    owner: WorkspaceOwner,
    authorization: CapturedRegionAuthorization,
    task_schema: JsonValue,
    ctx: ExecutionContext,
    *,
    scope: RequestScope,
    policy_epoch: int,
    model: str,
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> DerivedObservation:
    """Authorize after queue admission, then construct and close the client."""
    capabilities = owner.scope.narrow(scope).capabilities

    def call() -> DerivedObservation:
        provider = create_provider(
            offline=owner.config.values.offline,
            capabilities=capabilities,
            model=model,
            api_key=api_key,
            client_factory=client_factory,
        )
        try:
            return provider.interpret(authorization.region, task_schema, ctx)
        finally:
            provider.close()

    return await owner.run_captured_provider(
        "anthropic",
        call,
        ctx,
        scope=scope,
        authorization=authorization,
        policy_epoch=policy_epoch,
    )


def validate_target(target: str | None, allowed: frozenset[str]) -> None:
    if target is not None and target not in allowed:
        raise TychoraError("PROVIDER_FAILURE", "Vision returned an unknown target.")


def parse_response(
    payload: str,
    region: RegionInput,
    model: str,
    prompt_schema_version: str,
    task_schema: JsonValue,
    *,
    prompt_renderer_identity: str | None = None,
) -> DerivedObservation:
    schema_id, validator = _task_validator(task_schema)
    try:
        response = _Response.model_validate_json(payload)
        validate_target(response.target_id, region.allowed_candidate_target_ids)
        if response.interpretation is not None:
            try:
                validator.validate(schema_id, response.interpretation)
            except TychoraError:
                raise ValueError from None
        task_schema_sha = digest("vision-task-schema-v1", task_schema)
        prompt_identity = vision_prompt_identity(
            prompt_schema_version, task_schema, prompt_renderer_identity
        )
        return DerivedObservation(
            cache_key=vision_cache_key(
                region.image_sha256,
                region.task,
                model,
                prompt_identity,
                region.context_sha256,
            ),
            task=region.task,
            image_sha256=region.image_sha256,
            original_image_sha256=region.original_image_sha256,
            original_pixel_sha256=region.original_pixel_sha256,
            region_pixel_sha256=region.region_pixel_sha256,
            context_sha256=region.context_sha256,
            target_id=response.target_id,
            literal_transcription=response.literal_transcription,
            interpretation=response.interpretation,
            uncertainties=response.uncertainties,
            model=model,
            prompt_schema_version=prompt_schema_version,
            task_schema_sha256=task_schema_sha,
            prompt_renderer_identity=prompt_renderer_identity,
            prompt_identity=prompt_identity,
        )
    except TychoraError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise TychoraError(
            "PROVIDER_FAILURE", "Vision returned an invalid structured response."
        ) from None
