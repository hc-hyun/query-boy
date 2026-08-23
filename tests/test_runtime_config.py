from pathlib import Path

import pytest

from query_man.runtime_config import load_runtime_config
from tests.helpers import ROOT_DIRECTORY

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_non_loopback_requires_api_token() -> None:
    with pytest.raises(ValueError, match="QUERY_MAN_API_TOKEN"):
        load_runtime_config({"QUERY_MAN_HOST": "0.0.0.0"}, ROOT_DIRECTORY)


def test_non_loopback_with_api_token_is_allowed() -> None:
    config = load_runtime_config(
        {
            "QUERY_MAN_HOST": "0.0.0.0",
            "QUERY_MAN_API_TOKEN": "test-token-with-at-least-thirty-two-characters",
        },
        ROOT_DIRECTORY,
    )
    assert config.host == "0.0.0.0"


def test_non_loopback_with_access_policy_is_allowed() -> None:
    config = load_runtime_config(
        {
            "QUERY_MAN_HOST": "0.0.0.0",
            "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
        },
        ROOT_DIRECTORY,
    )
    assert config.access_policy_file == Path("config/access-policies.yaml")


def test_loads_mcp_transport_allowlists() -> None:
    config = load_runtime_config(
        {
            "QUERY_MAN_MCP_ALLOWED_HOSTS": "query.example:443,query.example:*",
            "QUERY_MAN_MCP_ALLOWED_ORIGINS": "https://query.example",
        },
        ROOT_DIRECTORY,
    )

    assert config.mcp_allowed_hosts == ("query.example:443", "query.example:*")
    assert config.mcp_allowed_origins == ("https://query.example",)


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1:*,,localhost:*", "127.0.0.1:*\nattacker.invalid"],
)
def test_rejects_invalid_mcp_transport_allowlist(value: str) -> None:
    with pytest.raises(ValueError, match="invalid MCP transport allowlist"):
        load_runtime_config({"QUERY_MAN_MCP_ALLOWED_HOSTS": value}, ROOT_DIRECTORY)


def test_rejects_ambiguous_authentication_configuration() -> None:
    with pytest.raises(ValueError, match="not both"):
        load_runtime_config(
            {
                "QUERY_MAN_API_TOKEN": "test-token-with-at-least-thirty-two-characters",
                "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            },
            ROOT_DIRECTORY,
        )


def test_rejects_unsupported_trace_log_level() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        load_runtime_config({"QUERY_MAN_LOG_LEVEL": "trace"}, ROOT_DIRECTORY)


def test_defaults_to_bootstrap_source_mode() -> None:
    loaded = load_runtime_config({}, ROOT_DIRECTORY)

    assert loaded.source_mode == "bootstrap"
    assert loaded.control_dsn is None
    assert loaded.source_encryption_key is None


def test_loads_managed_source_mode_without_exposing_control_dsn() -> None:
    dsn = "host=control.invalid dbname=query_man user=control password=private-secret"
    loaded = load_runtime_config(
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_CONTROL_DSN": dsn,
            "QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY,
        },
        ROOT_DIRECTORY,
    )
    assert loaded.source_mode == "managed"
    assert loaded.control_dsn == dsn
    assert loaded.source_encryption_key == _SOURCE_KEY

    secret = "sensitive-control-password"
    with pytest.raises(ValueError) as captured:
        load_runtime_config(
            {
                "QUERY_MAN_SOURCE_MODE": "managed",
                "QUERY_MAN_CONTROL_DSN": secret * 100,
                "QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY,
            },
            ROOT_DIRECTORY,
        )
    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"QUERY_MAN_CONTROL_DSN": "host=control.invalid"},
        {"QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY},
        {"QUERY_MAN_SOURCE_MODE": "managed"},
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_CONTROL_DSN": "host=control.invalid",
        },
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY,
        },
    ],
)
def test_rejects_incomplete_source_mode_configuration(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r"QUERY_MAN_SOURCE_MODE|are required"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_rejects_unknown_source_mode() -> None:
    with pytest.raises(ValueError, match="QUERY_MAN_SOURCE_MODE"):
        load_runtime_config({"QUERY_MAN_SOURCE_MODE": "hybrid"}, ROOT_DIRECTORY)
