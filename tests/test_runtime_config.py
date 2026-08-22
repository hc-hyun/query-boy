from pathlib import Path

import pytest

from query_man.runtime_config import load_runtime_config
from tests.helpers import ROOT_DIRECTORY


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


def test_loads_optional_control_dsn_without_exposing_it_on_validation_error() -> None:
    dsn = "host=control.invalid dbname=query_man user=control password=private-secret"
    loaded = load_runtime_config({"QUERY_MAN_CONTROL_DSN": dsn}, ROOT_DIRECTORY)
    assert loaded.control_dsn == dsn

    secret = "sensitive-control-password"
    with pytest.raises(ValueError) as captured:
        load_runtime_config({"QUERY_MAN_CONTROL_DSN": secret * 100}, ROOT_DIRECTORY)
    assert secret not in str(captured.value)
