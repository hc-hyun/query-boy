from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from query_man.delivery.authentication import OAuth2ResourceServerConfig

_DEFAULT_MCP_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_DEFAULT_MCP_ALLOWED_ORIGINS = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)
_DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES = 100 * 1024 * 1024
_RETIRED_SOURCE_AUTHORITY_SETTINGS = (
    "QUERY_MAN_SOURCE_MODE",
    "QUERY_MAN_CONTROL_DSN",
    "QUERY_MAN_SOURCE_ENCRYPTION_KEY",
    "QUERY_MAN_REPLICA_ID",
    "QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS",
)


class _Environment(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    host: str = Field("127.0.0.1", alias="QUERY_MAN_HOST", min_length=1)
    port: int = Field(3000, alias="QUERY_MAN_PORT", ge=1, le=65535)
    log_level: str = Field("info", alias="QUERY_MAN_LOG_LEVEL")
    api_token: str | None = Field(None, alias="QUERY_MAN_API_TOKEN", min_length=32, max_length=512)
    source_dir: str | None = Field(None, alias="QUERY_MAN_SOURCE_DIR")
    budget_file: str | None = Field(None, alias="QUERY_MAN_BUDGET_FILE")
    access_policy_file: str | None = Field(None, alias="QUERY_MAN_ACCESS_POLICY_FILE")
    oauth_issuer: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_ISSUER",
        min_length=1,
        max_length=2_048,
    )
    oauth_audience: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_AUDIENCE",
        min_length=1,
        max_length=2_048,
    )
    oauth_query_scopes: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_QUERY_SCOPES",
        min_length=1,
        max_length=4_096,
    )
    oauth_mcp_scopes: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_MCP_SCOPES",
        min_length=1,
        max_length=4_096,
    )
    oauth_operator_scopes: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_OPERATOR_SCOPES",
        min_length=1,
        max_length=4_096,
    )
    oauth_query_roles: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_QUERY_ROLES",
        max_length=4_096,
    )
    oauth_query_groups: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_QUERY_GROUPS",
        max_length=4_096,
    )
    oauth_operator_roles: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_OPERATOR_ROLES",
        max_length=4_096,
    )
    oauth_operator_groups: str | None = Field(
        None,
        alias="QUERY_MAN_OAUTH_OPERATOR_GROUPS",
        max_length=4_096,
    )
    cache_ttl_ms: int = Field(30_000, alias="QUERY_MAN_METADATA_CACHE_TTL_MS", ge=0, le=3_600_000)
    max_stale_ms: int = Field(300_000, alias="QUERY_MAN_METADATA_MAX_STALE_MS", ge=0, le=86_400_000)
    retry_delay_ms: int = Field(5_000, alias="QUERY_MAN_METADATA_RETRY_DELAY_MS", ge=100, le=300_000)
    diagnostic_capture_database: str | None = Field(
        None,
        alias="QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE",
        min_length=1,
        max_length=2_048,
    )
    diagnostic_capture_key: SecretStr | None = Field(
        None,
        alias="QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY",
        min_length=43,
        max_length=64,
    )
    diagnostic_capture_key_id: str | None = Field(
        None,
        alias="QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID",
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    diagnostic_capture_daily_bytes: int = Field(
        _DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES,
        alias="QUERY_MAN_DIAGNOSTIC_CAPTURE_DAILY_BYTES",
        ge=1_048_576,
        le=10_737_418_240,
    )
    shutdown_grace_ms: int = Field(
        10_000,
        alias="QUERY_MAN_SHUTDOWN_GRACE_MS",
        ge=0,
        le=300_000,
    )
    mcp_allowed_hosts: str = Field(
        ",".join(_DEFAULT_MCP_ALLOWED_HOSTS),
        alias="QUERY_MAN_MCP_ALLOWED_HOSTS",
        min_length=1,
        max_length=2_048,
    )
    mcp_allowed_origins: str = Field(
        ",".join(_DEFAULT_MCP_ALLOWED_ORIGINS),
        alias="QUERY_MAN_MCP_ALLOWED_ORIGINS",
        min_length=1,
        max_length=4_096,
    )

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        allowed = {"critical", "fatal", "error", "warning", "warn", "info", "debug"}
        if value.lower() not in allowed:
            raise ValueError("invalid log level")
        return value.lower()

    @field_validator("mcp_allowed_hosts", "mcp_allowed_origins")
    @classmethod
    def valid_mcp_transport_allowlist(cls, value: str) -> str:
        entries = [entry.strip() for entry in value.split(",")]
        if any(not entry or "\n" in entry or "\r" in entry for entry in entries):
            raise ValueError("invalid MCP transport allowlist")
        return value


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
    shutdown_grace_ms: int = 10_000
    mcp_allowed_hosts: tuple[str, ...] = _DEFAULT_MCP_ALLOWED_HOSTS
    mcp_allowed_origins: tuple[str, ...] = _DEFAULT_MCP_ALLOWED_ORIGINS
    diagnostic_capture_database: Path | None = None
    diagnostic_capture_key: str | None = None
    diagnostic_capture_key_id: str | None = None
    diagnostic_capture_daily_bytes: int = _DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES
    oauth: OAuth2ResourceServerConfig | None = None

    def __post_init__(self) -> None:
        if self.oauth is not None and (
            self.api_token is not None or self.access_policy_file is not None
        ):
            raise ValueError(
                "OAuth resource-server settings cannot be combined with "
                "QUERY_MAN_API_TOKEN or QUERY_MAN_ACCESS_POLICY_FILE"
            )
        diagnostic_values = (
            self.diagnostic_capture_database,
            self.diagnostic_capture_key,
            self.diagnostic_capture_key_id,
        )
        if any(value is not None for value in diagnostic_values) and any(
            value is None for value in diagnostic_values
        ):
            raise ValueError(
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE, "
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY and "
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID must be configured together"
            )
        if self.oauth is not None and any(value is not None for value in diagnostic_values):
            raise ValueError(
                "OAuth resource-server mode does not accept diagnostic capture settings"
            )
        if not 1_048_576 <= self.diagnostic_capture_daily_bytes <= 10_737_418_240:
            raise ValueError(
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_DAILY_BYTES must be between 1 MiB and 10 GiB"
            )


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
    root_directory: Path | None = None,
) -> RuntimeConfig:
    values = dict(os.environ if environment is None else environment)
    retired_settings = tuple(
        name for name in _RETIRED_SOURCE_AUTHORITY_SETTINGS if name in values
    )
    if retired_settings:
        raise ValueError(
            "Retired source-authority settings are no longer supported; "
            "Git-reviewed source packages are the only source authority. Remove: "
            + ", ".join(retired_settings)
        )
    for optional_name in (
        "QUERY_MAN_API_TOKEN",
        "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE",
        "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY",
        "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID",
        "QUERY_MAN_OAUTH_ISSUER",
        "QUERY_MAN_OAUTH_AUDIENCE",
        "QUERY_MAN_OAUTH_QUERY_SCOPES",
        "QUERY_MAN_OAUTH_MCP_SCOPES",
        "QUERY_MAN_OAUTH_OPERATOR_SCOPES",
        "QUERY_MAN_OAUTH_QUERY_ROLES",
        "QUERY_MAN_OAUTH_QUERY_GROUPS",
        "QUERY_MAN_OAUTH_OPERATOR_ROLES",
        "QUERY_MAN_OAUTH_OPERATOR_GROUPS",
    ):
        if values.get(optional_name) == "":
            values.pop(optional_name)
    try:
        parsed = _Environment.model_validate(values)
    except ValidationError as error:
        raise ValueError(f"Invalid runtime configuration: {error}") from error
    oauth_required = (
        parsed.oauth_issuer,
        parsed.oauth_audience,
        parsed.oauth_query_scopes,
        parsed.oauth_mcp_scopes,
        parsed.oauth_operator_scopes,
    )
    oauth_enabled = any(value is not None for value in oauth_required)
    if oauth_enabled and any(value is None for value in oauth_required):
        raise ValueError(
            "QUERY_MAN_OAUTH_ISSUER, QUERY_MAN_OAUTH_AUDIENCE, "
            "QUERY_MAN_OAUTH_QUERY_SCOPES, QUERY_MAN_OAUTH_MCP_SCOPES and "
            "QUERY_MAN_OAUTH_OPERATOR_SCOPES must be configured together"
        )
    configured_authentication = sum(
        (
            parsed.api_token is not None,
            parsed.access_policy_file is not None,
            oauth_enabled,
        )
    )
    if configured_authentication > 1:
        raise ValueError(
            "Configure exactly one of QUERY_MAN_API_TOKEN, "
            "QUERY_MAN_ACCESS_POLICY_FILE or OAuth resource-server settings"
        )
    if (
        not _is_loopback(parsed.host)
        and parsed.api_token is None
        and parsed.access_policy_file is None
        and not oauth_enabled
    ):
        raise ValueError(
            "QUERY_MAN_API_TOKEN, QUERY_MAN_ACCESS_POLICY_FILE or OAuth resource-server "
            "settings are required when QUERY_MAN_HOST is not loopback"
        )
    oauth = None
    if oauth_enabled:
        try:
            oauth = OAuth2ResourceServerConfig(
                issuer=_required(parsed.oauth_issuer),
                audience=_required(parsed.oauth_audience),
                query_scopes=_split_values(_required(parsed.oauth_query_scopes)),
                mcp_scopes=_split_values(_required(parsed.oauth_mcp_scopes)),
                operator_scopes=_split_values(_required(parsed.oauth_operator_scopes)),
                query_roles=_split_values(parsed.oauth_query_roles),
                query_groups=_split_values(parsed.oauth_query_groups),
                operator_roles=_split_values(parsed.oauth_operator_roles),
                operator_groups=_split_values(parsed.oauth_operator_groups),
            )
        except ValueError as error:
            raise ValueError(f"Invalid OAuth resource-server configuration: {error}") from error
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
        shutdown_grace_ms=parsed.shutdown_grace_ms,
        mcp_allowed_hosts=_split_allowlist(parsed.mcp_allowed_hosts),
        mcp_allowed_origins=_split_allowlist(parsed.mcp_allowed_origins),
        diagnostic_capture_database=(
            Path(parsed.diagnostic_capture_database)
            if parsed.diagnostic_capture_database is not None
            else None
        ),
        diagnostic_capture_key=(
            parsed.diagnostic_capture_key.get_secret_value()
            if parsed.diagnostic_capture_key is not None
            else None
        ),
        diagnostic_capture_key_id=parsed.diagnostic_capture_key_id,
        diagnostic_capture_daily_bytes=parsed.diagnostic_capture_daily_bytes,
        oauth=oauth,
    )


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_allowlist(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(entry.strip() for entry in value.split(",")))


def _split_values(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(entry.strip() for entry in value.split(",") if entry.strip())


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("required OAuth setting is missing")
    return value
