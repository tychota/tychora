"""Portable standard factory/dispatch gate, with no live model request."""

import os

import pytest

from tests.providers.test_reranker import (
    test_standard_factory_dispatches_with_verified_local_tokenizer as dispatch_standard,
)


@pytest.mark.qualification
@pytest.mark.asyncio
async def test_selected_reranker_factory_gate(tmp_path):
    configured = bool(os.environ.get("TYCHORA_TEST_MODEL_CACHE"))
    if not configured:
        pytest.fail("Verified model cache is required")
    await dispatch_standard(tmp_path)
