from __future__ import annotations

import asyncio
import logging
import uuid

from query_man.access import AccessPolicy, CallerContext
from query_man.errors import AppError, OperatorRequiredError, QueryNotFoundError, SourceNotFoundError
from query_man.metadata import MetadataService
from query_man.query import QueryService
from query_man.registry import SourceRegistry

logger = logging.getLogger("query_man.audit")


class GatewayService:
    def __init__(
        self,
        registry: SourceRegistry,
        metadata: MetadataService,
        queries: QueryService,
        access: AccessPolicy,
    ) -> None:
        self._registry = registry
        self._metadata = metadata
        self._queries = queries
        self._access = access

    def list_sources(self, caller: CallerContext) -> dict[str, object]:
        return {
            "sources": [
                source
                for source in self._registry.list()
                if caller.all_sources or source["source_id"] in caller.allowed_sources
            ]
        }

    async def get_context(
        self,
        caller: CallerContext,
        source_id: str,
        question: str,
        max_objects: int,
    ) -> dict[str, object]:
        self._require_source(caller, source_id, "get_context")
        return await self._metadata.get_context(source_id, question, max_objects)

    async def query(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        metadata_revision: str,
    ) -> dict[str, object]:
        self._require_source(caller, source_id, "query")
        query_id = str(uuid.uuid4())
        logger.info(
            "query_started query_id=%s caller_id=%s tenant_id=%s source_id=%s",
            query_id,
            caller.caller_id,
            caller.tenant_id,
            source_id,
            extra={
                "query_id": query_id,
                "caller_id": caller.caller_id,
                "tenant_id": caller.tenant_id,
                "source_id": source_id,
            },
        )
        try:
            result = await self._queries.query(
                source_id,
                sql,
                metadata_revision,
                query_id=query_id,
                tenant_id=caller.tenant_id,
            )
        except asyncio.CancelledError:
            logger.info(
                "query_interrupted query_id=%s caller_id=%s tenant_id=%s source_id=%s",
                query_id,
                caller.caller_id,
                caller.tenant_id,
                source_id,
                extra={
                    "query_id": query_id,
                    "caller_id": caller.caller_id,
                    "tenant_id": caller.tenant_id,
                    "source_id": source_id,
                    "cancel_reason": "interrupted",
                },
            )
            raise
        except AppError as error:
            reason_code = (
                error.details.get("reason_code")
                if isinstance(error.details, dict)
                else None
            )
            logger.info(
                "query_failed query_id=%s caller_id=%s tenant_id=%s source_id=%s "
                "error_code=%s reason_code=%s",
                query_id,
                caller.caller_id,
                caller.tenant_id,
                source_id,
                error.code,
                reason_code,
                extra={
                    "query_id": query_id,
                    "caller_id": caller.caller_id,
                    "tenant_id": caller.tenant_id,
                    "source_id": source_id,
                    "error_code": error.code,
                    "reason_code": reason_code,
                },
            )
            raise
        except Exception:
            logger.exception(
                "query_failed query_id=%s caller_id=%s tenant_id=%s source_id=%s "
                "error_code=INTERNAL_ERROR",
                query_id,
                caller.caller_id,
                caller.tenant_id,
                source_id,
                extra={
                    "query_id": query_id,
                    "caller_id": caller.caller_id,
                    "tenant_id": caller.tenant_id,
                    "source_id": source_id,
                    "error_code": "INTERNAL_ERROR",
                },
            )
            raise

        plan = result.get("plan_summary")
        total_cost = plan.get("total_cost") if isinstance(plan, dict) else None
        max_rows = plan.get("max_rows") if isinstance(plan, dict) else None
        node_count = plan.get("node_count") if isinstance(plan, dict) else None
        logger.info(
            "query_succeeded query_id=%s caller_id=%s tenant_id=%s source_id=%s "
            "fingerprint=%s queue_ms=%s elapsed_ms=%s row_count=%s result_bytes=%s "
            "truncated=%s plan_total_cost=%s plan_max_rows=%s plan_node_count=%s",
            query_id,
            caller.caller_id,
            caller.tenant_id,
            source_id,
            result.get("fingerprint"),
            result.get("queue_ms"),
            result.get("elapsed_ms"),
            result.get("row_count"),
            result.get("result_bytes"),
            result.get("truncated"),
            total_cost,
            max_rows,
            node_count,
            extra={
                "query_id": query_id,
                "caller_id": caller.caller_id,
                "tenant_id": caller.tenant_id,
                "source_id": source_id,
                "fingerprint": result.get("fingerprint"),
                "queue_ms": result.get("queue_ms"),
                "elapsed_ms": result.get("elapsed_ms"),
                "row_count": result.get("row_count"),
                "result_bytes": result.get("result_bytes"),
                "truncated": result.get("truncated"),
                "plan_total_cost": total_cost,
                "plan_max_rows": max_rows,
                "plan_node_count": node_count,
            },
        )
        return result

    async def cancel_query(self, caller: CallerContext, query_id: str) -> dict[str, str]:
        if not caller.operator:
            logger.warning(
                "authorization_denied caller_id=%s tenant_id=%s operation=cancel_query",
                caller.caller_id,
                caller.tenant_id,
            )
            raise OperatorRequiredError
        allowed_sources = (
            self._registry.source_ids() if caller.all_sources else caller.allowed_sources
        )
        cancelled = await self._queries.cancel(query_id, allowed_sources)
        if not cancelled:
            raise QueryNotFoundError
        logger.info(
            "query_cancel_requested query_id=%s caller_id=%s tenant_id=%s",
            query_id,
            caller.caller_id,
            caller.tenant_id,
            extra={
                "query_id": query_id,
                "caller_id": caller.caller_id,
                "tenant_id": caller.tenant_id,
                "cancel_reason": "operator",
            },
        )
        return {"status": "cancel_requested", "query_id": query_id}

    def _require_source(
        self,
        caller: CallerContext,
        source_id: str,
        operation: str,
    ) -> None:
        try:
            self._access.require_source(caller, source_id)
        except SourceNotFoundError:
            logger.warning(
                "authorization_denied caller_id=%s tenant_id=%s operation=%s",
                caller.caller_id,
                caller.tenant_id,
                operation,
            )
            raise
