from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from query_man.delivery.access import (
    AccessPolicy,
    AccessPolicyConfigurationError,
    CallerContext,
    caller_audit_fields,
)


def _write_policy(path: Path, body: str) -> Path:
    path.write_text(body.strip(), encoding="utf-8")
    return path


def test_loads_query_and_admin_identities_from_v2_policy(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: OPERATOR_TOKEN
    operator: true
""",
    )
    analyst_token = "analyst-token-value-with-at-least-32-characters"
    operator_token = "operator-token-value-with-at-least-32-characters"

    policy = AccessPolicy.load(
        path,
        {
            "ANALYST_TOKEN": analyst_token,
            "OPERATOR_TOKEN": operator_token,
        },
    )
    policy.require_shared_access()

    assert policy.authenticate(analyst_token) == CallerContext(
        caller_id="analyst",
        tenant_id="engineering",
    )
    assert policy.authenticate(operator_token) == CallerContext(
        caller_id="operator",
        tenant_id="operations",
        operator=True,
    )
    assert policy.authenticate("wrong-token-value-with-at-least-32-characters") is None


def test_rejects_v1_policy_without_silently_widening_access(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 1
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="version must be 2"):
        AccessPolicy.load(
            path,
            {"ANALYST_TOKEN": "analyst-token-value-with-at-least-32-characters"},
        )


@pytest.mark.parametrize(
    "source_scope",
    [
        "allowed_sources: [development-issues]",
        "all_sources: true",
    ],
)
def test_rejects_removed_source_scope_fields(tmp_path: Path, source_scope: str) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        f"""
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
    {source_scope}
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="Extra inputs are not permitted"):
        AccessPolicy.load(
            path,
            {"ANALYST_TOKEN": "analyst-token-value-with-at-least-32-characters"},
        )


@pytest.mark.parametrize(
    ("token_length", "accepted"),
    [
        (31, False),
        (32, True),
        (512, True),
        (513, False),
    ],
)
def test_enforces_token_length_boundaries(
    tmp_path: Path,
    token_length: int,
    accepted: bool,
) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
""",
    )
    token = "x" * token_length

    if not accepted:
        with pytest.raises(AccessPolicyConfigurationError, match="32- to 512-character"):
            AccessPolicy.load(path, {"ANALYST_TOKEN": token})
        return

    policy = AccessPolicy.load(path, {"ANALYST_TOKEN": token})
    assert policy.authenticate(token) == CallerContext("analyst", "engineering")


def test_rejects_duplicate_token_digest_without_disclosing_token(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: OPERATOR_TOKEN
    operator: true
""",
    )
    token = "shared-token-value-with-at-least-32-characters"

    with pytest.raises(AccessPolicyConfigurationError, match="tokens must be unique") as caught:
        AccessPolicy.load(
            path,
            {
                "ANALYST_TOKEN": token,
                "OPERATOR_TOKEN": token,
            },
        )

    assert token not in str(caught.value)


@pytest.mark.parametrize(
    "duplicate_field",
    [
        "caller_id: analyst\n    tenant_id: operations\n    token_env: OPERATOR_TOKEN",
        "caller_id: operator\n    tenant_id: operations\n    token_env: ANALYST_TOKEN",
    ],
)
def test_rejects_duplicate_caller_or_token_reference(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        f"""
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
  - {duplicate_field}
    operator: true
""",
    )

    with pytest.raises(AccessPolicyConfigurationError):
        AccessPolicy.load(
            path,
            {
                "ANALYST_TOKEN": "analyst-token-value-with-at-least-32-characters",
                "OPERATOR_TOKEN": "operator-token-value-with-at-least-32-characters",
            },
        )


def test_missing_token_error_does_not_disclose_other_credentials(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
""",
    )
    unrelated_secret = "unrelated-secret-value-with-at-least-32-characters"

    with pytest.raises(AccessPolicyConfigurationError, match="ANALYST_TOKEN") as caught:
        AccessPolicy.load(path, {"OTHER_TOKEN": unrelated_secret})

    assert unrelated_secret not in str(caught.value)


def test_local_and_legacy_compatibility_callers_are_query_only() -> None:
    local = AccessPolicy.local().authenticate(None)
    token = "legacy-token-value-with-at-least-32-characters"
    legacy = AccessPolicy.legacy(token).authenticate(token)

    assert local == CallerContext("local-development", "local-development")
    assert legacy == CallerContext("legacy-api-token", "default")
    assert local.operator is False
    assert legacy.operator is False


def test_loads_expiring_diagnostic_consent_and_pseudonymous_subject(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
    diagnostic_consent:
      version: 1
      receipt_id: consent-2026-001
      expires_at: 2026-09-30T12:00:00+09:00
""",
    )
    token = "analyst-token-value-with-at-least-32-characters"

    policy = AccessPolicy.load(path, {"ANALYST_TOKEN": token})
    policy = policy.with_subject_identifier(
        lambda tenant_id, caller_id: f"subject-{len(tenant_id)}-{len(caller_id)}"
    )
    caller = policy.authenticate(token)

    assert caller is not None
    assert caller.subject_id == "subject-11-7"
    assert caller_audit_fields(caller) == {"subject_id": "subject-11-7"}
    assert caller.diagnostic_consent is not None
    assert caller.diagnostic_consent.version == 1
    assert caller.diagnostic_consent.receipt_id == "consent-2026-001"
    assert caller.diagnostic_consent.expires_at == datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    assert caller.diagnostic_consent.is_active(
        caller.diagnostic_consent.expires_at - timedelta(seconds=1)
    )
    assert not caller.diagnostic_consent.is_active(caller.diagnostic_consent.expires_at)


@pytest.mark.parametrize(
    "consent",
    [
        "version: 2\n      receipt_id: consent-1\n      expires_at: 2026-09-30T00:00:00Z",
        "version: 1\n      receipt_id: consent-1\n      expires_at: 2026-09-30T00:00:00",
        "version: 1\n      receipt_id: consent-1\n      expires_at: 2026-09-30T00:00:00Z\n      extra: rejected",
    ],
)
def test_rejects_invalid_diagnostic_consent(tmp_path: Path, consent: str) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        f"""
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
    diagnostic_consent:
      {consent}
""",
    )

    with pytest.raises(AccessPolicyConfigurationError):
        AccessPolicy.load(
            path,
            {"ANALYST_TOKEN": "analyst-token-value-with-at-least-32-characters"},
        )


def test_rejects_diagnostic_consent_receipt_reuse(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / "access.yaml",
        """
version: 2
callers:
  - caller_id: analyst-a
    tenant_id: engineering
    token_env: ANALYST_A_TOKEN
    diagnostic_consent:
      version: 1
      receipt_id: consent-shared
      expires_at: 2026-09-30T00:00:00Z
  - caller_id: analyst-b
    tenant_id: quality
    token_env: ANALYST_B_TOKEN
    diagnostic_consent:
      version: 1
      receipt_id: consent-shared
      expires_at: 2026-09-30T00:00:00Z
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="receipt_id values must be unique"):
        AccessPolicy.load(
            path,
            {
                "ANALYST_A_TOKEN": "analyst-a-token-value-with-at-least-32-characters",
                "ANALYST_B_TOKEN": "analyst-b-token-value-with-at-least-32-characters",
            },
        )
