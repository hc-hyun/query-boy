from __future__ import annotations

from collections.abc import Callable
from types import FrameType

import uvicorn
from dotenv import load_dotenv

from query_man.app import build_app
from query_man.operations import configure_logging, operations
from query_man.runtime_config import load_runtime_config

load_dotenv()
runtime_config = load_runtime_config()
configure_logging(runtime_config.log_level)
app = build_app(runtime_config)


class _QueryManServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, stop_accepting: Callable[[], None]) -> None:
        super().__init__(config)
        self._stop_accepting = stop_accepting

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        operations.set_accepting(False)
        self._stop_accepting()
        super().handle_exit(sig, frame)


def main() -> None:
    config = uvicorn.Config(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level,
        timeout_graceful_shutdown=(runtime_config.shutdown_grace_ms + 999) // 1_000,
    )
    try:
        _QueryManServer(config, app.state.query_executor.stop_accepting).run()
    except KeyboardInterrupt:  # Uvicorn re-raises a captured SIGINT after graceful shutdown.
        pass
