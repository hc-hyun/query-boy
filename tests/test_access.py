from pathlib import Path

import pytest

from query_man.access import AccessPolicy, AccessPolicyConfigurationError
from query_man.errors import SourceNotFoundError


def _write_policy(path: Path, body: str) -> Path:
    path.write_text(body.strip(), encoding="utf-8")
    return path


def test_rejects_unknown_source_without_disclosing_token(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: quality
    token_env: ANALYST_TOKEN
    allowed_sources: [unknown-source]
""",
    )
    token = "secret-token-value-with-at-least-32-characters"

    with pytest.raises(AccessPolicyConfigurationError) as caught:
        AccessPolicy.load(path, ["development-issues"], {"ANALYST_TOKEN": token})

    assert token not in str(caught.value)


def test_rejects_missing_or_short_token(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: quality
    token_env: ANALYST_TOKEN
    allowed_sources: [development-issues]
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="32-character"):
        AccessPolicy.load(path, ["development-issues"], {"ANALYST_TOKEN": "too-short"})


def test_rejects_duplicate_caller_and_token_references(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: quality
    token_env: SHARED_TOKEN
    allowed_sources: [development-issues]
  - caller_id: analyst
    tenant_id: engineering
    token_env: SHARED_TOKEN
    allowed_sources: [development-issues]
""",
    )

    with pytest.raises(AccessPolicyConfigurationError):
        AccessPolicy.load(
            path,
            ["development-issues"],
            {"SHARED_TOKEN": "secret-token-value-with-at-least-32-characters"},
        )


def test_all_sources_explicitly_authorizes_future_dynamic_sources(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: operations
    tenant_id: quality
    token_env: OPERATIONS_TOKEN
    all_sources: true
    operator: true
""",
    )
    token = "operations-token-value-with-at-least-32-characters"
    policy = AccessPolicy.load(path, ["development-issues"], {"OPERATIONS_TOKEN": token})

    caller = policy.authenticate(token)

    assert caller is not None
    assert caller.all_sources is True
    assert caller.allowed_sources == frozenset()
    policy.require_source(caller, "future-control-plane-source")


@pytest.mark.parametrize(
    "source_scope",
    [
        "allowed_sources: [development-issues]\n    all_sources: true",
        "all_sources: false",
        "operator: true",
    ],
)
def test_rejects_ambiguous_or_missing_source_scope(
    tmp_path: Path,
    source_scope: str,
) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        f"""
version: 1
callers:
  - caller_id: operations
    tenant_id: quality
    token_env: OPERATIONS_TOKEN
    {source_scope}
""",
    )

    with pytest.raises(AccessPolicyConfigurationError):
        AccessPolicy.load(
            path,
            ["development-issues"],
            {"OPERATIONS_TOKEN": "operations-token-value-with-at-least-32-characters"},
        )


def test_existing_allowed_sources_policy_remains_limited(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: quality
    token_env: ANALYST_TOKEN
    allowed_sources: [development-issues]
    operator: true
""",
    )
    token = "analyst-token-value-with-at-least-32-characters"
    policy = AccessPolicy.load(path, ["development-issues"], {"ANALYST_TOKEN": token})
    caller = policy.authenticate(token)
    assert caller is not None
    assert caller.operator is True
    assert caller.all_sources is False

    policy.require_source(caller, "development-issues")
    with pytest.raises(SourceNotFoundError):
        policy.require_source(caller, "future-control-plane-source")
