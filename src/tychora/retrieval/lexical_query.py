"""Bounded, literal FTS5 expressions built from application tokens."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from tychora.configuration.registry import Registry
from tychora.contracts.candidates import ChannelCandidate, ChannelCandidates
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import PassageInput, QueryRequest, QuestionInput
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import ReadTransaction
from tychora.ports.search import RankedOwners

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


def _channel(
    name: Literal["exact", "lexical"],
    view: EligibilityView,
    ranked: RankedOwners,
    epoch: int,
    *,
    scored: bool,
) -> ChannelCandidates:
    """A complete channel; ``truncated`` when a window or hit limit bound it."""

    def candidates(kind: str) -> tuple[ChannelCandidate, ...]:
        return tuple(
            ChannelCandidate(
                key=owner.key,
                rank=owner.rank,
                matching_window_ids=owner.window_ids,
                score=owner.score if scored else None,
            )
            for owner in ranked.owners
            if owner.key[0] == kind
        )

    return ChannelCandidates(
        channel=name,
        snapshot_id=view.snapshot_id,
        policy_epoch=view.policy_epoch,
        source_candidates=candidates("unit"),
        asset_candidates=candidates("asset"),
        matching_window_count=ranked.detail.get("windows", 0),
        distinct_candidate_count=ranked.detail.get(
            "owners", ranked.detail.get("hits", 0)
        ),
        lexical_statistics_epoch=epoch,
        complete=True,
        truncated=ranked.truncated,
    )


def retrieve_exact_lexical(
    tx: ReadTransaction,
    view: EligibilityView,
    request: QueryRequest,
    registry: Registry,
    candidate_limit: int,
    ctx: ExecutionContext,
    *,
    stopwords: frozenset[str] = frozenset(),
) -> tuple[ChannelCandidates, ...]:
    """Run the exact channel, then lexical unless the request is exact-only."""
    ctx.checkpoint()
    if len(request.exact_identifiers) > 16:
        raise TychoraError(
            "INVALID_REQUEST",
            "At most 16 exact identifiers are accepted",
            reason="request.limit_exceeded",
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
    epoch = tx.workspace.lexical_epoch()
    exact = (
        tx.lexical.exact(view, namespaced, min(candidate_limit, 32), ctx)
        if namespaced
        else RankedOwners(owners=())
    )
    exact_channel = _channel("exact", view, exact, epoch, scored=False)
    if request.exact_only or not query:
        return (exact_channel,)
    lexical = tx.lexical.search(view, query, candidate_limit, ctx, stopwords=stopwords)
    return (exact_channel, _channel("lexical", view, lexical, epoch, scored=True))


def _phrases(text: str) -> list[tuple[str, ...]]:
    """At most four quoted phrases of at most eight terms, 32 terms in total."""
    phrases: list[tuple[str, ...]] = []
    used = 0
    for match in _PHRASE.finditer(text):
        words = tuple(token.casefold() for token in _TOKEN.findall(match.group(1)))
        if words and len(words) <= 8 and used + len(words) <= 32:
            phrases.append(words)
            used += len(words)
            if len(phrases) == 4:
                break
    return phrases


def _loose_terms(text: str, stopwords: frozenset[str], room: int) -> list[str]:
    """Distinct loose terms, most frequent first, without the stop words."""
    excluded = {word.casefold() for word in stopwords}
    tokens = [match.group().casefold() for match in _TOKEN.finditer(text)]
    tokens = [token for token in tokens if token not in excluded]
    frequency = Counter(tokens)
    first = {token: tokens.index(token) for token in frequency}
    return sorted(
        frequency, key=lambda token: (-frequency[token], -len(token), first[token])
    )[:room]


def lexical_expression(text: str, stopwords: frozenset[str]) -> str | None:
    """Return a literal OR query; user text never supplies FTS5 syntax.

    Stop words are dropped from loose terms; a query made only of stop words
    keeps its terms rather than becoming empty.
    """
    phrases = _phrases(text)
    room = 32 - sum(len(words) for words in phrases)
    terms = _loose_terms(text, stopwords, room)
    if not terms and not phrases:
        terms = _loose_terms(text, frozenset(), room)
    clauses = ['"' + " ".join(words).replace('"', '""') + '"' for words in phrases]
    clauses.extend('"' + token.replace('"', '""') + '"' for token in terms)
    return " OR ".join(clauses) or None
