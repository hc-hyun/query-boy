from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI

import query_man.runtime.composition as composition_module
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import operations
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry

_NORMAL_SHUTDOWN_EVENTS = [
    "probe",
    "body",
    "query_stop_accepting",
    "query_drain",
    "query_executor_close",
    "catalog_close",
]


def _runtime_config(*, shutdown_grace_ms: int = 10_000) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=ROOT_DIRECTORY / "config" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        shutdown_grace_ms=shutdown_grace_ms,
    )


def _raise_failure(failures: dict[str, BaseException], step: str) -> None:
    error = failures.get(step)
    if error is not None:
        raise error


class _RecordingCatalog:
    def __init__(
        self,
        events: list[str],
        failures: dict[str, BaseException],
    ) -> None:
        self._events = events
        self._failures = failures

    async def load(self, _source: object) -> object:
        raise AssertionError("The startup probe is replaced by this test")

    async def close(self) -> None:
        self._events.append("catalog_close")
        _raise_failure(self._failures, "catalog_close")


class _RecordingExecutor:
    def __init__(
        self,
        events: list[str],
        failures: dict[str, BaseException],
    ) -> None:
        self._events = events
        self._failures = failures
        self.accepting = True
        self.drain_timeouts: list[int] = []

    async def execute(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("Query execution is not expected")

    def stop_accepting(self) -> None:
        self.accepting = False
        self._events.append("query_stop_accepting")
        _raise_failure(self._failures, "query_stop_accepting")

    async def drain(self, grace_ms: int) -> None:
        assert operations.public_status() == "shutting_down"
        assert self.accepting is False
        self.drain_timeouts.append(grace_ms)
        self._events.append("query_drain")
        _raise_failure(self._failures, "query_drain")

    async def close(self) -> None:
        self._events.append("query_executor_close")
        _raise_failure(self._failures, "query_executor_close")


class _FakeClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance_ms(self, milliseconds: float) -> None:
        self.current += milliseconds / 1_000


def _build_runtime(
    monkeypatch: pytest.MonkeyPatch,
    failures: dict[str, BaseException] | None = None,
    *,
    shutdown_grace_ms: int = 10_000,
    clock: _FakeClock | None = None,
) -> tuple[FastAPI, list[str], _RecordingExecutor, _FakeClock]:
    events: list[str] = []
    configured_failures = {} if failures is None else failures
    registry = load_test_registry()
    catalog = _RecordingCatalog(events, configured_failures)
    executor = _RecordingExecutor(events, configured_failures)
    active_clock = _FakeClock() if clock is None else clock
    deadline = composition_module._ShutdownDeadline(
        shutdown_grace_ms,
        clock=active_clock,
    )

    def load_registry(
        _cls: type[SourceRegistry],
        source_directory: Path,
        budget_file: Path,
    ) -> SourceRegistry:
        assert source_directory == ROOT_DIRECTORY / "config" / "sources"
        assert budget_file == ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
        return registry

    def build_catalog() -> _RecordingCatalog:
        return catalog

    def build_deadline(grace_ms: int) -> composition_module._ShutdownDeadline:
        assert grace_ms == shutdown_grace_ms
        return deadline

    async def probe(_registry: object, _metadata: object) -> None:
        events.append("probe")
        _raise_failure(configured_failures, "probe")

    monkeypatch.setattr(
        composition_module.SourceRegistry,
        "load",
        classmethod(load_registry),
    )
    monkeypatch.setattr(composition_module, "PostgresCatalog", build_catalog)
    monkeypatch.setattr(composition_module, "PostgresQueryExecutor", lambda: executor)
    monkeypatch.setattr(composition_module, "_ShutdownDeadline", build_deadline)
    monkeypatch.setattr(composition_module, "_probe_registered_sources", probe)

    app = composition_module.build_app(
        _runtime_config(shutdown_grace_ms=shutdown_grace_ms),
    )
    return app, events, executor, active_clock


@pytest.mark.asyncio
async def test_lifespan_probes_before_serving_then_stops_drains_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(100.0)
    app, events, executor, _clock = _build_runtime(
        monkeypatch,
        shutdown_grace_ms=3_000,
        clock=clock,
    )

    async with app.router.lifespan_context(app):
        assert events == ["probe"]
        clock.advance_ms(50_000)
        events.append("body")

    assert executor.drain_timeouts == [3_000]
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_early_shutdown_trigger_is_idempotent_and_bounds_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events, executor, clock = _build_runtime(
        monkeypatch,
        shutdown_grace_ms=1_000,
    )

    async with app.router.lifespan_context(app):
        events.append("body")
        app.state.shutdown_trigger()
        app.state.shutdown_trigger()
        clock.advance_ms(2_000)

    assert executor.drain_timeouts == [0]
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_probe_failure_prevents_serving_but_closes_both_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_error = RuntimeError("probe failed")
    app, events, _executor, _clock = _build_runtime(
        monkeypatch,
        {
            "probe": startup_error,
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("The application must not start")

    assert raised.value is startup_error
    assert events == ["probe", "query_executor_close", "catalog_close"]


@pytest.mark.asyncio
async def test_first_cleanup_error_is_surfaced_after_every_cleanup_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_error = RuntimeError("stop accepting failed")
    app, events, _executor, _clock = _build_runtime(
        monkeypatch,
        {
            "query_stop_accepting": stop_error,
            "query_drain": RuntimeError("drain failed"),
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")

    assert raised.value is stop_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_body_error_wins_while_every_cleanup_step_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_error = RuntimeError("body failed")
    app, events, _executor, _clock = _build_runtime(
        monkeypatch,
        {
            "query_stop_accepting": RuntimeError("stop accepting failed"),
            "query_drain": RuntimeError("drain failed"),
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")
            raise body_error

    assert raised.value is body_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_preserved_after_resource_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, events, executor, _clock = _build_runtime(monkeypatch)
    drain_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def drain(_grace_ms: int) -> None:
        assert operations.public_status() == "shutting_down"
        assert executor.accepting is False
        events.append("query_drain")
        drain_started.set()
        await wait_forever.wait()

    monkeypatch.setattr(executor, "drain", drain)

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            events.append("body")

    task = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(drain_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert events == _NORMAL_SHUTDOWN_EVENTS
