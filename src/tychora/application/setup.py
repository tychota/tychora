"""Read-only model planning and explicit verified asset provisioning."""

import json
import os
import stat
from base64 import b64decode
from collections.abc import Callable
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Annotated, Self
from uuid import uuid4

from pydantic import Field, model_validator

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, NonEmptyStr, PositiveInt, Sha256
from tychora.contracts.embeddings import VisualEmbeddingLimits
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import JobHandle
from tychora.contracts.requests import RequestScope
from tychora.ports.artifacts import Artifacts
from tychora.ports.execution import ExecutionContext
from tychora.ports.jobs import JobRepository

MANIFEST = Path(__file__).parents[1] / "resources" / "model-assets.json"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_CL100K_PATTERN = r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
_CL100K_SPECIAL = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}


class ModelAsset(Contract):
    id: NonEmptyStr
    selection: NonEmptyStr
    path: NonEmptyStr
    sha256: Sha256
    size_bytes: PositiveInt
    url: Annotated[str, Field(pattern=r"^https://")]
    license: NonEmptyStr
    owner: NonEmptyStr
    revision: NonEmptyStr
    notice_url: Annotated[str, Field(pattern=r"^https://")]

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        path = PurePosixPath(self.path)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("Model asset path must be a safe relative path")
        return self


class ModelNotice(Contract):
    selection: NonEmptyStr
    attribution: NonEmptyStr
    license: NonEmptyStr
    notice_url: Annotated[str, Field(pattern=r"^https://")]
    model_card_url: Annotated[str, Field(pattern=r"^https://")]


class ModelAssetPlan(Contract):
    manifest_sha256: Sha256
    selection: tuple[NonEmptyStr, ...]
    assets: tuple[ModelAsset, ...]
    notices: tuple[ModelNotice, ...]
    total_size_bytes: int


def _open_directory(path: Path, *, create: bool) -> int:
    """Open every absolute path component without following a symlink."""
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_relative_parent(
    root_fd: int, path: PurePosixPath, *, create: bool
) -> tuple[int, str]:
    parts = path.parts
    if not parts:
        raise OSError("empty model asset path")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _open_parent(path: Path, *, create: bool) -> tuple[int, str]:
    absolute = path.absolute()
    if not absolute.name:
        raise OSError("model asset path requires a filename")
    return _open_directory(absolute.parent, create=create), absolute.name


def _read_verified_at(
    root_fd: int, asset: ModelAsset, ctx: ExecutionContext, *, retain: bool = True
) -> bytes | None:
    parent_fd = fd = None
    try:
        parent_fd, name = _open_relative_parent(
            root_fd, PurePosixPath(asset.path), create=False
        )
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != asset.size_bytes:
            return None
        chunks = [] if retain else None
        hasher = sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                ctx.checkpoint()
                if chunks is not None:
                    chunks.append(block)
                hasher.update(block)
        if hasher.hexdigest() != asset.sha256:
            return None
        return b"".join(chunks) if chunks is not None else b""
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


class SetupService:
    def __init__(
        self,
        cache_root: Path,
        *,
        offline: bool,
        fetch: Callable[[str], bytes] | None = None,
        jobs: JobRepository | None = None,
        scope: RequestScope | None = None,
        effective_config: ArtifactRef | None = None,
        artifacts: Artifacts | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.offline = offline
        self._fetch = fetch
        self.jobs = jobs
        self.scope = scope
        self.effective_config = effective_config
        self.artifacts = artifacts

    def plan(self, selection: tuple[str, ...]) -> ModelAssetPlan:
        raw = MANIFEST.read_bytes()
        manifest = json.loads(raw)
        known = {item["selection"] for item in manifest["assets"]}
        if len(selection) != len(set(selection)) or any(
            name not in known for name in selection
        ):
            raise ValueError("Model selection must contain unique allowlisted names")
        selected = tuple(
            ModelAsset.model_validate(item)
            for item in manifest["assets"]
            if item["selection"] in selection
        )
        notices = tuple(
            ModelNotice.model_validate(item)
            for item in manifest["notices"]
            if item["selection"] in selection
        )
        if {notice.selection for notice in notices} != set(selection):
            raise ValueError("Each model selection requires an explicit notice")
        return ModelAssetPlan(
            manifest_sha256=sha256(canonical_bytes(manifest)).hexdigest(),
            selection=selection,
            assets=selected,
            notices=notices,
            total_size_bytes=sum(asset.size_bytes for asset in selected),
        )

    def apply(
        self,
        plan: ModelAssetPlan,
        local_bundle: Path | None,
        ctx: ExecutionContext,
    ) -> JobHandle:
        ctx.checkpoint()
        expected = self.plan(plan.selection)
        if plan != expected:
            raise TychoraError("INTEGRITY_ERROR", "Model asset plan differs")
        if (
            self.jobs is None
            or self.scope is None
            or self.effective_config is None
            or self.artifacts is None
        ):
            raise TychoraError(
                "CAPABILITY_MISSING", "Setup requires the durable workspace job service"
            )
        capture = self.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "job_capture",
                        "schema_version": "1",
                        "required_artifacts": [],
                        "required_manifests": [],
                        "data": {"model_asset_plan": plan.model_dump(mode="json")},
                    }
                )
            ),
            "application/json",
        )
        job = self.jobs.accept(
            "setup",
            self.scope,
            None,
            {"model_asset_plan": plan.model_dump(mode="json")},
            self.effective_config,
        )
        self.jobs.capture(job.id, capture)
        self.jobs.start(job.id)
        self.jobs.stage(job.id, "provision")
        try:
            self._provision(plan, local_bundle, ctx)
            ctx.checkpoint()
            result = self.artifacts.put_stream(
                BytesIO(
                    canonical_bytes(
                        {
                            "type": "setup_result",
                            "schema_version": "1",
                            "data": {
                                "manifest_sha256": plan.manifest_sha256,
                                "selection": list(plan.selection),
                                "notices": [
                                    notice.model_dump(mode="json")
                                    for notice in plan.notices
                                ],
                                "assets": [
                                    {
                                        "id": asset.id,
                                        "path": asset.path,
                                        "sha256": asset.sha256,
                                        "size_bytes": asset.size_bytes,
                                    }
                                    for asset in plan.assets
                                ],
                            },
                        }
                    )
                ),
                "application/json",
            )
            request_digest = self.jobs.get(job.id).request_digest
            self.jobs.checkpoint(job.id, "provision", request_digest, result)
            ctx.checkpoint()
            status = self.jobs.complete(job.id, result=result)
            return JobHandle(
                id=status.id, operation=status.operation, state=status.state
            )
        except TychoraError as error:
            self.jobs.fail(job.id, error)
            raise

    def _provision(
        self,
        plan: ModelAssetPlan,
        local_bundle: Path | None,
        ctx: ExecutionContext,
    ) -> None:
        cache_fd = bundle_fd = None
        try:
            cache_fd = _open_directory(self.cache_root, create=True)
            if local_bundle is not None:
                bundle_fd = _open_directory(local_bundle, create=False)
            for asset in plan.assets:
                ctx.checkpoint()
                valid = (
                    _read_verified_at(cache_fd, asset, ctx, retain=False) is not None
                )
                ctx.checkpoint()
                if valid:
                    continue
                if bundle_fd is None:
                    if self.offline:
                        raise TychoraError(
                            "CAPABILITY_MISSING",
                            "Offline setup requires a verified cache or local bundle",
                        )
                    payload = self._download(asset, ctx)
                else:
                    payload = _read_verified_at(bundle_fd, asset, ctx)
                    if payload is None:
                        raise TychoraError(
                            "INTEGRITY_ERROR",
                            "Local model bundle is missing or corrupt",
                        )
                ctx.checkpoint()
                self._publish_at(cache_fd, payload, asset)
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Model bundle path could not be opened safely"
            ) from None
        finally:
            if bundle_fd is not None:
                os.close(bundle_fd)
            if cache_fd is not None:
                os.close(cache_fd)

    @staticmethod
    def _valid(path: Path, asset: ModelAsset) -> bool:
        fd = None
        try:
            parent_fd, name = _open_parent(path, create=False)
            try:
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != asset.size_bytes:
                return False
            hasher = sha256()
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while block := stream.read(1024 * 1024):
                    hasher.update(block)
            return hasher.hexdigest() == asset.sha256
        except OSError:
            return False
        finally:
            if fd is not None:
                os.close(fd)

    def _download(self, asset: ModelAsset, ctx: ExecutionContext) -> bytes:
        try:
            ctx.checkpoint()
            if self._fetch is not None:
                payload = self._fetch(asset.url)
            else:
                from urllib.request import urlopen

                remaining = (
                    max(0.001, ctx.deadline - __import__("time").monotonic())
                    if ctx.deadline is not None
                    else 60.0
                )
                with urlopen(asset.url, timeout=min(1.0, remaining)) as response:
                    chunks = []
                    size = 0
                    while size <= asset.size_bytes:
                        ctx.checkpoint()
                        block = response.read(
                            min(1024 * 1024, asset.size_bytes + 1 - size)
                        )
                        if not block:
                            break
                        chunks.append(block)
                        size += len(block)
                    payload = b"".join(chunks)
            ctx.checkpoint()
        except TychoraError:
            raise
        except Exception:  # noqa: BLE001 - downloader details must be sanitized
            ctx.checkpoint()
            raise TychoraError(
                "PROVIDER_FAILURE", "Model asset download failed", retryable=True
            ) from None
        if (
            len(payload) != asset.size_bytes
            or sha256(payload).hexdigest() != asset.sha256
        ):
            raise TychoraError("INTEGRITY_ERROR", "Downloaded model asset differs")
        return payload

    def _publish_at(self, root_fd: int, payload: bytes, asset: ModelAsset) -> None:
        if (
            len(payload) != asset.size_bytes
            or sha256(payload).hexdigest() != asset.sha256
        ):
            raise TychoraError("INTEGRITY_ERROR", "Model asset differs")
        parent_fd = None
        staging = ".staging-" + uuid4().hex
        try:
            parent_fd, name = _open_relative_parent(
                root_fd, PurePosixPath(asset.path), create=True
            )
            fd = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.chmod(name, 0o600, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Model asset could not be published safely"
            ) from None
        finally:
            try:
                if parent_fd is not None:
                    os.unlink(staging, dir_fd=parent_fd)
            except OSError:
                pass
            if parent_fd is not None:
                os.close(parent_fd)

    @staticmethod
    def _safe_asset_path(root: Path, asset: ModelAsset) -> Path:
        if root.is_symlink():
            raise TychoraError("INTEGRITY_ERROR", "Model bundle path is unsafe")
        current = root
        for part in PurePosixPath(asset.path).parts:
            current /= part
            if current.is_symlink():
                raise TychoraError("INTEGRITY_ERROR", "Model bundle path is unsafe")
        return current


def _verified_selection(cache_root: Path, selection: str) -> tuple[ModelAsset, ...]:
    service = SetupService(cache_root, offline=True)
    assets = service.plan((selection,)).assets
    if not assets or any(
        not service._valid(service._safe_asset_path(cache_root, asset), asset)
        for asset in assets
    ):
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Selected local tokenizer assets are missing or corrupt",
        )
    return assets


def load_output_tokenizer(cache_root: Path):
    """Construct cl100k_base from the verified local ranks file without SDK fetches."""
    import tiktoken

    (asset,) = _verified_selection(cache_root, "output-tokenizer")
    try:
        lines = (cache_root / asset.path).read_bytes().splitlines()
        mergeable_ranks: dict[bytes, int] = {}
        for line in lines:
            encoded, rank = line.split()
            token = b64decode(encoded, validate=True)
            number = int(rank)
            if token in mergeable_ranks:
                raise ValueError
            mergeable_ranks[token] = number
        if len(mergeable_ranks) != 100256 or set(mergeable_ranks.values()) != set(
            range(100256)
        ):
            raise ValueError
    except (OSError, ValueError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Local output tokenizer ranks are invalid"
        ) from None
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=_CL100K_PATTERN,
        mergeable_ranks=mergeable_ranks,
        special_tokens=_CL100K_SPECIAL,
    )


def load_voyage_tokenizer(cache_root: Path, model: str):
    """Load a model-bound Voyage tokenizer only from a verified local inventory."""
    if model not in (
        "voyage-context-4",
        "voyage-code-4",
        "voyage-code-3",
        "voyage-multimodal-3.5",
        "rerank-2.5",
    ):
        raise TychoraError("INVALID_REQUEST", "Unknown local Voyage tokenizer model")
    assets = _verified_selection(cache_root, model)
    tokenizer = next(
        asset for asset in assets if Path(asset.path).name == "tokenizer.json"
    )
    try:
        from tokenizers import Tokenizer

        result = Tokenizer.from_file(str(cache_root / tokenizer.path))
        result.no_truncation()
        return result
    except (OSError, ValueError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Local Voyage tokenizer cannot be loaded"
        ) from None


def load_voyage_multimodal_accounting(cache_root: Path) -> VisualEmbeddingLimits:
    """Read only the pinned local multimodal tokenizer/configuration resources."""
    try:
        assets = _verified_selection(cache_root, "voyage-multimodal-3.5")
        tokenizer = next(
            asset for asset in assets if Path(asset.path).name == "tokenizer.json"
        )
        config = next(
            asset for asset in assets if Path(asset.path).name == "client_config.json"
        )
        values = json.loads((cache_root / config.path).read_bytes())
        expected = {
            "multimodal_image_pixels_min",
            "multimodal_image_pixels_max",
            "multimodal_image_to_tokens_ratio",
            "multimodal_video_pixels_min",
            "multimodal_video_pixels_max",
            "multimodal_video_to_tokens_ratio",
        }
        if (
            not isinstance(values, dict)
            or set(values) != expected
            or any(type(value) is not int or value <= 0 for value in values.values())
            or values["multimodal_image_pixels_min"] != 50_000
            or values["multimodal_image_pixels_max"] != 2_000_000
            or values["multimodal_image_to_tokens_ratio"] != 560
        ):
            raise ValueError
    except (
        OSError,
        StopIteration,
        TychoraError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Local multimodal accounting resources are unavailable.",
        ) from None
    return VisualEmbeddingLimits(
        accounting_version="voyage-multimodal-accounting-v1",
        model_revision=config.revision,
        tokenizer_sha256=tokenizer.sha256,
        client_config_sha256=config.sha256,
        accounting_image_pixels_min=values["multimodal_image_pixels_min"],
        accounting_image_pixels_max=values["multimodal_image_pixels_max"],
        image_pixels_per_token=values["multimodal_image_to_tokens_ratio"],
        source_pixels_max=16_000_000,
        image_encoded_bytes_max=20_000_000,
        input_tokens_max=32_000,
        request_assets_max=32,
        request_tokens_max=100_000,
        request_wire_bytes_max=64 * 1024 * 1024,
        decoded_image_bytes_max=48_000_000,
    )
