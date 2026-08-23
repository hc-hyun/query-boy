from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.control_database import (
    DisposableControlDatabase,
    authority_fingerprint,
    disposable_control_database,
    postgres_dsn,
    postgres_environment,
)


@pytest.fixture
async def disposable_control_database_fixture() -> AsyncIterator[DisposableControlDatabase]:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL control-plane credentials are not configured")
    development_dsn = postgres_dsn(environment, environment["POSTGRES_DB"])
    before = await authority_fingerprint(development_dsn)
    async with disposable_control_database(environment) as database:
        yield database
    assert await authority_fingerprint(development_dsn) == before


@pytest.fixture
async def disposable_control_dsn(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> str:
    return disposable_control_database_fixture.dsn
