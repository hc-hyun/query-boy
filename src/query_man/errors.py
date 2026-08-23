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

_PUBLIC_QUERY_INVALID_REASONS = frozenset(
    {
        "QUERY_DIVISION_BY_ZERO",
        "QUERY_INVALID_CAST",
        "QUERY_INVALID_LIMIT",
        "QUERY_UNDEFINED_COLUMN",
    }
)


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
        if reason_code not in _PUBLIC_QUERY_INVALID_REASONS:
            raise ValueError("Query invalid reason is not public.")
        super().__init__(
            400,
            "QUERY_INVALID",
            "The query must be corrected before it can run.",
            {"reason_code": reason_code},
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
