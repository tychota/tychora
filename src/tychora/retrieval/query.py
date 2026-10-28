"""Pure whole-query identities and input guards."""

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.requests import PassageInput, QueryRequest, QuestionInput
from tychora.ports.tokens import TokenCounter


def result_cache_key(
    request: QueryRequest,
    eligibility: tuple[str | int, ...],
    lexical_statistics_epoch: int,
    config_sha: str,
    provider_fingerprint: str = "",
    projection_fingerprint: str = "",
) -> str:
    """Bind result reuse to every requested, rendered and indexed dimension.

    ``eligibility`` is the eligibility cache key of the view the result was
    admitted through: snapshot, policy epoch, scope, policy and request.
    """
    return digest(
        "query-result-cache-v2",
        list(eligibility),
        lexical_statistics_epoch,
        config_sha,
        provider_fingerprint,
        projection_fingerprint,
        request.model_dump(mode="json"),
    )


def validate_query_text(request: QueryRequest, counter: TokenCounter) -> None:
    if (
        isinstance(request.input, (QuestionInput, PassageInput))
        and counter.count(request.input.text) > 2000
    ):
        raise TychoraError(
            "INVALID_REQUEST",
            "Query exceeds 2,000 provider tokens; shorten or split it.",
        )
