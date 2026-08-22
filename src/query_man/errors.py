from typing import Any


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
            "Metadata changed after this SQL was generated. Fetch context and try again.",
        )


class QueryRejectedError(AppError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(
            400,
            "QUERY_REJECTED",
            "The query is not allowed by the source policy.",
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
