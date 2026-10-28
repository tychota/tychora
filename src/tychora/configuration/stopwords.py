"""Packaged stop-word lists selected by the lexical profile."""

from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from typing import NamedTuple

from tychora.contracts.identity import digest


class StopwordSet(NamedTuple):
    """The words removed from lexical queries and the digest of their lists."""

    words: frozenset[str]
    digest: str


@lru_cache(maxsize=16)
def _list(language: str, version: str) -> tuple[frozenset[str], str]:
    raw = (
        files("tychora")
        .joinpath(f"resources/stopwords/{language}@{version}.txt")
        .read_bytes()
    )
    words = frozenset(
        line.strip().casefold()
        for line in raw.decode("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    return words, sha256(raw).hexdigest()


@lru_cache(maxsize=16)
def load_stopwords(languages: tuple[str, ...], selection: str) -> StopwordSet:
    """The union of the selected lists; ``none`` selects no list."""
    if selection == "none":
        return StopwordSet(frozenset(), digest("stopwords-v1", "none"))
    version = selection.removeprefix("builtin@")
    lists = [(language, *_list(language, version)) for language in sorted(languages)]
    return StopwordSet(
        frozenset().union(*(words for _, words, _ in lists)),
        digest(
            "stopwords-v1", selection, [[language, sha] for language, _, sha in lists]
        ),
    )
