"""Deterministic contextual and independent provider input groups."""

from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import (
    ContextGroup,
    OwnerDependencies,
    RepresentationFacts,
    SearchWindow,
)
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
    """Windows carry their exact count from ``window_unit``; check identity only."""
    if len({window.chunk_id for window in windows}) != len(windows):
        raise ValueError("Context groups require unique windows")
    if any(window.tokenizer_id != counter.tokenizer_id for window in windows):
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
    tokens = counter.count(capsule) + sum(window.token_count for window in windows)
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
    output = []
    current: list[SearchWindow] = []
    for window in windows:
        candidate = (*current, window)
        capsule = _capsule(candidate)
        candidate_tokens = counter.count(capsule) + sum(
            item.token_count for item in candidate
        )
        if candidate_tokens <= limit:
            current.append(window)
            continue
        if not current:
            raise TychoraError(
                "INVALID_REQUEST", "One atomic window exceeds the group token limit"
            )
        subgroup = tuple(current)
        output.append(
            _make_group(subgroup, _capsule(subgroup), config, counter, "joint")
        )
        current = [window]
        single_capsule = _capsule((window,))
        if counter.count(single_capsule) + window.token_count > limit:
            raise TychoraError(
                "INVALID_REQUEST", "One atomic window exceeds the group token limit"
            )
    if current:
        subgroup = tuple(current)
        output.append(
            _make_group(subgroup, _capsule(subgroup), config, counter, "joint")
        )
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
            or window.context_key != first.context_key
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


def group_facts(
    extraction_id: str, groups: Iterable[ContextGroup]
) -> RepresentationFacts:
    """The dependency set of each context group, keyed by group ID."""
    found = {group.group_id: group.dependency_ids for group in groups}
    return RepresentationFacts(
        extraction_id=extraction_id,
        dependencies=tuple(
            OwnerDependencies(owner_id=group_id, dependency_ids=dependencies)
            for group_id, dependencies in sorted(found.items())
        ),
    )
