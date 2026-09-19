"""Qualified provider token counting boundary."""

from typing import Protocol


class TokenCounter(Protocol):
    model: str
    tokenizer_id: str

    def count(self, text: str) -> int: ...
