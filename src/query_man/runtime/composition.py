from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from query_man.assurance.verified import VerifiedQueryRegistry
from query_man.delivery.access import AccessPolicy
from query_man.delivery.app import build_http_app
from query_man.delivery.authentication import (
    BearerAuthenticator,
    OAuth2JWTBearerAuthenticator,
)
from query_man.delivery.gateway import GatewayService
from query_man.guarded_query.query import (
    DeliveryQueryExecutor,
    PostgresQueryExecutor,
    QueryService,
)
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.models import CatalogProvider
from query_man.metadata.service import MetadataService
from query_man.runtime.config import RuntimeConfig
from query_man.runtime.diagnostic_capture import EncryptedDiagnosticCapture
from query_man.runtime.operations import operations
from query_man.source_catalog.registry import SourceReader, SourceRegistry

logger = logging.getLogger("query_man")


def _require_runtime_capabilities(
    component: str,
    provider: object,
    methods: tuple[str, ...],
) -> None:
    missing = tuple(
        method
        for method in methods
        if not callable(getattr(provider, method, None))
    )
    if missing:
        raise TypeError(
            f"{component} is missing required runtime capabilities: {', '.join(missing)}"
        )


def _require_launch_inventory(registry: SourceReader) -> None:
    for source_id in registry.source_ids():
        source = registry.get(source_id)
        if source is not None and source.tenant_isolation == "rls":
            raise ValueError("Runtime launch inventory does not accept RLS sources")


def build_app(
    runtime_config: RuntimeConfig,
    *,
    registry: SourceRegistry | None = None,
    catalog: CatalogProvider | None = None,
    query_executor: DeliveryQueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
    authenticator: BearerAuthenticator | None = None,
) -> FastAPI:
    operations.reset()
    diagnostic_capture: EncryptedDiagnosticCapture | None = None
    if runtime_config.diagnostic_capture_database is not None:
        if (
            runtime_config.diagnostic_capture_key is None
            or runtime_config.diagnostic_capture_key_id is None
        ):
            raise ValueError("Diagnostic capture configuration is incomplete")
        diagnostic_capture = EncryptedDiagnosticCapture.from_base64(
            runtime_config.diagnostic_capture_database,
            runtime_config.diagnostic_capture_key,
            runtime_config.diagnostic_capture_key_id,
            daily_byte_budget=runtime_config.diagnostic_capture_daily_bytes,
        )
    if registry is None:
        registry = SourceRegistry.load(runtime_config.source_directory, runtime_config.budget_file)
    _require_launch_inventory(registry)
    operations.reconcile_sources(registry.source_ids())
    catalog = PostgresCatalog(reject_domain_columns=True) if catalog is None else catalog
    _require_runtime_capabilities(
        "catalog",
        catalog,
        ("load", "close"),
    )
    verified_revisions = VerifiedQueryRegistry.load(
        runtime_config.source_directory.parent / "verified-queries.yaml",
        set(registry.source_ids()),
    ).revision_map()
    metadata = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=runtime_config.metadata_cache_ttl_ms,
        max_stale_ms=runtime_config.metadata_max_stale_ms,
        refresh_retry_ms=runtime_config.metadata_retry_delay_ms,
        verified_revisions=verified_revisions,
    )
    query_executor = PostgresQueryExecutor() if query_executor is None else query_executor
    _require_runtime_capabilities(
        "query_executor",
        query_executor,
        ("execute", "cancel", "close", "stop_accepting", "drain"),
    )
    query_service = QueryService(registry, metadata, query_executor)
    if access_policy is None and authenticator is None:
        if runtime_config.oauth is not None:
            authenticator = OAuth2JWTBearerAuthenticator(runtime_config.oauth)
        elif runtime_config.access_policy_file is not None:
            access_policy = AccessPolicy.load(runtime_config.access_policy_file)
        elif runtime_config.api_token is not None:
            access_policy = AccessPolicy.legacy(runtime_config.api_token)
        else:
            access_policy = AccessPolicy.local()
    if diagnostic_capture is not None:
        if access_policy is None:
            raise ValueError("Diagnostic capture requires an access policy consent authority")
        access_policy = access_policy.with_subject_identifier(diagnostic_capture.subject_id)
    gateway = GatewayService(
        registry,
        metadata,
        query_service,
        diagnostic_capture=diagnostic_capture,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def close_diagnostic_capture() -> None:
            if diagnostic_capture is None:
                return
            try:
                await diagnostic_capture.close(
                    min(runtime_config.shutdown_grace_ms, 2_000)
                )
            except Exception:
                operations.increment("diagnostic_capture_storage_failed")
                logger.exception("diagnostic_capture_shutdown_failed")

        async def cleanup_step(
            step: str,
            cleanup: Callable[[], Awaitable[None]],
        ) -> None:
            try:
                await cleanup()
            except BaseException:
                logger.warning("startup_cleanup_step_failed step=%s", step)

        async def cleanup_failed_startup() -> None:
            cleanup_steps: list[tuple[str, object, Callable[[], Awaitable[None]]]] = []
            if diagnostic_capture is not None:
                cleanup_steps.append(
                    (
                        "diagnostic_capture",
                        diagnostic_capture,
                        close_diagnostic_capture,
                    )
                )
            cleanup_steps.extend([
                ("query_executor", query_executor, query_executor.close),
                ("catalog", catalog, catalog.close),
                ("metadata", metadata, metadata.close),
            ])
            attempted_resources: set[int] = set()
            for step, resource, cleanup in cleanup_steps:
                resource_id = id(resource)
                if resource_id in attempted_resources:
                    continue
                attempted_resources.add(resource_id)
                await cleanup_step(step, cleanup)

        operations.reconcile_sources(registry.source_ids())
        await _probe_registered_sources(registry, metadata)
        if diagnostic_capture is not None:
            diagnostic_capture.start()
        child_entered = False
        try:
            mcp_app: FastAPI = app.state.mcp_app
            async with mcp_app.router.lifespan_context(mcp_app):
                child_entered = True
                try:
                    yield
                finally:
                    operations.set_accepting(False)
                    await query_executor.drain(runtime_config.shutdown_grace_ms)
                    try:
                        if diagnostic_capture is not None:
                            await close_diagnostic_capture()
                    finally:
                        try:
                            await query_executor.close()
                        finally:
                            try:
                                await catalog.close()
                            finally:
                                await metadata.close()
        except BaseException:
            if not child_entered:
                await cleanup_failed_startup()
            raise

    return build_http_app(
        host=runtime_config.host,
        mcp_allowed_hosts=runtime_config.mcp_allowed_hosts,
        mcp_allowed_origins=runtime_config.mcp_allowed_origins,
        registry=registry,
        catalog=catalog,
        metadata=metadata,
        query_executor=query_executor,
        query_service=query_service,
        access_policy=access_policy,
        authenticator=authenticator,
        gateway=gateway,
        lifespan=lifespan,
        extra_state={"diagnostic_capture": diagnostic_capture},
    )


async def _probe_registered_sources(
    registry: SourceReader,
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
