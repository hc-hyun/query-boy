from __future__ import annotations

import asyncio
import base64
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType

import pytest
from fastapi import FastAPI

import query_man.runtime.composition as composition_module
from query_man.delivery.access import AccessPolicy
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import operations
from tests.helpers import ROOT_DIRECTORY, load_test_registry

_CAPTURE_KEY = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")
_NORMAL_SHUTDOWN_EVENTS = [
    "probe",
    "capture_start",
    "child_enter",
    "body",
    "query_stop_accepting",
    "query_drain",
    "capture_close",
    "query_executor_close",
    "catalog_close",
    "metadata_close",
    "child_exit",
]


def _runtime_config(
    tmp_path: Path,
    *,
    shutdown_grace_ms: int = 10_000,
) -> RuntimeConfig:
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
        diagnostic_capture_database=tmp_path / "capture.sqlite3",
        diagnostic_capture_key=_CAPTURE_KEY,
        diagnostic_capture_key_id="runtime-cleanup-test",
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

    async def cancel(self, _query_id: str) -> bool:
        return False

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


class _RecordingChildLifespan(AbstractAsyncContextManager[None]):
    def __init__(
        self,
        events: list[str],
        failures: dict[str, BaseException],
    ) -> None:
        self._events = events
        self._failures = failures

    async def __aenter__(self) -> None:
        self._events.append("child_enter")
        _raise_failure(self._failures, "child_enter")

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        self._events.append("child_exit")
        _raise_failure(self._failures, "child_exit")
        return False


def _build_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failures: dict[str, BaseException] | None = None,
    *,
    shutdown_grace_ms: int = 10_000,
    shutdown_deadline: composition_module._ShutdownDeadline | None = None,
) -> tuple[FastAPI, list[str]]:
    events: list[str] = []
    configured_failures = {} if failures is None else failures
    catalog = _RecordingCatalog(events, configured_failures)
    executor = _RecordingExecutor(events, configured_failures)
    if shutdown_deadline is not None:

        def deadline_factory(grace_ms: int) -> composition_module._ShutdownDeadline:
            assert grace_ms == shutdown_grace_ms
            return shutdown_deadline

        monkeypatch.setattr(
            composition_module,
            "_ShutdownDeadline",
            deadline_factory,
        )
    app = composition_module.build_app(
        _runtime_config(tmp_path, shutdown_grace_ms=shutdown_grace_ms),
        registry=load_test_registry(),
        catalog=catalog,  # type: ignore[arg-type]
        query_executor=executor,  # type: ignore[arg-type]
        access_policy=AccessPolicy.local(),
    )

    async def probe(_registry: object, _metadata: object) -> None:
        events.append("probe")
        _raise_failure(configured_failures, "probe")

    async def close_metadata() -> None:
        events.append("metadata_close")
        _raise_failure(configured_failures, "metadata_close")

    capture = app.state.diagnostic_capture
    assert capture is not None

    def start_capture() -> None:
        events.append("capture_start")
        _raise_failure(configured_failures, "capture_start")

    capture_timeouts: list[int] = []

    async def close_capture(timeout_ms: int) -> None:
        capture_timeouts.append(timeout_ms)
        events.append("capture_close")
        _raise_failure(configured_failures, "capture_close")

    def child_lifespan(_app: FastAPI) -> _RecordingChildLifespan:
        return _RecordingChildLifespan(events, configured_failures)

    monkeypatch.setattr(composition_module, "_probe_registered_sources", probe)
    monkeypatch.setattr(app.state.metadata, "close", close_metadata)
    monkeypatch.setattr(capture, "start", start_capture)
    monkeypatch.setattr(capture, "close", close_capture)
    monkeypatch.setattr(
        app.state.mcp_app.router,
        "lifespan_context",
        child_lifespan,
    )
    app.state.capture_close_timeouts = capture_timeouts
    return app, events


class _FakeClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance_ms(self, milliseconds: float) -> None:
        self.current += milliseconds / 1_000


@pytest.mark.asyncio
async def test_direct_lifespan_starts_full_deadline_then_capture_gets_exact_remainder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock(100.0)
    deadline = composition_module._ShutdownDeadline(3_000, clock=clock)
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        shutdown_grace_ms=3_000,
        shutdown_deadline=deadline,
    )
    drain_timeouts: list[int] = []

    async def drain(grace_ms: int) -> None:
        assert operations.public_status() == "shutting_down"
        assert app.state.query_executor.accepting is False
        drain_timeouts.append(grace_ms)
        events.append("query_drain")
        clock.advance_ms(1_250.4)

    monkeypatch.setattr(app.state.query_executor, "drain", drain)

    async with app.router.lifespan_context(app):
        clock.advance_ms(50_000)
        events.append("body")

    assert app.state.shutdown_deadline is deadline
    assert drain_timeouts == [3_000]
    assert app.state.capture_close_timeouts == [1_750]
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_expired_deadline_still_calls_zero_budget_drain_capture_and_all_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    deadline = composition_module._ShutdownDeadline(1_000, clock=clock)
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        shutdown_grace_ms=1_000,
        shutdown_deadline=deadline,
    )

    async with app.router.lifespan_context(app):
        events.append("body")
        deadline.begin()
        clock.advance_ms(2_000)

    assert app.state.query_executor.drain_timeouts == [0]
    assert app.state.capture_close_timeouts == [0]
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_zero_shutdown_grace_still_calls_drain_capture_and_all_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    deadline = composition_module._ShutdownDeadline(0, clock=clock)
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        shutdown_grace_ms=0,
        shutdown_deadline=deadline,
    )

    async with app.router.lifespan_context(app):
        events.append("body")

    assert app.state.query_executor.drain_timeouts == [0]
    assert app.state.capture_close_timeouts == [0]
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_shutdown_trigger_is_idempotent_across_early_and_lifespan_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, events = _build_runtime(monkeypatch, tmp_path)

    async with app.router.lifespan_context(app):
        events.append("body")
        app.state.shutdown_trigger()
        app.state.shutdown_trigger()

    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_startup_failure_lazily_starts_deadline_before_parent_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    startup_error = RuntimeError("capture startup failed")
    clock = _FakeClock()
    deadline = composition_module._ShutdownDeadline(3_000, clock=clock)
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {"capture_start": startup_error},
        shutdown_grace_ms=3_000,
        shutdown_deadline=deadline,
    )
    clock.advance_ms(50_000)

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("The application must not start")

    assert raised.value is startup_error
    assert deadline.remaining_ms() == 3_000
    assert app.state.capture_close_timeouts == [2_000]
    assert events == [
        "probe",
        "capture_start",
        "capture_close",
        "query_executor_close",
        "catalog_close",
        "metadata_close",
    ]


@pytest.mark.asyncio
async def test_capture_start_failure_closes_every_parent_once_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    startup_error = RuntimeError("capture startup failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {
            "capture_start": startup_error,
            "capture_close": RuntimeError("capture close failed"),
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
            "metadata_close": RuntimeError("metadata close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("The application must not start")

    assert raised.value is startup_error
    assert events == [
        "probe",
        "capture_start",
        "capture_close",
        "query_executor_close",
        "catalog_close",
        "metadata_close",
    ]


@pytest.mark.asyncio
async def test_child_enter_failure_closes_parents_without_calling_child_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_error = RuntimeError("child startup failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {
            "child_enter": child_error,
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
            "metadata_close": RuntimeError("metadata close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("The application must not start")

    assert raised.value is child_error
    assert events == [
        "probe",
        "capture_start",
        "child_enter",
        "capture_close",
        "query_executor_close",
        "catalog_close",
        "metadata_close",
    ]


@pytest.mark.asyncio
async def test_shutdown_drain_failure_does_not_skip_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    drain_error = RuntimeError("drain failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {
            "query_drain": drain_error,
            "capture_close": RuntimeError("capture close failed"),
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
            "metadata_close": RuntimeError("metadata close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")

    assert raised.value is drain_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_shutdown_trigger_failure_does_not_skip_drain_or_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stop_error = RuntimeError("stop accepting failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {"query_stop_accepting": stop_error},
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")

    assert raised.value is stop_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.parametrize(
    "failed_step",
    ["query_executor_close", "catalog_close", "metadata_close"],
)
@pytest.mark.asyncio
async def test_shutdown_close_failure_does_not_skip_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_step: str,
) -> None:
    close_error = RuntimeError(f"{failed_step} failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {failed_step: close_error},
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")

    assert raised.value is close_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_capture_close_failure_is_fail_open_and_later_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {"capture_close": RuntimeError("capture close failed")},
    )

    async with app.router.lifespan_context(app):
        events.append("body")

    assert events == _NORMAL_SHUTDOWN_EVENTS
    metrics = {
        metric["name"]: metric["value"]
        for metric in operations.snapshot()["metrics"]
    }
    assert metrics["diagnostic_capture_storage_failed"] == 1


@pytest.mark.asyncio
async def test_body_error_wins_over_every_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_error = RuntimeError("body failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {
            "query_drain": RuntimeError("drain failed"),
            "capture_close": RuntimeError("capture close failed"),
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
            "metadata_close": RuntimeError("metadata close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")
            raise body_error

    assert raised.value is body_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_child_exit_error_wins_over_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_error = RuntimeError("child exit failed")
    app, events = _build_runtime(
        monkeypatch,
        tmp_path,
        {
            "child_exit": child_error,
            "query_executor_close": RuntimeError("query close failed"),
            "catalog_close": RuntimeError("catalog close failed"),
            "metadata_close": RuntimeError("metadata close failed"),
        },
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            events.append("body")

    assert raised.value is child_error
    assert events == _NORMAL_SHUTDOWN_EVENTS


@pytest.mark.asyncio
async def test_shutdown_cancellation_runs_all_cleanup_and_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, events = _build_runtime(monkeypatch, tmp_path)
    drain_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def drain(_grace_ms: int) -> None:
        assert operations.public_status() == "shutting_down"
        assert app.state.query_executor.accepting is False
        events.append("query_drain")
        drain_started.set()
        await wait_forever.wait()

    monkeypatch.setattr(app.state.query_executor, "drain", drain)

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


@pytest.mark.asyncio
async def test_startup_cancellation_closes_every_parent_and_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, events = _build_runtime(monkeypatch, tmp_path)
    probe_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def probe(_registry: object, _metadata: object) -> None:
        events.append("probe")
        probe_started.set()
        await wait_forever.wait()

    monkeypatch.setattr(composition_module, "_probe_registered_sources", probe)

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pytest.fail("The application must not start")

    task = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert events == [
        "probe",
        "capture_close",
        "query_executor_close",
        "catalog_close",
        "metadata_close",
    ]


@pytest.mark.asyncio
async def test_aliased_query_and_catalog_resource_is_closed_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class CombinedProvider(_RecordingExecutor):
        async def load(self, _source: object) -> object:
            raise AssertionError("The startup probe is replaced by this test")

        async def close(self) -> None:
            events.append("combined_close")

    provider = CombinedProvider(events, {})
    app = composition_module.build_app(
        _runtime_config(tmp_path),
        registry=load_test_registry(),
        catalog=provider,  # type: ignore[arg-type]
        query_executor=provider,  # type: ignore[arg-type]
        access_policy=AccessPolicy.local(),
    )

    async def probe(_registry: object, _metadata: object) -> None:
        events.append("probe")

    async def close_metadata() -> None:
        events.append("metadata_close")

    capture = app.state.diagnostic_capture
    assert capture is not None
    monkeypatch.setattr(composition_module, "_probe_registered_sources", probe)
    monkeypatch.setattr(app.state.metadata, "close", close_metadata)
    monkeypatch.setattr(capture, "start", lambda: events.append("capture_start"))

    async def close_capture(_timeout_ms: int) -> None:
        events.append("capture_close")

    monkeypatch.setattr(capture, "close", close_capture)
    monkeypatch.setattr(
        app.state.mcp_app.router,
        "lifespan_context",
        lambda _app: _RecordingChildLifespan(events, {}),
    )

    async with app.router.lifespan_context(app):
        events.append("body")

    assert events == [
        "probe",
        "capture_start",
        "child_enter",
        "body",
        "query_stop_accepting",
        "query_drain",
        "capture_close",
        "combined_close",
        "metadata_close",
        "child_exit",
    ]
