import os
from hashlib import sha256
from pathlib import Path

import pytest

from tychora.application.setup import load_voyage_tokenizer
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.identity import unit_id


class CharacterCounter:
    model = "synthetic-codepoint-counter"
    tokenizer_id = "synthetic-codepoint-counter@1"

    def count(self, text: str) -> int:
        return len(text)


class VoyageCounter:
    model = "voyage-context-4"
    tokenizer_id = "voyage-context-4:8ca946072a18e398cd61f2ad0243b56d0350b1db"

    def __init__(self, tokenizer: object) -> None:
        self._tokenizer = tokenizer

    def count(self, text: str) -> int:
        if not text:
            return 0
        encodings = self._tokenizer.encode_batch(  # type: ignore[attr-defined]
            [text], add_special_tokens=False
        )
        return sum(len(encoding.ids) for encoding in encodings)


@pytest.fixture
def counter() -> CharacterCounter:
    return CharacterCounter()


@pytest.fixture
def config_factory(tmp_path):
    workspace = tmp_path / "workspace.toml"

    def make(
        *,
        model: str = "voyage-context-4",
        dimensions: int = 1024,
        representation_version: str = "1",
        max_group_tokens: int = 24_000,
        max_request_tokens: int = 28_000,
    ):
        workspace.write_text(
            f"""schema_version = 1
[profiles.test]
builtin = "documents@1"
[defaults]
profile = "test"
[configuration]
offline = false
[configuration.providers.text]
provider = "voyage"
model = "{model}"
dimensions = {dimensions}
representation_version = "{representation_version}"
max_group_tokens = {max_group_tokens}
max_request_tokens = {max_request_tokens}
[configuration.index.text]
backend = "chroma"
"""
        )
        return resolve_config(workspace, "test", {})

    return make


@pytest.fixture(scope="session")
def voyage_counter() -> VoyageCounter:
    root = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if root is None:
        pytest.skip("requires the explicitly provisioned verified model cache")
    return VoyageCounter(load_voyage_tokenizer(Path(root), "voyage-context-4"))


@pytest.fixture
def long_unit(voyage_counter: VoyageCounter) -> EvidenceUnit:
    marker = "\nEND_MARKER"
    low, high = 1, 12_000
    while low < high:
        middle = (low + high) // 2
        if voyage_counter.count(" token" * middle + marker) < 10_000:
            low = middle + 1
        else:
            high = middle
    text = " token" * low + marker
    assert voyage_counter.count(text) == 10_000
    text_sha = sha256(text.encode()).hexdigest()
    extraction_id = "extraction-long"
    return EvidenceUnit(
        unit_id=unit_id(extraction_id, "long", 0, text_sha),
        extraction_id=extraction_id,
        parent_id=None,
        ordinal=0,
        unit_type="paragraph",
        native_ref="long",
        boundary_key="section",
        text=text,
        text_sha=text_sha,
        locators=(),
        attributes=FrozenJsonMap(),
    )
