from pathlib import Path

import pytest

from query_man.runtime.config import load_runtime_config
from tests.helpers import ROOT_DIRECTORY

_API_TOKEN = "test-token-with-at-least-thirty-two-characters"


def test_defaults_to_local_http_and_reviewed_source_files() -> None:
    loaded = load_runtime_config({}, ROOT_DIRECTORY)

    assert loaded.host == "127.0.0.1"
    assert loaded.port == 3000
    assert loaded.log_level == "info"
    assert loaded.api_token is None
    assert loaded.access_policy_file is None
    assert loaded.source_directory == ROOT_DIRECTORY / "config" / "sources"
    assert loaded.budget_file == ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
    assert loaded.database_file == ROOT_DIRECTORY / "config" / "database-profiles.yaml"
    assert loaded.database_credential_directory == Path("/run/secrets/query-man/databases")
    assert loaded.metadata_cache_ttl_ms == 30_000
    assert loaded.metadata_max_stale_ms == 300_000
    assert loaded.metadata_retry_delay_ms == 5_000
    assert loaded.shutdown_grace_ms == 10_000


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "query.example"])
def test_non_loopback_requires_authentication(host: str) -> None:
    with pytest.raises(ValueError, match="API_TOKEN or QUERY_MAN_ACCESS_POLICY_FILE"):
        load_runtime_config({"QUERY_MAN_HOST": host}, ROOT_DIRECTORY)


@pytest.mark.parametrize(
    ("authentication", "expected"),
    [
        ({"QUERY_MAN_API_TOKEN": _API_TOKEN}, ("api_token", _API_TOKEN)),
        (
            {"QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml"},
            ("access_policy_file", Path("config/access-policies.yaml")),
        ),
    ],
)
def test_non_loopback_accepts_one_opaque_authentication_authority(
    authentication: dict[str, str],
    expected: tuple[str, object],
) -> None:
    loaded = load_runtime_config(
        {"QUERY_MAN_HOST": "0.0.0.0", **authentication},
        ROOT_DIRECTORY,
    )

    assert loaded.host == "0.0.0.0"
    assert getattr(loaded, expected[0]) == expected[1]


def test_rejects_two_authentication_authorities() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        load_runtime_config(
            {
                "QUERY_MAN_API_TOKEN": _API_TOKEN,
                "QUERY_MAN_ACCESS_POLICY_FILE": "config/access-policies.yaml",
            },
            ROOT_DIRECTORY,
        )


def test_rejects_retired_source_authority_settings_without_disclosure() -> None:
    secret = "retired-setting-value-that-must-not-be-disclosed"
    settings = (
        "QUERY_MAN_SOURCE_MODE",
        "QUERY_MAN_CONTROL_DSN",
        "QUERY_MAN_SOURCE_ENCRYPTION_KEY",
        "QUERY_MAN_REPLICA_ID",
        "QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS",
    )

    for setting in settings:
        with pytest.raises(ValueError, match="only source authority") as captured:
            load_runtime_config({setting: secret}, ROOT_DIRECTORY)
        assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"QUERY_MAN_LOG_LEVEL": "trace"},
        {"QUERY_MAN_PORT": "0"},
        {"QUERY_MAN_API_TOKEN": "too-short"},
        {"QUERY_MAN_METADATA_CACHE_TTL_MS": "3600001"},
        {"QUERY_MAN_METADATA_MAX_STALE_MS": "-1"},
        {"QUERY_MAN_METADATA_RETRY_DELAY_MS": "99"},
        {"QUERY_MAN_SHUTDOWN_GRACE_MS": "300001"},
        {"QUERY_MAN_DATABASE_CREDENTIAL_DIRECTORY": "relative/credentials"},
    ],
)
def test_rejects_invalid_bounded_configuration(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="Invalid runtime configuration"):
        load_runtime_config(environment, ROOT_DIRECTORY)


def test_loads_explicit_paths_and_runtime_limits() -> None:
    loaded = load_runtime_config(
        {
            "QUERY_MAN_LOG_LEVEL": "WARNING",
            "QUERY_MAN_PORT": "8080",
            "QUERY_MAN_SOURCE_DIR": "/srv/query-man/sources",
            "QUERY_MAN_BUDGET_FILE": "/srv/query-man/budgets.yaml",
            "QUERY_MAN_DATABASE_FILE": "/srv/query-man/databases.yaml",
            "QUERY_MAN_DATABASE_CREDENTIAL_DIRECTORY": "/srv/query-man/database-credentials",
            "QUERY_MAN_METADATA_CACHE_TTL_MS": "0",
            "QUERY_MAN_METADATA_MAX_STALE_MS": "1000",
            "QUERY_MAN_METADATA_RETRY_DELAY_MS": "100",
            "QUERY_MAN_SHUTDOWN_GRACE_MS": "0",
        },
        ROOT_DIRECTORY,
    )

    assert loaded.log_level == "warning"
    assert loaded.port == 8080
    assert loaded.source_directory == Path("/srv/query-man/sources")
    assert loaded.budget_file == Path("/srv/query-man/budgets.yaml")
    assert loaded.database_file == Path("/srv/query-man/databases.yaml")
    assert loaded.database_credential_directory == Path("/srv/query-man/database-credentials")
    assert (
        loaded.metadata_cache_ttl_ms,
        loaded.metadata_max_stale_ms,
        loaded.metadata_retry_delay_ms,
        loaded.shutdown_grace_ms,
    ) == (0, 1000, 100, 0)
