"""Explicitly authorized live shape qualification; never a quality benchmark."""

import json
import math
import os

import pytest
from tokenizers import Tokenizer

from tests.helpers.assets import asset_path
from tychora.adapters.voyage.text import (
    HttpxVoyageTransport,
    VoyageRuntime,
    VoyageTextEmbedder,
)
from tychora.adapters.voyage.validation import qualify_voyage_capabilities
from tychora.application.setup import load_voyage_tokenizer
from tychora.contracts.identity import digest
from tychora.contracts.indexing import ContextGroup
from tychora.ports.execution import ExecutionContext

pytestmark = pytest.mark.live


class ProvisionedCounter:
    def __init__(self, model: str, tokenizer: Tokenizer) -> None:
        self.model = model
        self.tokenizer_id = f"{model}:live-qualified"
        self._tokenizer = tokenizer

    def count(self, text: str) -> int:
        encoded = self._tokenizer.encode_batch([text], add_special_tokens=False)
        return len(encoded[0].ids)


def _live_enabled() -> bool:
    return (
        os.environ.get("TYCHORA_RUN_VOYAGE_LIVE") == "1"
        and bool(os.environ.get("VOYAGE_API_KEY"))
        and bool(os.environ.get("TYCHORA_TEST_MODEL_CACHE"))
    )


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.skipif(
    not _live_enabled(),
    reason="requires explicit live opt-in, credential, and provisioned tokenizer cache",
)
@pytest.mark.parametrize(
    ("model", "contextualization"),
    [
        ("voyage-context-4", "joint"),
        ("voyage-code-4", "independent"),
        ("voyage-code-3", "independent"),
    ],
)
def test_live_synthetic_document_shape_and_capability_receipt(
    tmp_path, model, contextualization
):
    cache = asset_path("TYCHORA_TEST_MODEL_CACHE")
    counter = ProvisionedCounter(model, load_voyage_tokenizer(cache, model))
    capabilities = qualify_voyage_capabilities(
        model=model,
        contextualization=contextualization,
        counter=counter,
    )
    capsule = '{"kind":"synthetic","path":"src/example.py"}'
    window = "def add(left, right):\n    return left + right"
    input_sha = digest(
        "embedding-input-v1",
        (capsule, window),
        "voyage",
        model,
        1024,
        "float32",
        "1",
        contextualization,
        capabilities.options,
    )
    group = ContextGroup(
        group_id=digest(
            "context-group-input-v1",
            "synthetic-extraction",
            "synthetic-context",
            ("synthetic-window",),
            input_sha,
        ),
        context_group_key="synthetic-context",
        extraction_id="synthetic-extraction",
        revision_id="synthetic-revision",
        capsule=capsule,
        input_texts=(capsule, window),
        window_ids=("synthetic-window",),
        window_input_shas=(
            (input_sha,)
            if contextualization == "joint"
            else (digest("synthetic-window-input", window),)
        ),
        input_sha=input_sha,
        dependency_ids=frozenset({"synthetic-revision", "synthetic-unit"}),
        token_count=counter.count(capsule) + counter.count(window),
        tokenizer_id=counter.tokenizer_id,
        provider="voyage",
        model=model,
        dimensions=1024,
        representation_version="1",
        contextualization=contextualization,
        options=capabilities.options,
    )
    runtime = VoyageRuntime(
        lambda: HttpxVoyageTransport(api_key=os.environ["VOYAGE_API_KEY"]),
        workspace_id="live-synthetic-workspace",
        credential_binding_id="live-explicit-credential",
    )
    try:
        embedder = VoyageTextEmbedder(
            capabilities=capabilities,
            counter=counter,
            runtime=runtime,
            authorize_document=lambda payload_sha, dependencies, ctx: None,
        )
        (record,) = embedder.embed_groups((group,), ExecutionContext())
    finally:
        runtime.close()
    assert len(record.values) == 1024
    assert math.isclose(
        math.sqrt(sum(value * value for value in record.values)),
        1.0,
        rel_tol=2e-6,
        abs_tol=2e-6,
    )
    receipt = {
        "schema_version": "1",
        "provider": capabilities.provider,
        "model": capabilities.model,
        "dimensions": capabilities.dimensions,
        "contextualization": capabilities.contextualization,
        "tokenizer_id": capabilities.tokenizer_id,
        "space_id": capabilities.space_id,
        "input_sha": record.input_sha,
        "value_sha": record.value_sha,
    }
    (tmp_path / f"{model}-capability.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    )
