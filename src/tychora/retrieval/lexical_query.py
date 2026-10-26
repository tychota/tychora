"""Bounded, literal FTS5 expressions built from application tokens."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import cast

from tychora.configuration.registry import Registry
from tychora.contracts.candidates import ChannelCandidates
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.requests import PassageInput, QueryRequest, QuestionInput
from tychora.ports.corpus import CorpusStore
from tychora.ports.execution import ExecutionContext
from tychora.ports.lexical import LexicalIndex

_TOKEN = re.compile(r"\w+", re.UNICODE)
_PHRASE = re.compile(r'"([^"]+)"')
_IDENTIFIER_NAMESPACES = frozenset(
    {
        "provision",
        "rule",
        "code.definition_key",
        "code.qualified_name",
        "code.short_name",
    }
)


def normalize_identifier(namespaced: str) -> tuple[str, str]:
    """Parse a registered namespace without changing its stored case-sensitive value."""
    namespace, separator, value = namespaced.partition(":")
    if not separator or namespace not in _IDENTIFIER_NAMESPACES or not value:
        raise ValueError("Identifier requires a registered namespace and value")
    return namespace, value


def extract_identifiers(text: str, registry: Registry) -> tuple[str, ...]:
    """Route conventional free-text IDs through registered normalizer families."""
    matches: list[tuple[int, str]] = []
    kinds = [cast(Mapping[str, object], kind) for kind in registry.kinds.values()]
    prefixes = {
        str(prefix)
        for kind in kinds
        if kind.get("normalizer") == "rule_records@1"
        for prefix in cast(
            Sequence[str],
            cast(Mapping[str, object], kind.get("normalizer_options", {})).get(
                "identifier_prefixes", ("STD-",)
            ),
        )
    }
    for prefix in sorted(prefixes):
        pattern = re.compile(rf"(?<!\w){re.escape(prefix)}[A-Za-z0-9][A-Za-z0-9._-]*")
        matches.extend(
            (hit.start(), f"rule:{hit.group()}") for hit in pattern.finditer(text)
        )
    if any(kind.get("normalizer") == "numbered_provisions@1" for kind in kinds):
        for hit in re.finditer(
            r"\b(?:Article|Art\.)\s+([0-9]+(?:[.-][0-9]+)*)", text, re.IGNORECASE
        ):
            matches.append((hit.start(), f"provision:{hit.group(1)}"))
    if any(kind.get("normalizer") == "code_symbols@1" for kind in kinds):
        for hit in re.finditer(r"(?<!\w)[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", text):
            matches.append((hit.start(), f"code.qualified_name:{hit.group()}"))
        for hit in re.finditer(r"`([A-Za-z_]\w*)`", text):
            matches.append((hit.start(), f"code.short_name:{hit.group(1)}"))
    result: list[str] = []
    for _, identifier in sorted(matches, key=lambda item: item[0])[:32]:
        if identifier not in result:
            result.append(identifier)
        if len(result) == 16:
            break
    return tuple(result)


def retrieve_exact_lexical(
    store: CorpusStore,
    lexical: LexicalIndex,
    view: LegacyEligibilityView,
    request: QueryRequest,
    registry: Registry,
    candidate_limit: int,
    ctx: ExecutionContext,
) -> tuple[ChannelCandidates, ...]:
    """Dispatch exact and lexical channels while preserving exact-only isolation."""
    ctx.checkpoint()
    if len(request.exact_identifiers) > 16:
        raise TychoraError(
            "INVALID_REQUEST", "At most 16 exact identifiers are accepted"
        )
    query = (
        request.input.text
        if isinstance(request.input, (QuestionInput, PassageInput))
        else ""
    )
    namespaced = tuple(
        dict.fromkeys(
            (*request.exact_identifiers, *extract_identifiers(query, registry))
        )
    )[:16]
    exact = store.lookup_identifiers(namespaced, view, min(candidate_limit, 32))
    if request.exact_only or not query:
        return (exact,)
    return (exact, lexical.search(view, query, candidate_limit, ctx))


def lexical_expression(text: str, stopwords: frozenset[str]) -> str | None:
    """Return a literal OR query; user text never supplies FTS5 syntax."""
    excluded = {word.casefold() for word in stopwords}
    tokens = [match.group().casefold() for match in _TOKEN.finditer(text)]
    tokens = [token for token in tokens if token not in excluded]

    phrases: list[tuple[str, ...]] = []
    used = 0
    for match in _PHRASE.finditer(text):
        words = tuple(token.casefold() for token in _TOKEN.findall(match.group(1)))
        if words and len(words) <= 8 and used + len(words) <= 32:
            phrases.append(words)
            used += len(words)
            if len(phrases) == 4:
                break

    frequency = Counter(tokens)
    first = {token: tokens.index(token) for token in frequency}
    selected = sorted(
        frequency, key=lambda token: (-frequency[token], -len(token), first[token])
    )[: 32 - used]
    clauses = ['"' + " ".join(words).replace('"', '""') + '"' for words in phrases]
    clauses.extend('"' + token.replace('"', '""') + '"' for token in selected)
    return " OR ".join(clauses) or None
