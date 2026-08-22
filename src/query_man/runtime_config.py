from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class _Environment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    host: str = Field("127.0.0.1", alias="QUERY_MAN_HOST", min_length=1)
    port: int = Field(3000, alias="QUERY_MAN_PORT", ge=1, le=65535)
    log_level: str = Field("info", alias="QUERY_MAN_LOG_LEVEL")
    api_token: str | None = Field(None, alias="QUERY_MAN_API_TOKEN", min_length=32, max_length=512)
    source_dir: str | None = Field(None, alias="QUERY_MAN_SOURCE_DIR")
    budget_file: str | None = Field(None, alias="QUERY_MAN_BUDGET_FILE")
    access_policy_file: str | None = Field(None, alias="QUERY_MAN_ACCESS_POLICY_FILE")
    cache_ttl_ms: int = Field(30_000, alias="QUERY_MAN_METADATA_CACHE_TTL_MS", ge=0, le=3_600_000)
    max_stale_ms: int = Field(300_000, alias="QUERY_MAN_METADATA_MAX_STALE_MS", ge=0, le=86_400_000)
    retry_delay_ms: int = Field(5_000, alias="QUERY_MAN_METADATA_RETRY_DELAY_MS", ge=100, le=300_000)
    control_dsn: SecretStr | None = Field(
        None,
        alias="QUERY_MAN_CONTROL_DSN",
        min_length=1,
        max_length=2_048,
    )
    source_encryption_key: SecretStr | None = Field(
        None,
        alias="QUERY_MAN_SOURCE_ENCRYPTION_KEY",
        min_length=43,
        max_length=64,
    )
    source_reload_interval_ms: int = Field(
        5_000,
        alias="QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS",
        ge=250,
        le=300_000,
    )
    shutdown_grace_ms: int = Field(
        10_000,
        alias="QUERY_MAN_SHUTDOWN_GRACE_MS",
        ge=0,
        le=300_000,
    )

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        allowed = {"critical", "fatal", "error", "warning", "warn", "info", "debug"}
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
    access_policy_file: Path | None
    metadata_cache_ttl_ms: int
    metadata_max_stale_ms: int
    metadata_retry_delay_ms: int
    control_dsn: str | None = None
    source_encryption_key: str | None = None
    source_reload_interval_ms: int = 5_000
    shutdown_grace_ms: int = 10_000


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
    if parsed.api_token is not None and parsed.access_policy_file is not None:
        raise ValueError("Configure QUERY_MAN_API_TOKEN or QUERY_MAN_ACCESS_POLICY_FILE, not both")
    if parsed.source_encryption_key is not None and parsed.control_dsn is None:
        raise ValueError("QUERY_MAN_CONTROL_DSN is required for source control")
    if not _is_loopback(parsed.host) and parsed.api_token is None and parsed.access_policy_file is None:
        raise ValueError(
            "QUERY_MAN_API_TOKEN or QUERY_MAN_ACCESS_POLICY_FILE is required when QUERY_MAN_HOST is not loopback"
        )
    root = (root_directory or Path.cwd()).resolve()
    return RuntimeConfig(
        host=parsed.host,
        port=parsed.port,
        log_level=parsed.log_level,
        api_token=parsed.api_token,
        source_directory=Path(parsed.source_dir) if parsed.source_dir else root / "config" / "sources",
        budget_file=Path(parsed.budget_file) if parsed.budget_file else root / "config" / "budget-profiles.yaml",
        access_policy_file=Path(parsed.access_policy_file) if parsed.access_policy_file else None,
        metadata_cache_ttl_ms=parsed.cache_ttl_ms,
        metadata_max_stale_ms=parsed.max_stale_ms,
        metadata_retry_delay_ms=parsed.retry_delay_ms,
        control_dsn=(parsed.control_dsn.get_secret_value() if parsed.control_dsn is not None else None),
        source_encryption_key=(
            parsed.source_encryption_key.get_secret_value()
            if parsed.source_encryption_key is not None
            else None
        ),
        source_reload_interval_ms=parsed.source_reload_interval_ms,
        shutdown_grace_ms=parsed.shutdown_grace_ms,
    )


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
