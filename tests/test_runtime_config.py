from pathlib import Path

import pytest

from query_man.runtime.config import load_runtime_config
from tests.helpers import ROOT_DIRECTORY

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_OAUTH_ISSUER = "https://smart-dna.sec.samsung.net/ws2/30001/realms/authbridge"


def _oauth_environment() -> dict[str, str]:
    return {
        "QUERY_MAN_OAUTH_ISSUER": _OAUTH_ISSUER,
        "QUERY_MAN_OAUTH_AUDIENCE": "query-man",
        "QUERY_MAN_OAUTH_QUERY_SCOPES": "query-man.read",
        "QUERY_MAN_OAUTH_MCP_SCOPES": "mcp.tools",
        "QUERY_MAN_OAUTH_OPERATOR_SCOPES": "query-man.admin",
    }


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


def test_non_loopback_with_oauth_resource_server_is_allowed() -> None:
    config = load_runtime_config(
        {"QUERY_MAN_HOST": "0.0.0.0", **_oauth_environment()},
        ROOT_DIRECTORY,
    )

    assert config.oauth is not None
    assert config.oauth.issuer == _OAUTH_ISSUER
    assert config.oauth.audience == "query-man"
    assert config.oauth.query_scopes == ("query-man.read",)
    assert config.oauth.mcp_scopes == ("mcp.tools",)
    assert config.oauth.operator_scopes == ("query-man.admin",)


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
    with pytest.raises(ValueError, match="exactly one"):
        load_runtime_config(
            {
                "QUERY_MAN_API_TOKEN": "test-token-with-at-least-thirty-two-characters",
                "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            },
            ROOT_DIRECTORY,
        )


def test_rejects_oauth_combined_with_opaque_access_policy() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        load_runtime_config(
            {
                **_oauth_environment(),
                "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            },
            ROOT_DIRECTORY,
        )


@pytest.mark.parametrize(
    "missing",
    [
        "QUERY_MAN_OAUTH_ISSUER",
        "QUERY_MAN_OAUTH_AUDIENCE",
        "QUERY_MAN_OAUTH_QUERY_SCOPES",
        "QUERY_MAN_OAUTH_MCP_SCOPES",
        "QUERY_MAN_OAUTH_OPERATOR_SCOPES",
    ],
)
def test_rejects_partial_oauth_resource_server_configuration(missing: str) -> None:
    environment = _oauth_environment()
    environment.pop(missing)

    with pytest.raises(ValueError, match="must be configured together"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_loads_optional_oauth_roles_and_groups() -> None:
    loaded = load_runtime_config(
        {
            **_oauth_environment(),
            "QUERY_MAN_OAUTH_QUERY_ROLES": "analyst",
            "QUERY_MAN_OAUTH_QUERY_GROUPS": "/query-users",
            "QUERY_MAN_OAUTH_OPERATOR_ROLES": "operator",
            "QUERY_MAN_OAUTH_OPERATOR_GROUPS": "/query-admins",
        },
        ROOT_DIRECTORY,
    )

    assert loaded.oauth is not None
    assert loaded.oauth.query_roles == ("analyst",)
    assert loaded.oauth.query_groups == ("/query-users",)
    assert loaded.oauth.operator_roles == ("operator",)
    assert loaded.oauth.operator_groups == ("/query-admins",)


def test_rejects_unsupported_trace_log_level() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        load_runtime_config({"QUERY_MAN_LOG_LEVEL": "trace"}, ROOT_DIRECTORY)


def test_defaults_to_git_reviewed_yaml_source_authority() -> None:
    loaded = load_runtime_config({}, ROOT_DIRECTORY)

    assert loaded.source_directory == ROOT_DIRECTORY / "config" / "sources"
    assert loaded.budget_file == ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
    assert loaded.diagnostic_capture_database is None
    assert loaded.diagnostic_capture_key is None
    assert loaded.diagnostic_capture_key_id is None
    assert loaded.diagnostic_capture_daily_bytes == 100 * 1024 * 1024
    assert loaded.oauth is None


def test_loads_complete_diagnostic_capture_configuration() -> None:
    loaded = load_runtime_config(
        {
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE": "/captures/query-man.sqlite3",
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY": _SOURCE_KEY,
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID": "capture-key-2026-08",
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_DAILY_BYTES": "1048576",
        },
        ROOT_DIRECTORY,
    )

    assert loaded.diagnostic_capture_database == Path("/captures/query-man.sqlite3")
    assert loaded.diagnostic_capture_key == _SOURCE_KEY
    assert loaded.diagnostic_capture_key_id == "capture-key-2026-08"
    assert loaded.diagnostic_capture_daily_bytes == 1_048_576


@pytest.mark.parametrize(
    "environment",
    [
        {"QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE": "/captures/query-man.sqlite3"},
        {"QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY": _SOURCE_KEY},
        {"QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID": "capture-key-2026-08"},
    ],
)
def test_rejects_partial_diagnostic_capture_configuration(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="must be configured together"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_empty_diagnostic_capture_environment_values_disable_capture() -> None:
    loaded = load_runtime_config(
        {
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE": "",
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY": "",
            "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID": "",
        },
        ROOT_DIRECTORY,
    )

    assert loaded.diagnostic_capture_database is None


def test_oauth_resource_server_rejects_diagnostic_capture() -> None:
    with pytest.raises(ValueError, match="does not accept diagnostic capture"):
        load_runtime_config(
            {
                **_oauth_environment(),
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE": "/captures/query-man.sqlite3",
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY": _SOURCE_KEY,
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID": "capture-key-2026-08",
            },
            ROOT_DIRECTORY,
        )


def test_invalid_diagnostic_capture_key_is_not_disclosed() -> None:
    secret = "too-short-private-capture-key"
    with pytest.raises(ValueError) as captured:
        load_runtime_config(
            {
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE": "/captures/query-man.sqlite3",
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY": secret,
                "QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID": "capture-key-2026-08",
            },
            ROOT_DIRECTORY,
        )

    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "setting",
    [
        "QUERY_MAN_SOURCE_MODE",
        "QUERY_MAN_CONTROL_DSN",
        "QUERY_MAN_SOURCE_ENCRYPTION_KEY",
        "QUERY_MAN_REPLICA_ID",
        "QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS",
    ],
)
def test_rejects_retired_source_authority_settings(setting: str) -> None:
    secret = "retired-setting-value-that-must-not-be-disclosed"

    with pytest.raises(ValueError, match="Git-reviewed YAML") as captured:
        load_runtime_config({setting: secret}, ROOT_DIRECTORY)

    assert setting in str(captured.value)
    assert secret not in str(captured.value)
