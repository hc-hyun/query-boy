from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

import query_man.runtime.server as server_module
from query_man.runtime.operations import operations
from tests.helpers import ROOT_DIRECTORY


def test_static_app_import_does_not_load_managed_package() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import query_man.runtime.composition; "
                "assert not any(name == 'query_man.managed' or "
                "name.startswith('query_man.managed.') for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_static_server_dispatch_does_not_load_managed_package() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUERY_MAN_")
    }
    environment.update(
        {
            "DEVELOPMENT_ISSUES_READER_PASSWORD": "static-import-test",
            "MARKET_VOC_READER_PASSWORD": "static-import-test",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import query_man.runtime.server; "
                "assert not any(name == 'query_man.managed' or "
                "name.startswith('query_man.managed.') for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=ROOT_DIRECTORY,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("grace_ms", "expected_seconds"),
    [(0, 0), (1, 1), (1_000, 1), (1_001, 2)],
)
def test_main_configures_ceil_uvicorn_shutdown_timeout(
    monkeypatch: pytest.MonkeyPatch,
    grace_ms: int,
    expected_seconds: int,
) -> None:
    captured: dict[str, object] = {}

    class RecordingServer:
        def __init__(
            self,
            config: uvicorn.Config,
            stop_accepting: Callable[[], None],
        ) -> None:
            captured["config"] = config
            captured["stop_accepting"] = stop_accepting

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(
        server_module,
        "runtime_config",
        replace(server_module.runtime_config, shutdown_grace_ms=grace_ms),
    )
    monkeypatch.setattr(server_module, "_QueryManServer", RecordingServer)

    server_module.main()

    config = captured["config"]
    assert isinstance(config, uvicorn.Config)
    assert config.timeout_graceful_shutdown == expected_seconds
    assert callable(captured["stop_accepting"])
    assert captured["ran"] is True


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
        executor_stopped.set,
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
