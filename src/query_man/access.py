from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from query_man.errors import SourceNotFoundError

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")]
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


class AccessPolicyConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class CallerContext:
    caller_id: str
    tenant_id: str
    allowed_sources: frozenset[str]
    operator: bool = False
    all_sources: bool = False


@dataclass(frozen=True)
class _Credential:
    token_digest: bytes
    caller: CallerContext


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Caller(_StrictModel):
    caller_id: Identifier
    tenant_id: Identifier
    token_env: EnvironmentName
    allowed_sources: list[Identifier] | None = Field(default=None, min_length=1, max_length=1_000)
    all_sources: Literal[True] | None = None
    operator: bool = False

    @model_validator(mode="after")
    def valid_source_scope(self) -> _Caller:
        if (self.allowed_sources is None) == (self.all_sources is None):
            raise ValueError("configure exactly one of allowed_sources or all_sources: true")
        return self


class _PolicyFile(_StrictModel):
    version: int
    callers: list[_Caller] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def valid_policy(self) -> _PolicyFile:
        if self.version != 1:
            raise ValueError("version must be 1")
        caller_ids = [caller.caller_id for caller in self.callers]
        token_envs = [caller.token_env for caller in self.callers]
        if len(set(caller_ids)) != len(caller_ids):
            raise ValueError("caller_id values must be unique")
        if len(set(token_envs)) != len(token_envs):
            raise ValueError("token_env values must be unique")
        if any(
            caller.allowed_sources is not None
            and len(set(caller.allowed_sources)) != len(caller.allowed_sources)
            for caller in self.callers
        ):
            raise ValueError("allowed_sources values must be unique per caller")
        return self


class AccessPolicy:
    def __init__(
        self,
        credentials: Iterable[_Credential],
        *,
        anonymous: CallerContext | None = None,
    ) -> None:
        self._credentials = tuple(credentials)
        self._anonymous = anonymous

    @classmethod
    def local(cls, source_ids: Iterable[str]) -> AccessPolicy:
        return cls(
            (),
            anonymous=CallerContext(
                caller_id="local-development",
                tenant_id="local-development",
                allowed_sources=frozenset(source_ids),
                operator=True,
                all_sources=True,
            ),
        )

    @classmethod
    def legacy(cls, token: str, source_ids: Iterable[str]) -> AccessPolicy:
        caller = CallerContext(
            caller_id="legacy-api-token",
            tenant_id="default",
            allowed_sources=frozenset(source_ids),
            operator=True,
            all_sources=True,
        )
        return cls((_Credential(_digest(token), caller),))

    @classmethod
    def load(
        cls,
        path: Path,
        known_sources: Iterable[str],
        environment: Mapping[str, str] | None = None,
    ) -> AccessPolicy:
        try:
            with path.open(encoding="utf-8") as stream:
                parsed = _PolicyFile.model_validate(yaml.safe_load(stream))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise AccessPolicyConfigurationError(f"Invalid access policy in {path}: {error}") from error

        source_ids = frozenset(known_sources)
        values = os.environ if environment is None else environment
        credentials: list[_Credential] = []
        seen_digests: set[bytes] = set()
        for configured in parsed.callers:
            allowed_sources = frozenset(configured.allowed_sources or ())
            unknown = allowed_sources - source_ids
            if unknown:
                raise AccessPolicyConfigurationError(
                    f"Access policy caller {configured.caller_id} references unknown sources"
                )
            token = values.get(configured.token_env, "")
            if len(token) < 32:
                raise AccessPolicyConfigurationError(
                    f"Access policy requires a 32-character token in {configured.token_env}"
                )
            digest = _digest(token)
            if digest in seen_digests:
                raise AccessPolicyConfigurationError("Access policy tokens must be unique")
            seen_digests.add(digest)
            credentials.append(
                _Credential(
                    digest,
                    CallerContext(
                        caller_id=configured.caller_id,
                        tenant_id=configured.tenant_id,
                        allowed_sources=allowed_sources,
                        operator=configured.operator,
                        all_sources=configured.all_sources is True,
                    ),
                )
            )
        return cls(credentials)

    def authenticate(self, token: str | None) -> CallerContext | None:
        if token is None:
            return self._anonymous
        if not 32 <= len(token) <= 512:
            return None
        received = _digest(token)
        matched: CallerContext | None = None
        for credential in self._credentials:
            if hmac.compare_digest(received, credential.token_digest):
                matched = credential.caller
        return matched

    def require_source(self, caller: CallerContext, source_id: str) -> None:
        if not caller.all_sources and source_id not in caller.allowed_sources:
            raise SourceNotFoundError


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()
