from __future__ import annotations

import inspect
from typing import Any, get_type_hints

import pytest
from psycopg import AsyncConnection

import query_man.source_catalog.reader_policy as reader_policy_module
from query_man.source_catalog.reader_policy import (
    READER_CLIENT_ENCODING,
    ReaderSessionPolicyError,
    require_reader_connection_policy,
    require_reader_session_policy,
)
from tests.helpers import load_test_registry


class _ConnectionInfo:
    def __init__(
        self,
        *,
        server_version: int = 180_006,
        server_encoding: str | None = "UTF8",
        client_encoding: str | None = "UTF8",
        encoding: str = "utf-8",
    ) -> None:
        self.server_version = server_version
        self.encoding = encoding
        self._parameters = {
            "server_encoding": server_encoding,
            "client_encoding": client_encoding,
        }

    def parameter_status(self, name: str) -> str | None:
        return self._parameters[name]


class _NoSqlConnection:
    def __init__(self, info: _ConnectionInfo) -> None:
        self.info = info
        self.execute_calls = 0

    def execute(self, *_args: object, **_kwargs: object) -> None:
        self.execute_calls += 1
        raise AssertionError("connection policy must not execute SQL")


class _PolicyCursor:
    async def fetchone(self) -> dict[str, bool]:
        return {"remaining_policy": True}


class _PolicyConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(
        self,
        query: str,
        params: tuple[object, ...],
    ) -> _PolicyCursor:
        self.calls.append((query, params))
        return _PolicyCursor()


def test_reader_connection_policy_interface_has_exact_approved_shape() -> None:
    assert READER_CLIENT_ENCODING == "UTF8"
    assert reader_policy_module.__annotations__["READER_CLIENT_ENCODING"] == "Final"
    assert get_type_hints(require_reader_connection_policy) == {
        "connection": AsyncConnection[Any],
        "return": type(None),
    }
    assert not inspect.iscoroutinefunction(require_reader_connection_policy)
    parameters = tuple(inspect.signature(require_reader_connection_policy).parameters.values())
    assert len(parameters) == 1
    assert parameters[0].name == "connection"
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters[0].default is inspect.Parameter.empty


def test_reader_connection_policy_accepts_postgresql_18_utf8_without_sql() -> None:
    connection = _NoSqlConnection(_ConnectionInfo())

    require_reader_connection_policy(connection)  # type: ignore[arg-type]

    assert connection.execute_calls == 0


@pytest.mark.parametrize(
    "info",
    [
        pytest.param(_ConnectionInfo(server_version=170_999), id="postgres-17"),
        pytest.param(_ConnectionInfo(server_version=190_000), id="postgres-19"),
        pytest.param(
            _ConnectionInfo(server_encoding="SQL_ASCII-private"),
            id="server-sql-ascii",
        ),
        pytest.param(
            _ConnectionInfo(client_encoding="LATIN1-private"),
            id="client-latin1",
        ),
        pytest.param(_ConnectionInfo(encoding="iso8859-1-private"), id="driver-codec"),
        pytest.param(_ConnectionInfo(server_encoding=None), id="missing-server-status"),
        pytest.param(_ConnectionInfo(client_encoding=None), id="missing-client-status"),
    ],
)
def test_reader_connection_policy_rejects_mismatch_without_sql_or_values(
    info: _ConnectionInfo,
) -> None:
    connection = _NoSqlConnection(info)

    with pytest.raises(ReaderSessionPolicyError) as captured:
        require_reader_connection_policy(connection)  # type: ignore[arg-type]

    assert str(captured.value) == "Source reader connection policy mismatch"
    assert "private" not in str(captured.value)
    assert connection.execute_calls == 0


def test_reader_connection_policy_propagates_info_property_error_unchanged() -> None:
    expected = RuntimeError("private connection info failure")

    class FailingConnection:
        execute_calls = 0

        @property
        def info(self) -> object:
            raise expected

        def execute(self, *_args: object, **_kwargs: object) -> None:
            self.execute_calls += 1
            raise AssertionError("connection policy must not execute SQL")

    connection = FailingConnection()

    with pytest.raises(RuntimeError) as captured:
        require_reader_connection_policy(connection)  # type: ignore[arg-type]

    assert captured.value is expected
    assert connection.execute_calls == 0


@pytest.mark.asyncio
async def test_reader_session_policy_does_not_probe_database_temp_privilege() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    connection = _PolicyConnection()

    await require_reader_session_policy(connection, source)  # type: ignore[arg-type]

    assert len(connection.calls) == 1
    query, _params = connection.calls[0]
    assert "has_database_privilege" not in query
    assert "'TEMP'" not in query
    assert "has_schema_privilege" in query
    assert "temp_file_limit" in query
