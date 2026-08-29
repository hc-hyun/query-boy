from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import jwt

from query_man.delivery.access import AccessPolicy, CallerContext

_ALGORITHM = "RS256"
_ACCESS_TOKEN_TYPES = frozenset({"jwt", "at+jwt", "application/at+jwt"})
_CACHE_TTL_SECONDS = 300.0
_CLOCK_SKEW_SECONDS = 60
_DOCUMENT_MAX_BYTES = 1_048_576
_FETCH_TIMEOUT_SECONDS = 10.0
_TOKEN_MAX_BYTES = 8_192
_UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 30.0

JsonFetcher = Callable[[str], Awaitable[object]]


class InvalidBearerTokenError(Exception):
    pass


class InsufficientBearerScopeError(Exception):
    pass


class BearerAuthenticator(Protocol):
    async def authenticate(
        self,
        token: str | None,
        *,
        mcp: bool,
    ) -> CallerContext: ...


@dataclass(frozen=True)
class OAuth2ResourceServerConfig:
    issuer: str
    audience: str
    query_scopes: tuple[str, ...]
    mcp_scopes: tuple[str, ...]
    operator_scopes: tuple[str, ...]
    query_roles: tuple[str, ...] = ()
    query_groups: tuple[str, ...] = ()
    operator_roles: tuple[str, ...] = ()
    operator_groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_https_url(self.issuer, "issuer")
        if self.issuer.endswith("/"):
            raise ValueError("OAuth issuer must not end with a slash")
        _require_bounded_value(self.audience, "audience", maximum=2_048)
        for name, values in (
            ("query_scopes", self.query_scopes),
            ("mcp_scopes", self.mcp_scopes),
            ("operator_scopes", self.operator_scopes),
        ):
            if not values:
                raise ValueError(f"OAuth {name} must not be empty")
            _require_distinct_values(values, name, scope=True)
        for name, values in (
            ("query_roles", self.query_roles),
            ("query_groups", self.query_groups),
            ("operator_roles", self.operator_roles),
            ("operator_groups", self.operator_groups),
        ):
            _require_distinct_values(values, name, scope=False)


@dataclass(frozen=True)
class _CachedDocument:
    value: dict[str, Any]
    loaded_at: float


class AccessPolicyBearerAuthenticator:
    def __init__(self, policy: AccessPolicy) -> None:
        self._policy = policy

    async def authenticate(
        self,
        token: str | None,
        *,
        mcp: bool,
    ) -> CallerContext:
        del mcp
        caller = self._policy.authenticate(token)
        if caller is None:
            raise InvalidBearerTokenError
        return caller


class OAuth2JWTBearerAuthenticator:
    def __init__(
        self,
        config: OAuth2ResourceServerConfig,
        *,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self._config = config
        self._fetch_json = _fetch_json if fetch_json is None else fetch_json
        self._metadata: _CachedDocument | None = None
        self._jwks: dict[str, _CachedDocument] = {}
        self._metadata_lock = asyncio.Lock()
        self._jwks_locks: dict[str, asyncio.Lock] = {}
        self._unknown_kid_lock = asyncio.Lock()
        self._last_unknown_kid_refresh_at = float("-inf")

    async def authenticate(
        self,
        token: str | None,
        *,
        mcp: bool,
    ) -> CallerContext:
        if token is None or not 1 <= len(token.encode("utf-8")) <= _TOKEN_MAX_BYTES:
            raise InvalidBearerTokenError
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != _ALGORITHM:
                raise InvalidBearerTokenError
            token_type = header.get("typ")
            if (
                not isinstance(token_type, str)
                or token_type.casefold() not in _ACCESS_TOKEN_TYPES
            ):
                raise InvalidBearerTokenError
            kid = header.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                raise InvalidBearerTokenError
            metadata = await self._get_metadata()
            jwks_uri = metadata["jwks_uri"]
            key = await self._get_signing_key(jwks_uri, kid)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[_ALGORITHM],
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
            _validate_access_token_kind(claims, token_type)
            self._validate_numeric_dates(claims)
            subject = claims.get("sub")
            if not isinstance(subject, str) or not 1 <= len(subject) <= 512:
                raise InvalidBearerTokenError
            scopes = _scopes(claims)
            roles = _roles(claims)
            groups = _groups(claims)
        except (InvalidBearerTokenError, InsufficientBearerScopeError):
            raise
        except Exception:
            raise InvalidBearerTokenError from None

        required_scopes = set(self._config.query_scopes)
        if mcp:
            required_scopes.update(self._config.mcp_scopes)
        if not (
            required_scopes.issubset(scopes)
            and set(self._config.query_roles).issubset(roles)
            and set(self._config.query_groups).issubset(groups)
        ):
            raise InsufficientBearerScopeError

        operator = (
            set(self._config.operator_scopes).issubset(scopes)
            and set(self._config.operator_roles).issubset(roles)
            and set(self._config.operator_groups).issubset(groups)
        )
        caller_digest = hashlib.sha256(
            f"{self._config.issuer}\0{subject}".encode()
        ).hexdigest()
        return CallerContext(
            caller_id=f"oauth-{caller_digest[:64]}",
            tenant_id="authbridge",
            operator=operator,
        )

    async def _get_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._metadata is not None and now - self._metadata.loaded_at < _CACHE_TTL_SECONDS:
            return self._metadata.value
        async with self._metadata_lock:
            now = time.monotonic()
            if self._metadata is not None and now - self._metadata.loaded_at < _CACHE_TTL_SECONDS:
                return self._metadata.value
            document = _json_object(
                await self._fetch_json(
                    f"{self._config.issuer}/.well-known/openid-configuration"
                )
            )
            if document.get("issuer") != self._config.issuer:
                raise InvalidBearerTokenError
            jwks_uri = document.get("jwks_uri")
            if not isinstance(jwks_uri, str):
                raise InvalidBearerTokenError
            _require_https_url(jwks_uri, "jwks_uri")
            value = {"issuer": self._config.issuer, "jwks_uri": jwks_uri}
            self._metadata = _CachedDocument(value, now)
            return value

    async def _get_signing_key(self, jwks_uri: str, kid: str) -> jwt.PyJWK:
        document = await self._get_jwks(jwks_uri)
        key = _matching_key(document, kid)
        if key is not None:
            return key
        async with self._unknown_kid_lock:
            document = await self._get_jwks(jwks_uri)
            key = _matching_key(document, kid)
            if key is not None:
                return key
            now = time.monotonic()
            if now - self._last_unknown_kid_refresh_at < _UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS:
                raise InvalidBearerTokenError
            self._last_unknown_kid_refresh_at = now
            document = await self._get_jwks(jwks_uri, force=True)
        key = _matching_key(document, kid)
        if key is None:
            raise InvalidBearerTokenError
        return key

    async def _get_jwks(
        self,
        jwks_uri: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._jwks.get(jwks_uri)
        if not force and cached is not None and now - cached.loaded_at < _CACHE_TTL_SECONDS:
            return cached.value
        lock = self._jwks_locks.setdefault(jwks_uri, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._jwks.get(jwks_uri)
            if not force and cached is not None and now - cached.loaded_at < _CACHE_TTL_SECONDS:
                return cached.value
            document = _json_object(await self._fetch_json(jwks_uri))
            keys = document.get("keys")
            if not isinstance(keys, list) or len(keys) > 100:
                raise InvalidBearerTokenError
            value = {"keys": keys}
            self._jwks[jwks_uri] = _CachedDocument(value, now)
            return value

    @staticmethod
    def _validate_numeric_dates(claims: Mapping[str, Any]) -> None:
        for name in ("exp", "nbf"):
            value = claims.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise InvalidBearerTokenError


def _matching_key(document: Mapping[str, Any], kid: str) -> jwt.PyJWK | None:
    keys = document.get("keys")
    if not isinstance(keys, list):
        raise InvalidBearerTokenError
    for candidate in keys:
        if not isinstance(candidate, dict) or candidate.get("kid") != kid:
            continue
        if candidate.get("kty") != "RSA":
            raise InvalidBearerTokenError
        if candidate.get("alg") not in (None, _ALGORITHM):
            raise InvalidBearerTokenError
        if candidate.get("use") not in (None, "sig"):
            raise InvalidBearerTokenError
        key_ops = candidate.get("key_ops")
        if key_ops is not None and (
            not isinstance(key_ops, list) or "verify" not in key_ops
        ):
            raise InvalidBearerTokenError
        try:
            return jwt.PyJWK.from_dict(candidate, algorithm=_ALGORITHM)
        except Exception:
            raise InvalidBearerTokenError from None
    return None


def _validate_access_token_kind(claims: Mapping[str, Any], header_type: str) -> None:
    claim_type = claims.get("typ")
    if claim_type is not None and claim_type != "Bearer":
        raise InvalidBearerTokenError
    if header_type.casefold() == "jwt" and claim_type != "Bearer":
        raise InvalidBearerTokenError


def _scopes(claims: Mapping[str, Any]) -> set[str]:
    value = claims.get("scope", "")
    if not isinstance(value, str) or len(value) > 4_096:
        raise InvalidBearerTokenError
    scopes = value.split()
    if len(scopes) > 128 or any(not _valid_scope(scope) for scope in scopes):
        raise InvalidBearerTokenError
    return set(scopes)


def _roles(claims: Mapping[str, Any]) -> set[str]:
    realm_access = claims.get("realm_access", {})
    if not isinstance(realm_access, dict):
        raise InvalidBearerTokenError
    return _bounded_string_set(realm_access.get("roles", []))


def _groups(claims: Mapping[str, Any]) -> set[str]:
    return _bounded_string_set(claims.get("groups", []))


def _bounded_string_set(value: object) -> set[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise InvalidBearerTokenError
    if any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in value):
        raise InvalidBearerTokenError
    return set(value)


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidBearerTokenError
    return value


async def _fetch_json(url: str) -> object:
    return await asyncio.to_thread(_fetch_json_blocking, url)


def _fetch_json_blocking(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    with opener.open(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        _require_https_url(response.geturl(), "response URL")
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > _DOCUMENT_MAX_BYTES:
            raise ValueError("OAuth metadata response is too large")
        payload = response.read(_DOCUMENT_MAX_BYTES + 1)
    if len(payload) > _DOCUMENT_MAX_BYTES:
        raise ValueError("OAuth metadata response is too large")
    return json.loads(payload)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        _require_https_url(new_url, "redirect URL")
        return super().redirect_request(
            request,
            response,
            code,
            message,
            headers,
            new_url,
        )


def _require_https_url(value: str, name: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(f"OAuth {name} must be an absolute HTTPS URL")


def _require_bounded_value(value: str, name: str, *, maximum: int) -> None:
    if not 1 <= len(value) <= maximum or any(character.isspace() for character in value):
        raise ValueError(f"OAuth {name} is invalid")


def _require_distinct_values(values: tuple[str, ...], name: str, *, scope: bool) -> None:
    if len(set(values)) != len(values) or len(values) > 32:
        raise ValueError(f"OAuth {name} values must be distinct and bounded")
    for value in values:
        if scope:
            if not _valid_scope(value):
                raise ValueError(f"OAuth {name} contains an invalid scope")
        else:
            _require_bounded_value(value, name, maximum=256)


def _valid_scope(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(
        ord(character) == 0x21
        or 0x23 <= ord(character) <= 0x5B
        or 0x5D <= ord(character) <= 0x7E
        for character in value
    )
