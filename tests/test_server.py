from __future__ import annotations

import asyncio
import signal
import socket
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

import query_man.runtime.composition as composition_module
import query_man.runtime.server as server_module
from query_man.runtime.composition import _ShutdownDeadline, _ShutdownTrigger
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import OperationalState, operations


def test_server_uses_the_runtime_composition() -> None:
    assert server_module.build_app.__module__ == "query_man.runtime.composition"


@pytest.mark.parametrize(
    ("grace_ms", "expected_seconds"),
    [(0, 0), (1, 1), (1_000, 1), (1_001, 2)],
)
def test_main_configures_ceil_uvicorn_shutdown_timeout(
    monkeypatch: pytest.MonkeyPatch,
    grace_ms: int,
    expected_seconds: int,
) -> None:
    runtime_config = RuntimeConfig(
        host="127.0.0.1",
        port=4321,
        log_level="warning",
        api_token=None,
        source_directory=Path("unused-sources"),
        budget_file=Path("unused-budgets.yaml"),
        database_file=Path("unused-databases.yaml"),
        database_credential_directory=Path("/unused-credentials"),
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        shutdown_grace_ms=grace_ms,
    )
    app = FastAPI()

    def shutdown_trigger() -> None:
        pass

    app.state.shutdown_trigger = shutdown_trigger
    captured: dict[str, object] = {}

    class RecordingServer:
        def __init__(
            self,
            config: uvicorn.Config,
            shutdown_trigger: Callable[[], None],
        ) -> None:
            captured["config"] = config
            captured["shutdown_trigger"] = shutdown_trigger

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(server_module, "load_dotenv", lambda: captured.setdefault("dotenv", True))
    monkeypatch.setattr(server_module, "load_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(
        server_module,
        "configure_logging",
        lambda level: captured.setdefault("log_level", level),
    )
    monkeypatch.setattr(server_module, "build_app", lambda config: app)
    monkeypatch.setattr(server_module, "_QueryManServer", RecordingServer)

    server_module.main()

    config = captured["config"]
    assert isinstance(config, uvicorn.Config)
    assert config.app is app
    assert config.host == "127.0.0.1"
    assert config.port == 4321
    assert config.log_level == "warning"
    assert config.timeout_graceful_shutdown == expected_seconds
    assert captured["dotenv"] is True
    assert captured["log_level"] == "warning"
    assert captured["shutdown_trigger"] is shutdown_trigger
    assert captured["ran"] is True


class _FakeClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance_ms(self, milliseconds: float) -> None:
        self.current += milliseconds / 1_000


@pytest.mark.parametrize(
    ("elapsed_ms", "expected_remaining_ms"),
    [(0.0, 1_000), (250.4, 750), (1_000.0, 0), (2_000.0, 0)],
)
def test_shutdown_deadline_ceil_clamps_full_partial_and_expired_budget(
    elapsed_ms: float,
    expected_remaining_ms: int,
) -> None:
    clock = _FakeClock(100.0)
    deadline = _ShutdownDeadline(1_000, clock=clock)

    deadline.begin()
    clock.advance_ms(elapsed_ms)

    assert deadline.remaining_ms() == expected_remaining_ms


def test_first_signal_owns_deadline_and_repeated_signal_does_not_extend_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(100.0)
    deadline = _ShutdownDeadline(10_000, clock=clock)
    events: list[object] = []
    monkeypatch.setattr(
        operations,
        "set_accepting",
        lambda accepting: events.append(("accepting", accepting)),
    )
    shutdown_trigger = _ShutdownTrigger(
        deadline,
        lambda: events.append("executor_stop"),
    )

    server = server_module._QueryManServer(
        uvicorn.Config(FastAPI(), log_level="critical"),
        shutdown_trigger,
    )

    server.handle_exit(signal.SIGTERM, None)
    clock.advance_ms(4_000)
    server.handle_exit(signal.SIGINT, None)

    assert deadline.remaining_ms() == 6_000
    assert server.should_exit is True
    assert server.force_exit is True
    assert events == [
        ("accepting", False),
        "executor_stop",
    ]


def test_signal_shutdown_update_can_reenter_operational_state_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = OperationalState()
    handled = threading.Event()
    errors: list[BaseException] = []
    monkeypatch.setattr(composition_module, "operations", state)
    shutdown_trigger = _ShutdownTrigger(
        _ShutdownDeadline(10_000),
        lambda: None,
    )
    server = server_module._QueryManServer(
        uvicorn.Config(FastAPI(), log_level="critical"),
        shutdown_trigger,
    )

    def handle_signal_while_state_lock_is_held() -> None:
        try:
            with state._lock:
                server.handle_exit(signal.SIGTERM, None)
        except BaseException as error:
            errors.append(error)
        finally:
            handled.set()

    signal_thread = threading.Thread(
        target=handle_signal_while_state_lock_is_held,
        daemon=True,
    )
    signal_thread.start()

    assert handled.wait(timeout=1)
    signal_thread.join(timeout=1)
    assert signal_thread.is_alive() is False
    assert errors == []
    assert state.public_status() == "shutting_down"


@pytest.mark.asyncio
async def test_programmatic_shutdown_begins_deadline_before_uvicorn_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    deadline = _ShutdownDeadline(10_000, clock=clock)
    events: list[str] = []

    async def uvicorn_shutdown(
        _server: uvicorn.Server,
        sockets: list[socket.socket] | None = None,
    ) -> None:
        assert sockets is None
        events.append("uvicorn_shutdown")
        clock.advance_ms(4_000)

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    monkeypatch.setattr(
        operations,
        "set_accepting",
        lambda accepting: events.append(f"accepting:{accepting}"),
    )
    shutdown_trigger = _ShutdownTrigger(
        deadline,
        lambda: events.append("executor_stop"),
    )

    server = server_module._QueryManServer(
        uvicorn.Config(FastAPI(), log_level="critical"),
        shutdown_trigger,
    )

    await server.shutdown()

    assert events == [
        "accepting:False",
        "executor_stop",
        "uvicorn_shutdown",
    ]
    assert deadline.remaining_ms() == 6_000


@pytest.mark.asyncio
async def test_signal_stops_admission_before_lifespan_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    lifespan_shutdown_started = asyncio.Event()
    executor_stopped = asyncio.Event()
    monkeypatch.setattr(signal, "raise_signal", lambda _signal_number: None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        lifespan_shutdown_started.set()

    app = FastAPI(lifespan=lifespan)

    @app.get("/block")
    async def block() -> dict[str, str]:
        request_started.set()
        await release_request.wait()
        return {"status": "released"}

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = int(server_socket.getsockname()[1])
    server = server_module._QueryManServer(
        uvicorn.Config(
            app,
            log_level="critical",
            lifespan="on",
            timeout_graceful_shutdown=1,
        ),
        _ShutdownTrigger(
            _ShutdownDeadline(1_000),
            executor_stopped.set,
        ),
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    operations.reset()
    request_task: asyncio.Task[httpx.Response] | None = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient() as client:
            request_task = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/block"))
            await asyncio.wait_for(request_started.wait(), timeout=1)

            server.handle_exit(signal.SIGTERM, None)

            assert operations.public_status() == "shutting_down"
            assert executor_stopped.is_set()
            assert not lifespan_shutdown_started.is_set()
            release_request.set()
            assert (await request_task).status_code == 200

        await asyncio.wait_for(server_task, timeout=2)
        assert lifespan_shutdown_started.is_set()
    finally:
        release_request.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        if not server_task.done():
            server.force_exit = True
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        server_socket.close()
        operations.reset()
