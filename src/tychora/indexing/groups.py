"""Deterministic contextual and independent provider input groups."""

from collections import defaultdict
from typing import Literal

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import ContextGroup, SearchWindow
from tychora.ports.tokens import TokenCounter

Contextualization = Literal["joint", "independent"]


def representation_input_sha(
    input_texts: tuple[str, ...],
    config: ResolvedConfig,
    contextualization: Contextualization,
    options: FrozenJsonMap,
) -> str:
    """Hash provider content/config separately from citation and permission identity."""
    settings = config.values.providers.text
    if settings.provider == "disabled" or settings.model is None:
        raise ValueError("Text representation requires an enabled provider model")
    return digest(
        "embedding-input-v1",
        input_texts,
        settings.provider,
        settings.model,
        settings.dimensions,
        settings.dtype,
        settings.representation_version,
        contextualization,
        options,
    )


def _capsule(windows: tuple[SearchWindow, ...]) -> str:
    first = windows[0]
    paths = []
    seen = set()
    for window in windows:
        path = (window.heading_path, window.symbol_path)
        if path not in seen:
            seen.add(path)
            paths.append({"heading": window.heading_path, "symbol": window.symbol_path})
    return canonical_bytes(
        {"title": first.title, "kind": first.kind_id, "paths": paths}
    ).decode("utf-8")


def _contextualization(
    windows: tuple[SearchWindow, ...], model: str
) -> Contextualization:
    is_code = all(window.kind_id.split("@")[0] == "code_file" for window in windows)
    if any(
        (window.kind_id.split("@")[0] == "code_file") != is_code for window in windows
    ):
        raise ValueError("Context group mixes code and document representations")
    return "independent" if is_code and model != "voyage-context-4" else "joint"


def _validate_windows(windows: tuple[SearchWindow, ...], counter: TokenCounter) -> None:
    if len({window.chunk_id for window in windows}) != len(windows):
        raise ValueError("Context groups require unique windows")
    for window in windows:
        if (
            window.tokenizer_id != counter.tokenizer_id
            or counter.count(window.text) != window.token_count
        ):
            raise ValueError("Window count differs from the qualified tokenizer")


def _make_group(
    windows: tuple[SearchWindow, ...],
    capsule: str,
    config: ResolvedConfig,
    counter: TokenCounter,
    contextualization: Contextualization,
) -> ContextGroup:
    settings = config.values.providers.text
    inputs = (capsule, *(window.text for window in windows))
    options = FrozenJsonMap(
        {
            "input_type": "document",
            "enable_auto_chunking": settings.enable_auto_chunking,
            "truncation": settings.truncation,
        }
    )
    input_sha = representation_input_sha(inputs, config, contextualization, options)
    tokens = sum(counter.count(text) for text in inputs)
    dependencies = frozenset(
        dependency for window in windows for dependency in window.dependency_ids
    )
    group_id = digest(
        "context-group-input-v1",
        windows[0].extraction_id,
        windows[0].context_group_key,
        [window.chunk_id for window in windows],
        input_sha,
    )
    return ContextGroup(
        group_id=group_id,
        context_group_key=windows[0].context_group_key,
        extraction_id=windows[0].extraction_id,
        revision_id=windows[0].revision_id,
        capsule=capsule,
        input_texts=inputs,
        window_ids=tuple(window.chunk_id for window in windows),
        window_input_shas=(input_sha,) * len(windows),
        input_sha=input_sha,
        dependency_ids=dependencies,
        token_count=tokens,
        tokenizer_id=counter.tokenizer_id,
        provider=settings.provider,
        model=settings.model or "",
        dimensions=settings.dimensions,
        dtype=settings.dtype,
        representation_version=settings.representation_version,
        contextualization=contextualization,
        options=options,
    )


def _split_joint(
    windows: tuple[SearchWindow, ...],
    config: ResolvedConfig,
    counter: TokenCounter,
) -> tuple[ContextGroup, ...]:
    limit = config.values.providers.text.max_group_tokens
    capsule = _capsule(windows)
    capsule_tokens = counter.count(capsule)
    if capsule_tokens <= 0 or capsule_tokens >= limit:
        raise TychoraError(
            "INVALID_REQUEST", "Context capsule exceeds the group token limit"
        )
    output = []
    current: list[SearchWindow] = []
    current_tokens = capsule_tokens
    for window in windows:
        if capsule_tokens + window.token_count > limit:
            raise TychoraError(
                "INVALID_REQUEST", "One atomic window exceeds the group token limit"
            )
        if current and current_tokens + window.token_count > limit:
            output.append(
                _make_group(tuple(current), capsule, config, counter, "joint")
            )
            current = []
            current_tokens = capsule_tokens
        current.append(window)
        current_tokens += window.token_count
    if current:
        output.append(_make_group(tuple(current), capsule, config, counter, "joint"))
    return tuple(output)


def build_groups(
    windows: tuple[SearchWindow, ...],
    config: ResolvedConfig,
    counter: TokenCounter,
) -> tuple[ContextGroup, ...]:
    """Build bounded pure inputs; provider/model/storage construction stays upstream."""
    if not windows:
        return ()
    settings = config.values.providers.text
    if settings.model is None or settings.provider == "disabled":
        raise ValueError("Text groups require an enabled provider model")
    if counter.model != settings.model:
        raise ValueError("Token counter model differs from configured provider model")
    _validate_windows(windows, counter)
    grouped: dict[tuple[str, str, str], list[SearchWindow]] = defaultdict(list)
    for window in windows:
        grouped[
            (window.extraction_id, window.revision_id, window.context_group_key)
        ].append(window)
    built = []
    for key in sorted(grouped):
        ordered = tuple(
            sorted(
                grouped[key],
                key=lambda item: (item.unit_ordinal, item.char_start, item.chunk_id),
            )
        )
        first = ordered[0]
        if any(
            window.title != first.title
            or window.kind_id != first.kind_id
            or window.boundary_key != first.boundary_key
            for window in ordered
        ):
            raise ValueError("Context group canonical metadata differs")
        mode = _contextualization(ordered, settings.model)
        if mode == "joint":
            built.extend(_split_joint(ordered, config, counter))
        else:
            for window in ordered:
                capsule = _capsule((window,))
                group = _make_group((window,), capsule, config, counter, mode)
                if group.token_count > settings.max_group_tokens:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "One atomic window exceeds the group token limit",
                    )
                built.append(group)
    packed = []
    request_index = 0
    request_tokens = 0
    for group in built:
        if group.token_count > settings.max_request_tokens:
            raise TychoraError(
                "INVALID_REQUEST", "One atomic group exceeds the request token limit"
            )
        if (
            request_tokens
            and request_tokens + group.token_count > settings.max_request_tokens
        ):
            request_index += 1
            request_tokens = 0
        packed.append(group.model_copy(update={"request_index": request_index}))
        request_tokens += group.token_count
    return tuple(packed)
