import math
import struct

import pytest

from tychora.adapters.voyage.validation import (
    normalize_vector,
    qualify_voyage_capabilities,
)
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.vector_batches import vector_value_sha


@pytest.mark.parametrize("values", [[0.0, 0.0], [math.nan, 1.0], [1.0]])
def test_invalid_embeddings_cannot_be_sealed(values):
    with pytest.raises(TychoraError):
        normalize_vector(values, 2)


def test_embedding_record_preserves_exact_normalized_float32_bytes():
    stored = normalize_vector([3.0, 4.0], 2)

    record = EmbeddingRecord.from_normalized_bytes(
        record_id="record-a",
        input_sha="a" * 64,
        space_id="b" * 64,
        stored=stored,
    )

    assert struct.pack("<2f", *record.values) == stored
    assert record.value_sha == vector_value_sha(stored)


class QualifiedCounter:
    def __init__(self, model):
        self.model = model
        self.tokenizer_id = f"{model}:qualified"

    def count(self, text):
        return len(text)


@pytest.mark.parametrize(
    ("model", "contextualization"),
    [
        ("voyage-context-4", "joint"),
        ("voyage-code-4", "independent"),
        ("voyage-code-3", "independent"),
    ],
)
def test_qualified_models_declare_distinct_contextualization(model, contextualization):
    capabilities = qualify_voyage_capabilities(
        model=model,
        contextualization=contextualization,
        counter=QualifiedCounter(model),
    )

    assert capabilities.dimensions == 1024
    assert capabilities.contextualization == contextualization
    assert capabilities.input_types == frozenset({"document", "query"})


@pytest.mark.parametrize(
    ("model", "contextualization", "counter"),
    [
        ("voyage-context-4", "independent", QualifiedCounter("voyage-context-4")),
        ("voyage-code-4", "joint", QualifiedCounter("voyage-code-4")),
        ("unknown", "independent", QualifiedCounter("unknown")),
        ("voyage-context-4", "joint", None),
        ("voyage-context-4", "joint", QualifiedCounter("voyage-code-4")),
    ],
)
def test_unqualified_model_or_tokenizer_is_unavailable(
    model, contextualization, counter
):
    with pytest.raises(TychoraError) as exc:
        qualify_voyage_capabilities(
            model=model,
            contextualization=contextualization,
            counter=counter,
        )
    assert exc.value.code == "CAPABILITY_MISSING"
