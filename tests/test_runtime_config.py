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
