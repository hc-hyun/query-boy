from pathlib import Path

import pytest

from query_man.access import AccessPolicy, AccessPolicyConfigurationError


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
