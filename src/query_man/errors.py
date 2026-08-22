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
