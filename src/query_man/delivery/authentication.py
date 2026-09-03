from __future__ import annotations

from query_man.delivery.access import AccessPolicy, CallerContext


class InvalidBearerTokenError(Exception):
    pass


class AccessPolicyBearerAuthenticator:
    def __init__(self, policy: AccessPolicy) -> None:
        self._policy = policy

    async def authenticate(self, token: str | None) -> CallerContext:
        caller = self._policy.authenticate(token)
        if caller is None:
            raise InvalidBearerTokenError
        return caller
