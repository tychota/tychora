"""Explicit local OCR in the owner-managed isolated parser process."""

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast

from tychora.application.owner import StagedOutput, WorkspaceOwner
from tychora.contracts.composition import source_context_identity
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.ports.execution import ExecutionContext
from tychora.ports.vision import (
    DerivedObservation,
    RegionInput,
    vision_cache_key,
    vision_prompt_identity,
)

_MODELS = (
    (
        "PP-OCRv6_det_small.onnx",
        9_929_594,
        "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    ),
    (
        "PP-OCRv6_rec_small.onnx",
        21_234_383,
        "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    ),
    (
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        585_532,
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    ),
)
_RUNTIME_VERSIONS = {"rapidocr": "3.9.2", "onnxruntime": "1.30.0"}


def _qualified_runtime_versions() -> dict[str, str]:
    try:
        installed = {package: version(package) for package in _RUNTIME_VERSIONS}
    except PackageNotFoundError:
        raise TychoraError(
            "CAPABILITY_MISSING", "Installed local OCR runtime is not qualified"
        ) from None
    if installed != _RUNTIME_VERSIONS:
        raise TychoraError(
            "CAPABILITY_MISSING", "Installed local OCR runtime is not qualified"
        )
    return installed


def _verify_models(root: Path) -> tuple[Path, Path, Path]:
    paths = []
    for name, size, expected in _MODELS:
        path = root / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise ValueError
            hasher = sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    hasher.update(block)
            if hasher.hexdigest() != expected:
                raise ValueError
        except (OSError, ValueError):
            raise TychoraError(
                "CAPABILITY_MISSING", "Local OCR model bundle is missing or corrupt"
            ) from None
        paths.append(path.resolve())
    return paths[0], paths[1], paths[2]


@dataclass(frozen=True)
class _OcrWorker:
    detection: str
    recognition: str
    classification: str

    def __call__(self, paths: tuple[Path, ...]) -> tuple[StagedOutput, ...]:
        if len(paths) != 1:
            raise ValueError("Local OCR requires one staged region")
        staging = paths[0].parent
        os.chdir(staging)
        initial = {entry.name for entry in staging.iterdir()}
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import onnxruntime

        onnxruntime.disable_telemetry_events()
        from rapidocr import RapidOCR

        reader = RapidOCR(
            params={
                "Det.model_path": self.detection,
                "Rec.model_path": self.recognition,
                "Cls.model_path": self.classification,
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "error",
            }
        )
        result = cast(Any, reader(paths[0]))
        unexpected = {entry.name for entry in staging.iterdir()} - initial
        if unexpected:
            raise TychoraError(
                "PROVIDER_FAILURE", "Local OCR created an unexpected runtime file"
            )
        output = canonical_bytes(
            {
                "literal_transcription": "\n".join(result.txts or ()),
                "boxes": result.boxes.tolist() if result.boxes is not None else [],
                "raw_model_scores": list(result.scores or ()),
                "inventory": [
                    {"path": name, "size_bytes": size, "sha256": model_sha}
                    for name, size, model_sha in _MODELS
                ],
                "runtime": {
                    "rapidocr": version("rapidocr"),
                    "onnxruntime": version("onnxruntime"),
                    "threads": 2,
                    "telemetry": "disabled-before-native-import",
                },
            }
        )
        target = staging / "ocr-result.json"
        with target.open("xb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)
        return (StagedOutput(target.name, "application/json", len(output)),)


class LocalOcrProvider:
    def __init__(self, owner: WorkspaceOwner, model_root: Path):
        self.owner = owner
        self.models = _verify_models(model_root)
        runtime = _qualified_runtime_versions()
        self.model = "rapidocr==3.9.2:" + digest(
            "rapidocr-model-inventory-v1",
            [
                {"path": name, "size_bytes": size, "sha256": model_sha}
                for name, size, model_sha in _MODELS
            ],
            runtime,
        )
        self.prompt_schema_version = "local-ocr-v1"

    def interpret(
        self, region: RegionInput, task_schema: JsonValue, ctx: ExecutionContext
    ) -> DerivedObservation:
        if region.task != "ocr" or canonical_bytes(task_schema) != b'{"type":"null"}':
            raise TychoraError(
                "INVALID_REQUEST", "Local OCR only accepts the explicit OCR task"
            )
        ctx.checkpoint()
        image_ref = self.owner.run_blocking_sync(
            lambda: self.owner.artifacts.put_stream(
                BytesIO(region.image), region.media_type
            ),
            ctx,
        )
        if image_ref != region.image_ref:
            raise TychoraError(
                "INTEGRITY_ERROR", "Local OCR region differs from retained artifact"
            )
        return self.owner.run_parser_sync(
            _OcrWorker(*(str(path) for path in self.models)),
            (image_ref,),
            ctx,
            collector=lambda outputs: self._retain(outputs, region),
        )

    def _retain(
        self,
        outputs: tuple[tuple[StagedOutput, BinaryIO], ...],
        region: RegionInput,
    ) -> DerivedObservation:
        if len(outputs) != 1 or outputs[0][0].name != "ocr-result.json":
            raise TychoraError("INTEGRITY_ERROR", "Unexpected local OCR output")
        payload = outputs[0][1].read(16 * 1024 * 1024 + 1)
        try:
            result = json.loads(payload)
            if canonical_bytes(result) != payload:
                raise ValueError
            literal = result["literal_transcription"]
            if not isinstance(literal, str):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Local OCR returned an invalid result"
            ) from None
        return retain_local_ocr_observation(
            self.owner,
            region,
            result,
            model=self.model,
            prompt_schema_version=self.prompt_schema_version,
        )


def retain_local_ocr_observation(
    owner, region, result, *, model, prompt_schema_version
):
    """Shared receipt serializer for computed OCR; performs no model invocation."""
    literal = result["literal_transcription"]
    task_schema = {"type": "null"}
    task_schema_sha = digest("vision-task-schema-v1", task_schema)
    prompt_identity = vision_prompt_identity(prompt_schema_version, task_schema)
    receipt = canonical_bytes(
        {
            "type": "vision_derivation",
            "schema_version": "1",
            "data": {
                "task": region.task,
                "source_native_ref": region.source_native_ref,
                "image_sha256": region.image_sha256,
                "original_image_sha256": region.original_image_sha256,
                "original_pixel_sha256": region.original_pixel_sha256,
                "region_pixel_sha256": region.region_pixel_sha256,
                "context_sha256": region.context_sha256,
                "source_context_sha256": source_context_identity(region.source_context),
                "dependency_ids": sorted(region.dependency_ids),
                "model": model,
                "prompt_schema_version": prompt_schema_version,
                "task_schema_sha256": task_schema_sha,
                "prompt_identity": prompt_identity,
                "result": {
                    **result,
                    "uncertainties": ["OCR model scores are uncalibrated."],
                },
            },
        }
    )
    derivation = owner.artifacts.put_stream(BytesIO(receipt), "application/json")
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
        literal_transcription=literal,
        interpretation=None,
        uncertainties=("OCR model scores are uncalibrated.",),
        model=model,
        prompt_schema_version=prompt_schema_version,
        task_schema_sha256=task_schema_sha,
        prompt_identity=prompt_identity,
        derivation=derivation,
    )
