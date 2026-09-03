from pathlib import Path
from textwrap import dedent, indent

import pytest

from query_man.delivery.access import (
    AccessPolicy,
    AccessPolicyConfigurationError,
    CallerContext,
    caller_audit_fields,
)

_ANALYST_TOKEN = "analyst-token-value-with-at-least-32-characters"
_OPERATOR_TOKEN = "operator-token-value-with-at-least-32-characters"


def _policy_file(tmp_path: Path, callers: str, *, version: int = 2) -> Path:
    path = tmp_path / "access.yaml"
    path.write_text(
        f"version: {version}\ncallers:\n{indent(dedent(callers).strip(), '  ')}\n",
        encoding="utf-8",
    )
    return path


def test_policy_authenticates_query_and_operator_callers(tmp_path: Path) -> None:
    policy = AccessPolicy.load(
        _policy_file(
            tmp_path,
            """
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: OPERATOR_TOKEN
    operator: true
""",
        ),
        {
            "ANALYST_TOKEN": _ANALYST_TOKEN,
            "OPERATOR_TOKEN": _OPERATOR_TOKEN,
        },
    )

    analyst = CallerContext("analyst", "engineering")
    operator = CallerContext("operator", "operations", operator=True)
    assert policy.authenticate(_ANALYST_TOKEN) == analyst
    assert policy.authenticate(_OPERATOR_TOKEN) == operator
    assert policy.authenticate("wrong-token-value-with-at-least-32-characters") is None
    assert caller_audit_fields(analyst) == {
        "caller_id": "analyst",
        "tenant_id": "engineering",
    }


@pytest.mark.parametrize(
    ("token_length", "accepted"),
    [(31, False), (32, True), (512, True), (513, False)],
)
def test_policy_enforces_token_length(
    tmp_path: Path,
    token_length: int,
    accepted: bool,
) -> None:
    path = _policy_file(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
""",
    )
    token = "x" * token_length

    if accepted:
        assert AccessPolicy.load(path, {"ANALYST_TOKEN": token}).authenticate(token)
    else:
        with pytest.raises(AccessPolicyConfigurationError, match="32- to 512-character"):
            AccessPolicy.load(path, {"ANALYST_TOKEN": token})


@pytest.mark.parametrize(
    "unsupported",
    [
        "allowed_sources: [development-issues]",
        "all_sources: true",
    ],
)
def test_policy_rejects_removed_caller_fields(
    tmp_path: Path,
    unsupported: str,
) -> None:
    path = _policy_file(
        tmp_path,
        f"""
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
    {unsupported}
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="Extra inputs are not permitted"):
        AccessPolicy.load(path, {"ANALYST_TOKEN": _ANALYST_TOKEN})


@pytest.mark.parametrize(
    "callers",
    [
        """
  - caller_id: duplicate
    tenant_id: engineering
    token_env: FIRST_TOKEN
  - caller_id: duplicate
    tenant_id: operations
    token_env: SECOND_TOKEN
""",
        """
  - caller_id: first
    tenant_id: engineering
    token_env: SHARED_TOKEN
  - caller_id: second
    tenant_id: operations
    token_env: SHARED_TOKEN
""",
    ],
)
def test_policy_rejects_duplicate_identity_or_token_reference(
    tmp_path: Path,
    callers: str,
) -> None:
    with pytest.raises(AccessPolicyConfigurationError):
        AccessPolicy.load(
            _policy_file(tmp_path, callers),
            {
                "FIRST_TOKEN": _ANALYST_TOKEN,
                "SECOND_TOKEN": _OPERATOR_TOKEN,
                "SHARED_TOKEN": _ANALYST_TOKEN,
            },
        )


def test_policy_rejects_duplicate_token_without_disclosure(tmp_path: Path) -> None:
    shared_secret = "shared-secret-value-with-at-least-32-characters"
    path = _policy_file(
        tmp_path,
        """
  - caller_id: first
    tenant_id: engineering
    token_env: FIRST_TOKEN
  - caller_id: second
    tenant_id: operations
    token_env: SECOND_TOKEN
""",
    )

    with pytest.raises(AccessPolicyConfigurationError, match="tokens must be unique") as caught:
        AccessPolicy.load(
            path,
            {"FIRST_TOKEN": shared_secret, "SECOND_TOKEN": shared_secret},
        )

    assert shared_secret not in str(caught.value)


def test_policy_rejects_old_version_and_missing_secret(tmp_path: Path) -> None:
    callers = """
  - caller_id: analyst
    tenant_id: engineering
    token_env: ANALYST_TOKEN
"""
    with pytest.raises(AccessPolicyConfigurationError, match="version must be 2"):
        AccessPolicy.load(
            _policy_file(tmp_path, callers, version=1),
            {"ANALYST_TOKEN": _ANALYST_TOKEN},
        )

    unrelated_secret = "unrelated-secret-value-with-at-least-32-characters"
    with pytest.raises(AccessPolicyConfigurationError, match="ANALYST_TOKEN") as caught:
        AccessPolicy.load(
            _policy_file(tmp_path, callers),
            {"OTHER_TOKEN": unrelated_secret},
        )
    assert unrelated_secret not in str(caught.value)


def test_local_and_single_token_policies_are_query_only() -> None:
    local = AccessPolicy.local().authenticate(None)
    token = "single-token-value-with-at-least-32-characters"
    single = AccessPolicy.legacy(token).authenticate(token)

    assert local == CallerContext("local-development", "local-development")
    assert single == CallerContext("legacy-api-token", "default")
    assert local is not None and not local.operator
    assert single is not None and not single.operator
