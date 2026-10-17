"""Transport-independent, bounded public error envelopes."""

from .base import Contract, Identifier, NonEmptyStr
from .error_reasons import REASONS, reason_for

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
    "LIMIT_EXCEEDED": 413,
    "IDEMPOTENCY_CONFLICT": 409,
    "JOB_INTERRUPTED": 409,
    "ORIGINAL_REMOVED": 410,
    "REPLAY_UNAVAILABLE": 409,
    "NOT_ACCEPTABLE": 406,
}


class ErrorEnvelope(Contract):
    """Public, versioned shape of one error."""

    schema_version: str = "2"
    code: NonEmptyStr
    message: str
    retryable: bool = False
    reason: str | None = None
    run_or_job_id: Identifier | None = None
    diagnostic_refs: tuple[Identifier, ...] = ()


class TychoraError(Exception):
    """Accept only a sanitized public message, never an SDK exception/body."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        reason: str | None = None,
        diagnostic_id: str | None = None,
    ) -> None:
        if code not in ERROR_HTTP_STATUS:
            raise ValueError("Unregistered error code")
        if not isinstance(message, str):
            raise TypeError("Public error message must be a string")
        registered = reason_for(code, reason) if reason is not None else None
        if (
            registered is not None
            and retryable is not None
            and retryable != registered.retryable
        ):
            raise ValueError("Retryable flag differs from the registered reason")
        self.code = code
        self.message = message
        self.reason = reason
        self.retryable = (
            registered.retryable if registered is not None else bool(retryable)
        )
        self.diagnostic_id = diagnostic_id
        super().__init__(message)

    @property
    def http_status(self) -> int:
        if self.reason is not None:
            override = REASONS[self.reason].http_status
            if override is not None:
                return override
        return ERROR_HTTP_STATUS[self.code]

    @property
    def cli_exit_code(self) -> int:
        if self.code == "CANCELLED":
            return 130
        if self.code in {
            "INVALID_REQUEST",
            "BUDGET_TOO_SMALL",
            "OUTPUT_BUDGET_EXCEEDED",
            "LIMIT_EXCEEDED",
        }:
            return 2
        return 1

    def envelope(self) -> ErrorEnvelope:
        """Return the public envelope of this error."""
        return ErrorEnvelope(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            reason=self.reason,
            diagnostic_refs=(self.diagnostic_id,) if self.diagnostic_id else (),
        )

    def to_json(self) -> str:
        """Serialize the envelope, halving the message until it fits in 8 KiB."""
        envelope = self.envelope()
        while True:
            encoded = envelope.model_dump_json()
            if len(encoded.encode("utf-8")) < 8192:
                return encoded
            envelope = envelope.model_copy(
                update={"message": envelope.message[: len(envelope.message) // 2]}
            )
