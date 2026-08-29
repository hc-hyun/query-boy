from __future__ import annotations

import asyncio
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from query_man.delivery.authentication import (
    InsufficientBearerScopeError,
    InvalidBearerTokenError,
    OAuth2JWTBearerAuthenticator,
    OAuth2ResourceServerConfig,
    _HttpsOnlyRedirectHandler,
)

_ISSUER = "https://smart-dna.sec.samsung.net/ws2/30001/realms/authbridge"
_DISCOVERY = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS = f"{_ISSUER}/protocol/openid-connect/certs"
_AUDIENCE = "query-man"


class _Fetcher:
    def __init__(self, documents: Mapping[str, object | list[object]]) -> None:
        self.documents = dict(documents)
        self.calls: list[str] = []

    async def __call__(self, url: str) -> object:
        self.calls.append(url)
        value = self.documents[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"No response left for {url}")
            return value.pop(0)
        return value


class _BlockingRotationFetcher:
    def __init__(self, old_jwk: dict[str, Any], active_jwk: dict[str, Any]) -> None:
        self.old_jwk = old_jwk
        self.active_jwk = active_jwk
        self.jwks_calls = 0
        self.refresh_started = asyncio.Event()
        self.release_refresh = asyncio.Event()

    async def __call__(self, url: str) -> object:
        if url == _DISCOVERY:
            return {"issuer": _ISSUER, "jwks_uri": _JWKS}
        if url != _JWKS:
            raise AssertionError(f"Unexpected URL: {url}")
        self.jwks_calls += 1
        if self.jwks_calls == 1:
            return {"keys": [self.old_jwk]}
        self.refresh_started.set()
        await self.release_refresh.wait()
        return {"keys": [self.old_jwk, self.active_jwk]}


def _key(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    document = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    document.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private_key, document


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": _ISSUER,
        "aud": [_AUDIENCE],
        "sub": "authbridge-user-001",
        "typ": "Bearer",
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "nbf": int((now - timedelta(seconds=1)).timestamp()),
        "scope": "query-man.read mcp.tools query-man.admin",
        "realm_access": {"roles": ["analyst", "operator"]},
        "groups": ["/query-users", "/query-admins"],
    }
    claims.update(overrides)
    return claims


def _token(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    claims: Mapping[str, object] | None = None,
    token_type: str = "JWT",
) -> str:
    return jwt.encode(
        dict(_claims() if claims is None else claims),
        private_key,
        algorithm="RS256",
        headers={"kid": kid, "typ": token_type},
    )


def _config() -> OAuth2ResourceServerConfig:
    return OAuth2ResourceServerConfig(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        query_scopes=("query-man.read",),
        mcp_scopes=("mcp.tools",),
        operator_scopes=("query-man.admin",),
        query_roles=("analyst",),
        query_groups=("/query-users",),
        operator_roles=("operator",),
        operator_groups=("/query-admins",),
    )


def _documents(jwk: dict[str, Any]) -> dict[str, object]:
    return {
        _DISCOVERY: {"issuer": _ISSUER, "jwks_uri": _JWKS},
        _JWKS: {"keys": [jwk]},
    }


@pytest.mark.asyncio
async def test_validates_authbridge_access_token_and_caches_discovery_and_jwks() -> None:
    private_key, jwk = _key("active-key")
    fetcher = _Fetcher(_documents(jwk))
    authenticator = OAuth2JWTBearerAuthenticator(_config(), fetch_json=fetcher)
    token = _token(private_key, "active-key")

    first = await authenticator.authenticate(token, mcp=True)
    second = await authenticator.authenticate(token, mcp=False)

    assert first == second
    assert first.operator is True
    assert first.tenant_id == "authbridge"
    assert first.caller_id.startswith("oauth-")
    assert "authbridge-user-001" not in first.caller_id
    assert fetcher.calls == [_DISCOVERY, _JWKS]


@pytest.mark.asyncio
async def test_requires_transport_scope_and_optional_query_role_and_group() -> None:
    private_key, jwk = _key("active-key")
    authenticator = OAuth2JWTBearerAuthenticator(
        _config(),
        fetch_json=_Fetcher(_documents(jwk)),
    )
    http_only = _token(
        private_key,
        "active-key",
        claims=_claims(scope="query-man.read"),
    )
    missing_role = _token(
        private_key,
        "active-key",
        claims=_claims(realm_access={"roles": []}),
    )
    missing_group = _token(
        private_key,
        "active-key",
        claims=_claims(groups=[]),
    )

    http_caller = await authenticator.authenticate(http_only, mcp=False)
    assert http_caller.operator is False
    for token in (http_only, missing_role, missing_group):
        with pytest.raises(InsufficientBearerScopeError):
            await authenticator.authenticate(token, mcp=True)


@pytest.mark.asyncio
async def test_operator_requires_all_configured_scope_role_and_group() -> None:
    private_key, jwk = _key("active-key")
    authenticator = OAuth2JWTBearerAuthenticator(
        _config(),
        fetch_json=_Fetcher(_documents(jwk)),
    )
    tokens = (
        _token(private_key, "active-key", claims=_claims(scope="query-man.read mcp.tools")),
        _token(
            private_key,
            "active-key",
            claims=_claims(realm_access={"roles": ["analyst"]}),
        ),
        _token(
            private_key,
            "active-key",
            claims=_claims(groups=["/query-users"]),
        ),
    )

    callers = [await authenticator.authenticate(token, mcp=True) for token in tokens]
    assert all(caller.operator is False for caller in callers)


@pytest.mark.asyncio
async def test_rejects_wrong_signature_issuer_audience_time_and_token_kind() -> None:
    private_key, jwk = _key("active-key")
    other_key, _ = _key("other-key")
    authenticator = OAuth2JWTBearerAuthenticator(
        _config(),
        fetch_json=_Fetcher(_documents(jwk)),
    )
    now = datetime.now(UTC)
    missing_exp = _claims()
    missing_exp.pop("exp")
    missing_sub = _claims()
    missing_sub.pop("sub")
    invalid_tokens = (
        jwt.encode(
            _claims(),
            "not-an-rsa-key-but-at-least-32-bytes-long",
            algorithm="HS256",
            headers={"kid": "active-key", "typ": "JWT"},
        ),
        _token(other_key, "active-key"),
        _token(private_key, "active-key", claims=_claims(iss="https://issuer.invalid")),
        _token(private_key, "active-key", claims=_claims(aud=["other-service"])),
        _token(
            private_key,
            "active-key",
            claims=_claims(exp=int((now - timedelta(minutes=2)).timestamp())),
        ),
        _token(
            private_key,
            "active-key",
            claims=_claims(nbf=int((now + timedelta(minutes=2)).timestamp())),
        ),
        _token(private_key, "active-key", token_type="Refresh"),
        _token(private_key, "active-key", claims=_claims(typ="ID")),
        _token(private_key, "active-key", claims=_claims(typ=None)),
        _token(private_key, "active-key", claims=missing_exp),
        _token(private_key, "active-key", claims=missing_sub),
        _token(
            private_key,
            "active-key",
            claims=_claims(aud=["codex-client"], scope="openid profile"),
        ),
    )

    for token in invalid_tokens:
        with pytest.raises(InvalidBearerTokenError):
            await authenticator.authenticate(token, mcp=False)


@pytest.mark.asyncio
async def test_accepts_rfc_access_token_header_type_case_insensitively() -> None:
    private_key, jwk = _key("active-key")
    authenticator = OAuth2JWTBearerAuthenticator(
        _config(),
        fetch_json=_Fetcher(_documents(jwk)),
    )

    caller = await authenticator.authenticate(
        _token(private_key, "active-key", claims=_claims(typ=None), token_type="at+JWT"),
        mcp=False,
    )

    assert caller.caller_id.startswith("oauth-")


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_jwks_once_with_global_cooldown() -> None:
    private_key, active_jwk = _key("rotated-key")
    _, old_jwk = _key("old-key")
    fetcher = _Fetcher(
        {
            _DISCOVERY: {"issuer": _ISSUER, "jwks_uri": _JWKS},
            _JWKS: [
                {"keys": [old_jwk]},
                {"keys": [old_jwk, active_jwk]},
            ],
        }
    )
    authenticator = OAuth2JWTBearerAuthenticator(_config(), fetch_json=fetcher)

    caller = await authenticator.authenticate(
        _token(private_key, "rotated-key"),
        mcp=False,
    )
    assert caller.caller_id.startswith("oauth-")
    assert fetcher.calls == [_DISCOVERY, _JWKS, _JWKS]

    with pytest.raises(InvalidBearerTokenError):
        await authenticator.authenticate(
            _token(private_key, "still-unknown"),
            mcp=False,
        )
    assert fetcher.calls == [_DISCOVERY, _JWKS, _JWKS]


@pytest.mark.asyncio
async def test_concurrent_requests_reuse_the_single_unknown_kid_refresh() -> None:
    old_private_key, old_jwk = _key("old-key")
    active_private_key, active_jwk = _key("rotated-key")
    fetcher = _BlockingRotationFetcher(old_jwk, active_jwk)
    authenticator = OAuth2JWTBearerAuthenticator(_config(), fetch_json=fetcher)
    await authenticator.authenticate(_token(old_private_key, "old-key"), mcp=False)

    first = asyncio.create_task(
        authenticator.authenticate(
            _token(active_private_key, "rotated-key"),
            mcp=False,
        )
    )
    await fetcher.refresh_started.wait()
    second = asyncio.create_task(
        authenticator.authenticate(
            _token(active_private_key, "rotated-key"),
            mcp=False,
        )
    )
    await asyncio.sleep(0)
    fetcher.release_refresh.set()

    callers = await asyncio.gather(first, second)

    assert callers[0] == callers[1]
    assert fetcher.jwks_calls == 2


@pytest.mark.asyncio
async def test_rejects_discovery_issuer_mismatch_and_non_https_jwks() -> None:
    private_key, _ = _key("active-key")
    token = _token(private_key, "active-key")
    documents = (
        {_DISCOVERY: {"issuer": "https://issuer.invalid", "jwks_uri": _JWKS}},
        {_DISCOVERY: {"issuer": _ISSUER, "jwks_uri": "http://issuer.invalid/keys"}},
    )

    for document in documents:
        authenticator = OAuth2JWTBearerAuthenticator(
            _config(),
            fetch_json=_Fetcher(document),
        )
        with pytest.raises(InvalidBearerTokenError):
            await authenticator.authenticate(token, mcp=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "http://issuer.invalid"},
        {"audience": "contains whitespace"},
        {"query_scopes": ()},
        {"mcp_scopes": ()},
        {"operator_scopes": ()},
        {"query_scopes": ("duplicate", "duplicate")},
    ],
)
def test_rejects_unsafe_resource_server_configuration(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "query_scopes": ("query-man.read",),
        "mcp_scopes": ("mcp.tools",),
        "operator_scopes": ("query-man.admin",),
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        OAuth2ResourceServerConfig(**values)  # type: ignore[arg-type]


def test_rejects_https_metadata_redirect_downgrade() -> None:
    handler = _HttpsOnlyRedirectHandler()

    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        handler.redirect_request(
            urllib.request.Request(_DISCOVERY),
            None,
            302,
            "Found",
            {},
            "http://issuer.invalid/keys",
        )
