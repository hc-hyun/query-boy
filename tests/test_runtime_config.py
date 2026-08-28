from dataclasses import replace
from pathlib import Path

import pytest

from query_man.runtime.config import RuntimeConfig, load_runtime_config
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
    assert loaded.replica_id is None
    assert loaded.diagnostic_capture_database is None
    assert loaded.diagnostic_capture_key is None
    assert loaded.diagnostic_capture_key_id is None
    assert loaded.diagnostic_capture_daily_bytes == 100 * 1024 * 1024


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


def _managed_environment(**overrides: str) -> dict[str, str]:
    return {
        "QUERY_MAN_SOURCE_MODE": "managed",
        "QUERY_MAN_CONTROL_DSN": "host=control.invalid",
        "QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY,
        "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
        "QUERY_MAN_REPLICA_ID": "runtime-a",
        **overrides,
    }


def test_loads_managed_source_mode_without_exposing_control_dsn() -> None:
    dsn = "host=control.invalid dbname=query_man user=control password=private-secret"
    loaded = load_runtime_config(
        _managed_environment(QUERY_MAN_CONTROL_DSN=dsn),
        ROOT_DIRECTORY,
    )
    assert loaded.source_mode == "managed"
    assert loaded.control_dsn == dsn
    assert loaded.source_encryption_key == _SOURCE_KEY
    assert loaded.replica_id == "runtime-a"

    secret = "sensitive-control-password"
    with pytest.raises(ValueError) as captured:
        load_runtime_config(
            _managed_environment(QUERY_MAN_CONTROL_DSN=secret * 100),
            ROOT_DIRECTORY,
        )
    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"QUERY_MAN_CONTROL_DSN": "host=control.invalid"},
        {"QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY},
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            "QUERY_MAN_REPLICA_ID": "runtime-a",
        },
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_CONTROL_DSN": "host=control.invalid",
            "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            "QUERY_MAN_REPLICA_ID": "runtime-a",
        },
        {
            "QUERY_MAN_SOURCE_MODE": "managed",
            "QUERY_MAN_SOURCE_ENCRYPTION_KEY": _SOURCE_KEY,
            "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            "QUERY_MAN_REPLICA_ID": "runtime-a",
        },
    ],
)
def test_rejects_incomplete_source_mode_configuration(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r"QUERY_MAN_SOURCE_MODE|are required"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_rejects_unknown_source_mode() -> None:
    with pytest.raises(ValueError, match="QUERY_MAN_SOURCE_MODE"):
        load_runtime_config({"QUERY_MAN_SOURCE_MODE": "hybrid"}, ROOT_DIRECTORY)


def test_managed_source_mode_requires_access_policy() -> None:
    environment = _managed_environment()
    environment.pop("QUERY_MAN_ACCESS_POLICY_FILE")

    with pytest.raises(ValueError, match="requires QUERY_MAN_ACCESS_POLICY_FILE"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_managed_source_mode_rejects_legacy_api_token() -> None:
    environment = _managed_environment(
        QUERY_MAN_API_TOKEN="legacy-token-value-with-at-least-32-characters",
    )
    environment.pop("QUERY_MAN_ACCESS_POLICY_FILE")

    with pytest.raises(ValueError, match="does not accept QUERY_MAN_API_TOKEN"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_managed_runtime_config_rejects_direct_api_token_construction() -> None:
    managed = load_runtime_config(
        _managed_environment(),
        ROOT_DIRECTORY,
    )

    with pytest.raises(ValueError, match="not accepted"):
        replace(
            managed,
            api_token="legacy-token-value-with-at-least-32-characters",
        )


def test_managed_source_mode_requires_replica_id() -> None:
    environment = _managed_environment()
    environment.pop("QUERY_MAN_REPLICA_ID")

    with pytest.raises(ValueError, match="QUERY_MAN_REPLICA_ID"):
        load_runtime_config(environment, ROOT_DIRECTORY)


@pytest.mark.parametrize("replica_id", ["a", "a" * 80, "runtime-a"])
def test_managed_source_mode_accepts_replica_id_boundaries(replica_id: str) -> None:
    loaded = load_runtime_config(
        _managed_environment(QUERY_MAN_REPLICA_ID=replica_id),
        ROOT_DIRECTORY,
    )

    assert loaded.replica_id == replica_id


@pytest.mark.parametrize(
    "replica_id",
    ["", "Runtime-a", "runtime_a", "-runtime", "runtime-", "runtime--a", "a" * 81],
)
def test_managed_source_mode_rejects_invalid_replica_id(replica_id: str) -> None:
    with pytest.raises(ValueError, match="QUERY_MAN_REPLICA_ID"):
        load_runtime_config(
            _managed_environment(QUERY_MAN_REPLICA_ID=replica_id),
            ROOT_DIRECTORY,
        )


@pytest.mark.parametrize("replica_id", ["INVALID_replica--id", "a" * 81])
def test_bootstrap_ignores_invalid_replica_id(replica_id: str) -> None:
    loaded = load_runtime_config(
        {"QUERY_MAN_REPLICA_ID": replica_id},
        ROOT_DIRECTORY,
    )

    assert loaded.replica_id is None


@pytest.mark.parametrize("replica_id", [None, "runtime_a", "a" * 81])
def test_runtime_config_direct_construction_enforces_replica_id_boundary(
    replica_id: str | None,
) -> None:
    managed = load_runtime_config(_managed_environment(), ROOT_DIRECTORY)

    with pytest.raises(ValueError, match="QUERY_MAN_REPLICA_ID"):
        replace(managed, replica_id=replica_id)


def test_runtime_config_direct_bootstrap_construction_does_not_validate_replica_id() -> None:
    bootstrap: RuntimeConfig = load_runtime_config({}, ROOT_DIRECTORY)

    replaced = replace(bootstrap, replica_id="INVALID_replica--id")

    assert replaced.replica_id == "INVALID_replica--id"
