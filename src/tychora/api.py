"""Primary local Python API composition and typed provider bindings."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from tychora.application.bridge import ClientBridge
from tychora.application.owner import WorkspaceOwner
from tychora.application.services import Services, build_services
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.segments import OutputTokenCounter

if TYPE_CHECKING:
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.contracts.graph import GraphBuildInput
    from tychora.ports.embeddings import OwnerBoundVisualEmbedder, TextEmbedder
    from tychora.ports.graph import GraphStore
    from tychora.ports.query import TextQueryProvider, VisualQueryProvider
    from tychora.ports.tokens import TokenCounter
    from tychora.ports.vectors import VectorIndex
    from tychora.retrieval.cache import QueryCaches

DEFAULT_COLLECTION_ID = UUID("00000000-0000-4000-8000-000000000001")
_DEFAULT_CONFIG = (
    'schema_version = 1\n[profiles.local]\nbuiltin = "documents@1"\n'
    '[defaults]\nprofile = "local"\n'
)


@dataclass(frozen=True)
class LocalTextBinding:
    """Trusted explicit local document embedder binding."""

    embedder: TextEmbedder


@dataclass(frozen=True)
class ProviderBindings:
    """Explicit qualified ports; no provider is constructed by open()."""

    text_embedder: TextEmbedder | None = None
    local_text: LocalTextBinding | None = None
    text_counter: TokenCounter | None = None
    text_query: TextQueryProvider | None = None
    text_index: VectorIndex | None = None
    visual_embedder: OwnerBoundVisualEmbedder | None = None
    visual_query: VisualQueryProvider | None = None
    visual_index: VectorIndex | None = None
    reranker: VoyageReranker | None = None
    rerank_counter: TokenCounter | None = None
    graph: GraphStore | None = None
    graph_factory: Callable[[GraphBuildInput], GraphStore] | None = None
    caches: QueryCaches | None = None


class _LazyOutputCounter:
    model = "cl100k_base"
    tokenizer_id = "cl100k_base"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded: OutputTokenCounter | None = None

    def count(self, text: str) -> int:
        if self._loaded is None:
            self._loaded = OutputTokenCounter(self.path)
        return self._loaded.count(text)


class LocalWorkspace:
    def __init__(
        self,
        path: str | Path,
        *,
        scope: RequestScope | None,
        config: ResolvedConfig | None,
        providers: ProviderBindings | None,
        token_counter: TokenCounter | None,
    ) -> None:
        root = Path(path)
        existed = root.exists()
        if not existed:
            root.mkdir(parents=True)
        settings = root / "workspace.toml"
        if config is None:
            if not settings.exists():
                if existed and any(root.iterdir()):
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "Existing workspace requires an explicit configuration.",
                    )
                settings.write_text(_DEFAULT_CONFIG)
            config = resolve_config(settings, "local", {})
        if scope is None:
            scope = RequestScope(
                principal_id="local-owner",
                collection_ids=frozenset({str(DEFAULT_COLLECTION_ID)}),
                access_ceiling=0,
                uses=frozenset({"compose", "audit", "lookup"}),
                capabilities=frozenset({"read", "write", "manage"}),
            )
        self.scope = scope
        self.config = config
        self._stack = ExitStack()
        self.bridge = ClientBridge()
        self.closed = False
        try:
            self.owner = self._stack.enter_context(
                WorkspaceOwner.open(root, config, scope)
            )
            if str(DEFAULT_COLLECTION_ID) in scope.collection_ids:
                self.bridge.call(
                    self.owner.run_blocking(
                        self._ensure_default_collection, ExecutionContext()
                    )
                )
            counter = token_counter or _LazyOutputCounter(
                Path(os.environ.get("TYCHORA_MODEL_CACHE", root / "models"))
            )
            self.services: Services = self.bridge.call(
                self.owner.run_blocking(
                    lambda: build_services(
                        self.owner, config, providers or ProviderBindings(), counter
                    ),
                    ExecutionContext(),
                )
            )
            startup_bridge = self.bridge
            self.bridge = self.services.source_jobs.bridge
            startup_bridge.close()
        except BaseException:
            if hasattr(self, "services"):
                self.services.source_jobs.close()
            else:
                self.bridge.close()
            self._stack.close()
            raise

    def _ensure_default_collection(self) -> None:
        registered = self.owner.db.execute(
            "SELECT collection_id FROM collections WHERE workspace_id=? AND slug='default'",
            (self.owner.workspace_id,),
        ).fetchone()
        fixed = self.owner.db.execute(
            "SELECT slug FROM collections WHERE workspace_id=? AND collection_id=?",
            (self.owner.workspace_id, str(DEFAULT_COLLECTION_ID)),
        ).fetchone()
        if (registered is not None and registered[0] != str(DEFAULT_COLLECTION_ID)) or (
            fixed is not None and fixed[0] != "default"
        ):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Existing default collection identity conflicts with this workspace.",
            )
        if registered is not None:
            return
        self.owner.db.execute(
            "INSERT INTO collections "
            "(collection_id,workspace_id,slug,title,access_floor,policy_json) "
            'VALUES (?,?,?, ?,0,\'{"schema_version":1,"upload_providers":[]}\')',
            (str(DEFAULT_COLLECTION_ID), self.owner.workspace_id, "default", "Default"),
        )
        self.owner.db.commit()

    @property
    def default_collection_id(self) -> UUID:
        self.require_open()
        if str(DEFAULT_COLLECTION_ID) not in self.scope.collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Default collection is outside the grant."
            )
        return DEFAULT_COLLECTION_ID

    def require_open(self) -> None:
        if self.closed:
            raise RuntimeError("Tychora client is closed")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.services.source_jobs.close()
            finally:
                self._stack.close()

    def parse_source(self, source: SourceInput | Mapping) -> SourceInput:
        if isinstance(source, SourceInput):
            return source
        normalized = dict(source)
        if normalized.get("collection_id") == "default":
            normalized["collection_id"] = str(self.default_collection_id)
        return SourceInput.from_mapping(normalized)
