"""Qualified provider token counting boundary."""

from typing import Protocol, runtime_checkable


class TokenCounter(Protocol):
    model: str
    tokenizer_id: str

    def count(self, text: str) -> int: ...


@runtime_checkable
class OffsetTokenCounter(TokenCounter, Protocol):
    """A token counter that also reports where each token sits in the text.

    ``count_with_offsets(text)`` returns one ``(char_start, char_end)`` span per
    counted token, in text order, without the fixed tokens ``count("")`` adds.
    For every ``a < b`` the spans overlapping ``[a, b)`` plus ``count("")`` must
    equal ``count(text[a:b])``; windows are then identical to counting each
    candidate range separately.
    """

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """One ``(char_start, char_end)`` span per counted token, in text order."""
        ...
