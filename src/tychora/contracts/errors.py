"""Transport-independent, bounded public error envelopes."""

from .base import Contract, Identifier, NonEmptyStr

ERROR_HTTP_STATUS = {
    "INVALID_REQUEST": 400,
    "FORBIDDEN_SCOPE": 403,
    "NOT_FOUND": 404,
    "WORKSPACE_BUSY": 409,
    "CAPABILITY_MISSING": 503,
    "INDEX_INCOMPLETE": 503,
    "PROVIDER_FAILURE": 503,
    "INTEGRITY_ERROR": 409,
    "CANCELLED": 409,
    "DEADLINE_EXCEEDED": 504,
    "SCOPE_CHANGED": 409,
    "PROFILE_INCOMPATIBLE": 409,
    "BUDGET_TOO_SMALL": 400,
    "OUTPUT_BUDGET_EXCEEDED": 400,
    "IDEMPOTENCY_CONFLICT": 409,
    "JOB_INTERRUPTED": 409,
    "ORIGINAL_REMOVED": 410,
    "REPLAY_UNAVAILABLE": 409,
}


class ErrorEnvelope(Contract):
    schema_version: str = "1"
    code: NonEmptyStr
    message: str
    retryable: bool = False
    run_or_job_id: Identifier | None = None
    diagnostic_refs: tuple[Identifier, ...] = ()


class TychoraError(Exception):
    """Accept only a sanitized public message, never an SDK exception/body."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if code not in ERROR_HTTP_STATUS:
            raise ValueError("Unregistered error code")
        if not isinstance(message, str):
            raise TypeError("Public error message must be a string")
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS[self.code]

    @property
    def cli_exit_code(self) -> int:
        if self.code == "CANCELLED":
            return 130
        if self.code in {
            "INVALID_REQUEST",
            "BUDGET_TOO_SMALL",
            "OUTPUT_BUDGET_EXCEEDED",
        }:
            return 2
        return 1

    def to_json(self) -> str:
        message = self.message
        while True:
            encoded = ErrorEnvelope(
                code=self.code, message=message, retryable=self.retryable
            ).model_dump_json()
            if len(encoded.encode("utf-8")) < 8192:
                return encoded
            message = message[: len(message) // 2]
