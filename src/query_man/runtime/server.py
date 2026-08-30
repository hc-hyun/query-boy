from __future__ import annotations

import socket
from collections.abc import Callable
from types import FrameType

import uvicorn
from dotenv import load_dotenv

from query_man.runtime.composition import build_app
from query_man.runtime.config import load_runtime_config
from query_man.runtime.operations import configure_logging, operations

load_dotenv()
runtime_config = load_runtime_config()
configure_logging(runtime_config.log_level)
app = build_app(runtime_config)


class _QueryManServer(uvicorn.Server):
    def __init__(
        self,
        config: uvicorn.Config,
        stop_accepting: Callable[[], None],
        begin_shutdown: Callable[[], None],
    ) -> None:
        super().__init__(config)
        self._stop_accepting = stop_accepting
        self._begin_shutdown = begin_shutdown

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._begin_shutdown()
        operations.set_accepting(False)
        self._stop_accepting()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._begin_shutdown()
        await super().shutdown(sockets=sockets)


def main() -> None:
    config = uvicorn.Config(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level,
        timeout_graceful_shutdown=(runtime_config.shutdown_grace_ms + 999) // 1_000,
    )
    try:
        _QueryManServer(
            config,
            app.state.query_executor.stop_accepting,
            app.state.shutdown_deadline.begin,
        ).run()
    except KeyboardInterrupt:  # Uvicorn re-raises a captured SIGINT after graceful shutdown.
        pass
