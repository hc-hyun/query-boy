from typing import Any

_PUBLIC_REJECTED_CONSTRUCTS = frozenset(
    {
        "BETWEEN",
        "BETWEEN SYMMETRIC",
        "NOT BETWEEN",
        "NOT BETWEEN SYMMETRIC",
        "OPERATOR",
    }
)

_QUERY_INVALID_MESSAGES = {
    "QUERY_DIVISION_BY_ZERO": (
        "The query can divide by zero. Guard the denominator with NULLIF or exclude zero values, "
        "then retry once."
    ),
    "QUERY_FUNCTION_SIGNATURE_MISMATCH": (
        "A function or operator call has unsupported argument types or count. Use a supported "
        "built-in signature and only advertised casts, then retry once."
    ),
    "QUERY_INVALID_CAST": (
        "The query casts a value to an incompatible type. Use an advertised compatible cast or "
        "filter invalid values, then retry once."
    ),
    "QUERY_INVALID_FUNCTION_ARGUMENT": (
        "A function argument has an invalid value. Correct the argument while preserving the "
        "requested calculation, then retry once."
    ),
    "QUERY_INVALID_FUNCTION_USAGE": (
        "An aggregate or window function is used in an unsupported form. Use its supported call "
        "form, then retry once."
    ),
    "QUERY_INVALID_LIMIT": (
        "LIMIT and OFFSET must use non-negative values. Correct them and retry once."
    ),
    "QUERY_INVALID_REGULAR_EXPRESSION": (
        "The regular expression is invalid. Correct its pattern or flags, then retry once."
    ),
    "QUERY_NUMERIC_VALUE_OUT_OF_RANGE": (
        "A numeric value is outside its supported range. For percentile fractions use a value "
        "from 0 through 1, then retry once."
    ),
    "QUERY_UNDEFINED_COLUMN": (
        "The query references a column PostgreSQL cannot resolve. Use a returned column sql_name "
        "or an alias declared in the query, then retry once."
    ),
}


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class SourceNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(404, "SOURCE_NOT_FOUND", "The requested source was not found.")


class MetadataUnavailableError(AppError):
    def __init__(self, details: Any | None = None) -> None:
        super().__init__(
            503,
            "METADATA_UNAVAILABLE",
            "Metadata is temporarily unavailable for the requested source.",
            details,
        )


class MetadataRevisionMismatchError(AppError):
    def __init__(self) -> None:
        super().__init__(
            409,
            "METADATA_REVISION_MISMATCH",
            "Metadata or SQL policy changed after this SQL was generated. Fetch context and try again.",
        )


class QueryRejectedError(AppError):
    def __init__(self, reason_code: str, *, rejected_construct: str | None = None) -> None:
        details = {"reason_code": reason_code}
        if rejected_construct in _PUBLIC_REJECTED_CONSTRUCTS:
            details["rejected_construct"] = rejected_construct
        super().__init__(
            400,
            "QUERY_REJECTED",
            "The query is not allowed by the source policy.",
            details,
        )


class QueryInvalidError(AppError):
    def __init__(self, reason_code: str) -> None:
        message = _QUERY_INVALID_MESSAGES.get(reason_code)
        if message is None:
            raise ValueError("Query invalid reason is not public.")
        super().__init__(
            400,
            "QUERY_INVALID",
            message,
            {
                "reason_code": reason_code,
                "action": "CORRECT_SQL",
                "retryable": True,
            },
        )


class QueryOverloadedError(AppError):
    def __init__(self) -> None:
        super().__init__(429, "QUERY_OVERLOADED", "The source is currently at its query limit.")


class QueryTimeoutError(AppError):
    def __init__(self) -> None:
        super().__init__(408, "QUERY_TIMEOUT", "The query exceeded its execution deadline.")


class QueryUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(503, "QUERY_UNAVAILABLE", "The query could not be completed.")


class OperatorRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__(403, "OPERATOR_REQUIRED", "Operator permission is required.")


class InsufficientScopeError(AppError):
    def __init__(self) -> None:
        super().__init__(403, "INSUFFICIENT_SCOPE", "Required bearer permission is missing.")


class QueryNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(404, "QUERY_NOT_FOUND", "The active query was not found.")


class SourceValidationError(AppError):
    def __init__(self) -> None:
        super().__init__(400, "SOURCE_VALIDATION_FAILED", "The source configuration was rejected.")


class SourceGenerationConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(409, "SOURCE_GENERATION_CONFLICT", "The source changed; retry with current state.")


class SourceControlUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(503, "SOURCE_CONTROL_UNAVAILABLE", "Source administration is unavailable.")


class MutationNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(404, "MUTATION_NOT_FOUND", "The requested mutation receipt was not found.")


class MutationIdempotencyConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            409,
            "MUTATION_IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used for a different mutation request.",
        )
