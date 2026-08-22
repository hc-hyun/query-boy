from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _Environment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    host: str = Field("127.0.0.1", alias="QUERY_MAN_HOST", min_length=1)
    port: int = Field(3000, alias="QUERY_MAN_PORT", ge=1, le=65535)
    log_level: str = Field("info", alias="QUERY_MAN_LOG_LEVEL")
    api_token: str | None = Field(None, alias="QUERY_MAN_API_TOKEN", min_length=32, max_length=512)
    source_dir: str | None = Field(None, alias="QUERY_MAN_SOURCE_DIR")
    budget_file: str | None = Field(None, alias="QUERY_MAN_BUDGET_FILE")
    cache_ttl_ms: int = Field(30_000, alias="QUERY_MAN_METADATA_CACHE_TTL_MS", ge=0, le=3_600_000)
    max_stale_ms: int = Field(300_000, alias="QUERY_MAN_METADATA_MAX_STALE_MS", ge=0, le=86_400_000)
    retry_delay_ms: int = Field(5_000, alias="QUERY_MAN_METADATA_RETRY_DELAY_MS", ge=100, le=300_000)

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        allowed = {"critical", "fatal", "error", "warning", "warn", "info", "debug", "trace"}
        if value.lower() not in allowed:
            raise ValueError("invalid log level")
        return value.lower()


@dataclass(frozen=True)
class RuntimeConfig:
    host: str
    port: int
    log_level: str
    api_token: str | None
    source_directory: Path
    budget_file: Path
    metadata_cache_ttl_ms: int
    metadata_max_stale_ms: int
    metadata_retry_delay_ms: int


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
    root_directory: Path | None = None,
) -> RuntimeConfig:
    values = dict(os.environ if environment is None else environment)
    if values.get("QUERY_MAN_API_TOKEN") == "":
        values.pop("QUERY_MAN_API_TOKEN")
    try:
        parsed = _Environment.model_validate(values)
    except ValidationError as error:
        raise ValueError(f"Invalid runtime configuration: {error}") from error
    if not _is_loopback(parsed.host) and parsed.api_token is None:
        raise ValueError("QUERY_MAN_API_TOKEN is required when QUERY_MAN_HOST is not loopback")
    root = (root_directory or Path.cwd()).resolve()
    return RuntimeConfig(
        host=parsed.host,
        port=parsed.port,
        log_level=parsed.log_level,
        api_token=parsed.api_token,
        source_directory=Path(parsed.source_dir) if parsed.source_dir else root / "config" / "sources",
        budget_file=Path(parsed.budget_file) if parsed.budget_file else root / "config" / "budget-profiles.yaml",
        metadata_cache_ttl_ms=parsed.cache_ttl_ms,
        metadata_max_stale_ms=parsed.max_stale_ms,
        metadata_retry_delay_ms=parsed.retry_delay_ms,
    )


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
