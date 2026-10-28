"""What one query runs: the configured channels narrowed by its request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import (
    ChannelSelection,
    ImageInput,
    PassageInput,
    QueryRequest,
    QuestionInput,
)

if TYPE_CHECKING:
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.configuration.models import ResolvedConfig
    from tychora.ports.graph import GraphStore
    from tychora.ports.query import TextQueryProvider, VisualQueryProvider
    from tychora.ports.tokens import TokenCounter
    from tychora.ports.vectors import VectorIndex
    from tychora.retrieval.cache import QueryCaches

_SWITCHES = ("lexical", "text", "image", "graph")
_COVERAGE_NAMES = {"image": "visual"}


def _not_configured(name: str) -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        f"The {name} channel is not configured; a request can only narrow channels",
        reason="channel.not_configured",
    )


@dataclass(frozen=True)
class QueryBindings:
    """The optional query collaborators a workspace is built with."""

    text_provider: TextQueryProvider | None = None
    text_index: VectorIndex | None = None
    visual_provider: VisualQueryProvider | None = None
    visual_index: VectorIndex | None = None
    reranker: VoyageReranker | None = None
    rerank_counter: TokenCounter | None = None
    rerank_unavailable: TychoraError | None = None
    graph: GraphStore | None = None

    def validate(self, config: ResolvedConfig, caches: QueryCaches) -> None:
        """Refuse providers whose identity differs from the configured one."""
        for provider, settings, modality in (
            (self.text_provider, config.values.providers.text, "text"),
            (self.visual_provider, config.values.providers.image, "visual"),
        ):
            if provider is None:
                continue
            caps = provider.capabilities
            if (
                provider.caches is not caches
                or caps.provider != settings.provider
                or caps.model != settings.model
                or caps.dimensions != settings.dimensions
                or caps.modality != modality
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query provider binding differs from configuration.",
                    reason="provider.binding_mismatch",
                )


@dataclass(frozen=True)
class QueryPlan:
    """The channels, graph strategies and rerank stage one query runs.

    ``explicit`` names the channels the request itself selected: only those
    are refused when they cannot run. A channel that is merely configured and
    turns out unavailable is reported by coverage instead. ``rerank`` is
    whether the rerank stage runs; the stage reports a disabled reranker.
    """

    lexical: bool
    text: bool
    image: bool
    graph_strategies: frozenset[str]
    rerank: bool
    skipped: tuple[str, ...]
    explicit: frozenset[str] = frozenset()


def needs_text(request: QueryRequest) -> bool:
    """Whether the request asks for the text vector channel."""
    return (
        not request.exact_only
        and "text" in request.modalities
        and isinstance(request.input, (QuestionInput, PassageInput))
    )


def needs_visual(request: QueryRequest) -> bool:
    """Whether the request asks for the visual vector channel."""
    return not request.exact_only and (
        "image" in request.modalities or isinstance(request.input, ImageInput)
    )


def _configured_strategies(config: ResolvedConfig) -> frozenset[str]:
    graph = config.values.retrieval.graph
    return frozenset(
        name
        for name, enabled in (
            ("structural", graph.structural == "code_context@1"),
            ("semantic", graph.semantic),
        )
        if enabled
    )


def _configured(config: ResolvedConfig) -> dict[str, bool]:
    """Which channels the profile configures, whatever is bound at runtime."""
    values = config.values
    return {
        "lexical": True,
        "text": values.index.text.backend != "disabled",
        "image": values.index.image.backend != "disabled",
        "rerank": values.providers.rerank.provider != "disabled",
    }


def _bound(bindings: QueryBindings) -> dict[str, bool]:
    """Which channels have the collaborators they need to run."""
    return {
        "lexical": True,
        "text": bindings.text_provider is not None and bindings.text_index is not None,
        "image": bindings.visual_provider is not None
        and bindings.visual_index is not None,
        "rerank": bindings.reranker is not None
        and bindings.rerank_counter is not None
        and bindings.rerank_unavailable is None,
        "graph": bindings.graph is not None,
    }


def _graph_off(selection: ChannelSelection) -> bool:
    return not selection.graph or selection.graph_strategies == frozenset()


def _explicit(selection: ChannelSelection, request: QueryRequest) -> frozenset[str]:
    """The channels the request turned on itself, rather than by default."""
    chosen = {
        name
        for name in _SWITCHES
        if name in selection.model_fields_set and getattr(selection, name)
    }
    if selection.graph_strategies and selection.graph:
        chosen.add("graph")
    if request.rerank == "on":
        chosen.add("rerank")
    return frozenset(chosen)


def _requested_strategies(
    selection: ChannelSelection, config: ResolvedConfig
) -> frozenset[str]:
    if _graph_off(selection):
        return frozenset()
    if selection.graph_strategies is not None:
        return frozenset(selection.graph_strategies)
    return _configured_strategies(config)


def _require_runnable(
    explicit: frozenset[str],
    strategies: frozenset[str],
    config: ResolvedConfig,
    bindings: QueryBindings,
) -> None:
    """Refuse an explicitly selected channel the workspace cannot run."""
    configured, bound = _configured(config), _bound(bindings)
    for name in sorted(explicit - {"graph"}):
        if not (configured[name] and bound[name]):
            raise _not_configured(name)
    if "graph" in explicit and (
        not strategies
        or not strategies <= _configured_strategies(config)
        or not bound["graph"]
    ):
        raise _not_configured("graph")


def _reranks(request: QueryRequest) -> bool:
    return not request.exact_only


def _require_usable(explicit: frozenset[str], request: QueryRequest) -> None:
    """Refuse an explicitly selected stage this query's input cannot use."""
    for name, usable in (
        ("text", needs_text),
        ("image", needs_visual),
        ("rerank", _reranks),
    ):
        if name in explicit and not usable(request):
            raise TychoraError(
                "INVALID_REQUEST",
                f"Selected {name} channel cannot run for this query",
                reason="channel.input_unusable",
            )


def plan_for(
    request: QueryRequest, config: ResolvedConfig, bindings: QueryBindings
) -> QueryPlan:
    """Narrow the configured plan by the request; enabling anything unconfigured is refused."""
    selection = request.channels or ChannelSelection()
    explicit = _explicit(selection, request)
    strategies = _requested_strategies(selection, config)
    _require_runnable(explicit, strategies, config, bindings)
    _require_usable(explicit, request)
    configured = _configured(config)
    # Coverage names the image channel ``visual``; skipped channels use its names.
    skipped = tuple(
        _COVERAGE_NAMES.get(name, name)
        for name in _SWITCHES
        if name in selection.model_fields_set and not getattr(selection, name)
    )
    if "graph" not in skipped and _graph_off(selection):
        skipped += ("graph",)
    return QueryPlan(
        lexical=selection.lexical,
        text=selection.text and configured["text"],
        image=selection.image and configured["image"],
        graph_strategies=strategies,
        rerank=request.rerank != "off",
        skipped=skipped + (("rerank",) if request.rerank == "off" else ()),
        explicit=explicit,
    )
