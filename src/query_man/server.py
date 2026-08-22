from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv

from query_man.app import build_app
from query_man.runtime_config import load_runtime_config

load_dotenv()
runtime_config = load_runtime_config()
logging.basicConfig(
    level=runtime_config.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app = build_app(runtime_config)


def main() -> None:
    uvicorn.run(
        app,
        host=runtime_config.host,
        port=runtime_config.port,
        log_level=runtime_config.log_level,
    )
