"""Owner-local composition of configured, qualified runtime adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING

from tychora.adapters.ladybug.traversal import resolve_graph_traversal_policy
from tychora.adapters.voyage.query import (
    VoyageTextQueryProvider,
    VoyageVisualQueryProvider,
)
from tychora.adapters.voyage.request import HttpxVoyageTransport
from tychora.adapters.voyage.rerank import VoyageReranker
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.text import VoyageTextEmbedder
from tychora.adapters.voyage.validation import (
    qualify_voyage_capabilities,
    qualify_voyage_rerank,
    qualify_voyage_visual_capabilities,
)
from tychora.adapters.voyage.visual import VoyageVisualEmbedder
from tychora.api import ProviderBindings
from tychora.application.reranking import OwnerRerankAccess
from tychora.application.setup import (
    SetupService,
    load_voyage_multimodal_accounting,
    load_voyage_tokenizer,
)
from tychora.application.visual import OwnerVisualAccess
from tychora.configuration.registry import Registry
from tychora.contracts.embeddings import VisualEmbeddingLimits
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.indexing import FamilySpec
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches

if TYPE_CHECKING:
    from tokenizers import Tokenizer

    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.models import ResolvedConfig


class _VerifiedVoyageCounter:
    """Expose the pinned identity while loading verified tokenizer bytes on use."""

    def __init__(self, cache_root: Path, model: str) -> None:
        self.cache_root = cache_root
        self.model = model
        assets = SetupService(cache_root, offline=True).plan((model,)).assets
        tokenizer = next(
            (asset for asset in assets if Path(asset.path).name == "tokenizer.json"),
            None,
        )
        if tokenizer is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured tokenizer has no pinned asset."
            )
        self.tokenizer_id = f"{model}:{tokenizer.revision}"
        self._tokenizer: Tokenizer | None = None

    def count(self, text: str) -> int:
        if self._tokenizer is None:
            try:
                self._tokenizer = load_voyage_tokenizer(self.cache_root, self.model)
            except ImportError:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Voyage tokenizer package is unavailable; install the provider extra.",
                ) from None
        if self.model == "voyage-multimodal-3.5":
            load_voyage_multimodal_accounting(self.cache_root)
        return len(self._tokenizer.encode(text).ids)


def _credential(ref: str | None, resolver: Callable[[str], str] | None) -> str:
    if ref is None:
        raise TychoraError(
            "CAPABILITY_MISSING", "Configured provider needs a credential reference."
        )
    scheme, name = ref.split(":", 1)
    if scheme == "env":
        value = os.environ.get(name)
    elif resolver is not None:
        try:
            value = resolver(name)
        except Exception:  # noqa: BLE001 - never leak resolver details into public errors
            value = None
    else:
        value = None
    if not isinstance(value, str) or not value:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Configured provider credential is unavailable; set the referenced environment variable or supply a secret resolver.",
        )
    return value


def _runtime(
    owner: WorkspaceOwner,
    ref: str | None,
    resolver: Callable[[str], str] | None,
    caches: QueryCaches,
) -> VoyageRuntime:
    runtime = VoyageRuntime(
        lambda: HttpxVoyageTransport(api_key=_credential(ref, resolver)),
        workspace_id=owner.workspace_id,
        credential_binding_id=token_hex(16),
        query_caches=caches,
    )
    runtime.service_owned = True
    return runtime


def _visual_limits(cache_root: Path) -> VisualEmbeddingLimits:
    assets = (
        SetupService(cache_root, offline=True).plan(("voyage-multimodal-3.5",)).assets
    )
    tokenizer = next(
        asset for asset in assets if Path(asset.path).name == "tokenizer.json"
    )
    config = next(
        asset for asset in assets if Path(asset.path).name == "client_config.json"
    )
    return VisualEmbeddingLimits(
        accounting_version="voyage-multimodal-accounting-v1",
        model_revision=config.revision,
        tokenizer_sha256=tokenizer.sha256,
        client_config_sha256=config.sha256,
        accounting_image_pixels_min=50_000,
        accounting_image_pixels_max=2_000_000,
        image_pixels_per_token=560,
        source_pixels_max=16_000_000,
        image_encoded_bytes_max=20_000_000,
        input_tokens_max=32_000,
        request_assets_max=32,
        request_tokens_max=100_000,
        request_wire_bytes_max=64 * 1024 * 1024,
        decoded_image_bytes_max=48_000_000,
    )


def _native_index(owner: WorkspaceOwner, channel: str):
    attr = f"_configured_{channel}_index"
    index = getattr(owner, attr, None)
    if index is None:
        try:
            from tychora.adapters.chroma.index import ChromaVectorIndex
        except ImportError:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured Chroma index requires the native index extra.",
            ) from None
        index = ChromaVectorIndex(owner.path / "indexes" / channel)
        setattr(owner, attr, index)
    return index


def _graph_store(owner: WorkspaceOwner, config: ResolvedConfig):
    graph = getattr(owner, "_configured_graph_store", None)
    if graph is None:
        try:
            from tychora.adapters.ladybug.generation import LadybugGraphStore
        except ImportError:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured Ladybug graph requires the native graph extra.",
            ) from None
        policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
        graph = LadybugGraphStore(
            owner.path / "indexes" / "graph", traversal_policy=policy
        )
        owner.register_close(graph.close)
        owner._configured_graph_store = graph
    else:
        policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
        graph.set_traversal_policy(policy)
    return graph


def compose_default_bindings(
    owner: WorkspaceOwner,
    config: ResolvedConfig,
    *,
    credential_resolver: Callable[[str], str] | None = None,
) -> tuple[ProviderBindings, tuple[VoyageRuntime, ...]]:
    """Create configured ports after owner acquisition without remote clients."""
    with ExitStack() as cleanup:
        cache_root = Path(os.environ.get("TYCHORA_MODEL_CACHE", owner.path / "models"))
        caches = QueryCaches()
        runtimes: list[VoyageRuntime] = []
        text_embedder = text_query = text_counter = None
        visual_embedder = visual_query = None
        reranker = rerank_counter = None
        text_index = (
            _native_index(owner, "text")
            if config.values.index.text.backend == "chroma"
            else None
        )
        visual_index = (
            _native_index(owner, "image")
            if config.values.index.image.backend == "chroma"
            else None
        )
        graph = (
            _graph_store(owner, config)
            if config.values.index.graph == "ladybug"
            else None
        )
        if owner.store.active_snapshot_id() is not None:
            view = owner.store.resolve_snapshot(None)
            for slot in view.slots:
                index = (
                    text_index
                    if slot.slot == "text"
                    else visual_index
                    if slot.slot == "image"
                    else None
                )
                if index is not None:
                    spec = FamilySpec.model_validate_json(
                        canonical_bytes(slot.family.config_json["spec"])
                    )
                    index.create_family(spec)
                    owner.store.vector_indexes[spec.family_id] = index
                elif graph is not None and slot.slot == "graph":
                    for batch in slot.batches:
                        if batch.graph_manifest is not None:
                            owner.store.graph_indexes[
                                batch.graph_manifest.spec.generation_id
                            ] = graph
        text_settings = config.values.providers.text
        if not config.values.offline and text_settings.provider == "voyage":
            if text_settings.model not in {
                "voyage-context-4",
                "voyage-code-4",
                "voyage-code-3",
            }:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Configured text model is unsupported."
                )
            text_counter = _VerifiedVoyageCounter(cache_root, text_settings.model)
            capabilities = qualify_voyage_capabilities(
                model=text_settings.model,
                contextualization="joint"
                if text_settings.model == "voyage-context-4"
                else "independent",
                counter=text_counter,
            )
            if capabilities.dimensions != text_settings.dimensions:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Configured text dimensions are unsupported."
                )
            runtime = _runtime(
                owner, text_settings.credential_ref, credential_resolver, caches
            )
            runtimes.append(runtime)
            cleanup.callback(runtime.close)

            def authorize(payload_sha: str, dependencies: frozenset[str], ctx):
                owner.authorize_current_document("voyage", dependencies, ctx)

            text_embedder = VoyageTextEmbedder(
                capabilities=capabilities,
                counter=text_counter,
                runtime=runtime,
                authorize_document=authorize,
            )
            text_query = VoyageTextQueryProvider(text_embedder)
        image_settings = config.values.providers.image
        if not config.values.offline and image_settings.provider == "voyage":
            if (
                image_settings.model != "voyage-multimodal-3.5"
                or image_settings.dimensions != 1024
            ):
                raise TychoraError(
                    "CAPABILITY_MISSING", "Configured visual model is unsupported."
                )
            visual_counter = _VerifiedVoyageCounter(cache_root, image_settings.model)
            visual_caps = qualify_voyage_visual_capabilities(
                counter=visual_counter, limits=_visual_limits(cache_root)
            )
            visual_runtime = _runtime(
                owner, image_settings.credential_ref, credential_resolver, caches
            )
            runtimes.append(visual_runtime)
            cleanup.callback(visual_runtime.close)
            epoch = owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()[0]
            access = OwnerVisualAccess(owner, owner.scope, epoch)
            access.read_scope((), ExecutionContext())
            visual_embedder = VoyageVisualEmbedder(
                capabilities=visual_caps,
                counter=visual_counter,
                runtime=visual_runtime,
                access=access,
            )
            visual_query = VoyageVisualQueryProvider(visual_embedder)
        rerank_settings = config.values.providers.rerank
        if not config.values.offline and rerank_settings.provider == "voyage":
            if rerank_settings.model != "rerank-2.5":
                raise TychoraError(
                    "CAPABILITY_MISSING", "Configured reranker model is unsupported."
                )
            rerank_counter = _VerifiedVoyageCounter(cache_root, rerank_settings.model)
            rerank_caps = qualify_voyage_rerank(
                rerank_settings.model, rerank_counter.tokenizer_id
            )
            rerank_runtime = _runtime(
                owner, rerank_settings.credential_ref, credential_resolver, caches
            )
            runtimes.append(rerank_runtime)
            cleanup.callback(rerank_runtime.close)
            epoch = owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()[0]
            access = OwnerRerankAccess(owner, owner.scope, epoch)
            reranker = VoyageReranker(
                capabilities=rerank_caps,
                counter=rerank_counter,
                runtime=rerank_runtime,
                access=access,
            )
        cleanup.pop_all()
        return (
            ProviderBindings(
                text_embedder=text_embedder,
                text_counter=text_counter,
                text_query=text_query,
                text_index=text_index,
                visual_embedder=visual_embedder,
                visual_query=visual_query,
                visual_index=visual_index,
                reranker=reranker,
                rerank_counter=rerank_counter,
                graph=graph,
                graph_factory=(lambda build: graph.prepare_build(build))
                if graph is not None
                else None,
                caches=caches,
                vision_credential=(
                    lambda: _credential(
                        config.values.providers.vision.credential_ref,
                        credential_resolver,
                    )
                )
                if not config.values.offline
                and config.values.providers.vision.provider == "anthropic"
                else None,
            ),
            tuple(runtimes),
        )
