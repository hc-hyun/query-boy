from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from query_man.delivery.access import AccessPolicy
from query_man.delivery.app import build_http_app
from query_man.delivery.gateway import GatewayService
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.operations import operations
from query_man.source_catalog.registry import SourceRegistry

logger = logging.getLogger("query_man")


class _ShutdownDeadline:
    def __init__(
        self,
        grace_ms: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if grace_ms < 0:
            raise ValueError("Shutdown grace must not be negative")
        self._grace_ms = grace_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._deadline: float | None = None

    def begin(self) -> None:
        with self._lock:
            if self._deadline is None:
                self._deadline = self._clock() + self._grace_ms / 1_000

    def remaining_ms(self) -> int:
        self.begin()
        with self._lock:
            deadline = self._deadline
        assert deadline is not None
        remaining = math.ceil((deadline - self._clock()) * 1_000)
        return max(0, min(self._grace_ms, remaining))


class _ShutdownTrigger:
    def __init__(
        self,
        deadline: _ShutdownDeadline,
        stop_query_admission: Callable[[], None],
    ) -> None:
        self._deadline = deadline
        self._stop_query_admission = stop_query_admission
        self._lock = threading.RLock()
        self._started = False

    def __call__(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._deadline.begin()
            operations.set_accepting(False)
            self._stop_query_admission()


class _CleanupErrors:
    def __init__(self) -> None:
        self._first: BaseException | None = None

    async def attempt(
        self,
        step: str,
        cleanup: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await cleanup()
        except BaseException as error:
            if self._first is None:
                self._first = error
            logger.warning("runtime_cleanup_step_failed step=%s", step)

    def attempt_sync(self, step: str, cleanup: Callable[[], None]) -> None:
        try:
            cleanup()
        except BaseException as error:
            if self._first is None:
                self._first = error
            logger.warning("runtime_cleanup_step_failed step=%s", step)

    def raise_first(self) -> None:
        if self._first is not None:
            raise self._first


def build_app(runtime_config: RuntimeConfig) -> FastAPI:
    operations.reset()
    shutdown_deadline = _ShutdownDeadline(runtime_config.shutdown_grace_ms)
    registry = SourceRegistry.load(runtime_config.source_directory, runtime_config.budget_file)
    operations.reconcile_sources(registry.source_ids())
    catalog = PostgresCatalog()
    metadata = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=runtime_config.metadata_cache_ttl_ms,
        max_stale_ms=runtime_config.metadata_max_stale_ms,
        refresh_retry_ms=runtime_config.metadata_retry_delay_ms,
    )
    query_executor = PostgresQueryExecutor()
    shutdown_trigger = _ShutdownTrigger(
        shutdown_deadline,
        query_executor.stop_accepting,
    )
    query_service = QueryService(registry, metadata, query_executor)
    if runtime_config.access_policy_file is not None:
        access_policy = AccessPolicy.load(runtime_config.access_policy_file)
    elif runtime_config.api_token is not None:
        access_policy = AccessPolicy.legacy(runtime_config.api_token)
    else:
        access_policy = AccessPolicy.local()
    gateway = GatewayService(registry, metadata, query_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        cleanup_errors = _CleanupErrors()
        async with AsyncExitStack() as resources:
            resources.push_async_callback(
                cleanup_errors.attempt,
                "catalog",
                catalog.close,
            )
            resources.push_async_callback(
                cleanup_errors.attempt,
                "query_executor",
                query_executor.close,
            )
            await _probe_registered_sources(registry, metadata)
            try:
                yield
            finally:
                cleanup_errors.attempt_sync("shutdown_trigger", shutdown_trigger)
                await cleanup_errors.attempt(
                    "query_drain",
                    lambda: query_executor.drain(shutdown_deadline.remaining_ms()),
                )
                await resources.aclose()
            cleanup_errors.raise_first()

    app = build_http_app(
        access_policy=access_policy,
        gateway=gateway,
        lifespan=lifespan,
    )
    app.state.shutdown_trigger = shutdown_trigger
    return app


async def _probe_registered_sources(
    registry: SourceRegistry,
    metadata: MetadataService,
) -> None:
    async def probe(source_id: str) -> None:
        source = registry.get(source_id)
        if source is None:
            return
        try:
            async with asyncio.timeout(
                max(1, source.budget.metadata_statement_timeout_ms) / 1_000
            ):
                await metadata.get_published(source_id)
        except Exception:
            operations.increment("startup_metadata_probe_failed", source_id)
            operations.set_source_health(source_id, "unavailable")
            logger.exception("startup_metadata_probe_failed source_id=%s", source_id)

    await asyncio.gather(*(probe(source_id) for source_id in registry.source_ids()))
