from __future__ import annotations

import asyncio
import logging
import uuid

from query_man.delivery.access import CallerContext, caller_audit_fields
from query_man.errors import AppError, OperatorRequiredError, QueryNotFoundError, SourceNotFoundError
from query_man.guarded_query.query import QueryService
from query_man.metadata.service import MetadataService
from query_man.runtime.operations import operations
from query_man.source_catalog.registry import SourceRegistry

logger = logging.getLogger("query_man.audit")


class GatewayService:
    def __init__(
        self,
        registry: SourceRegistry,
        metadata: MetadataService,
        queries: QueryService,
    ) -> None:
        self._registry = registry
        self._metadata = metadata
        self._queries = queries

    def list_sources(self, _caller: CallerContext) -> dict[str, object]:
        return {"sources": self._registry.list()}

    async def get_context(
        self,
        caller: CallerContext,
        source_id: str,
    ) -> dict[str, object]:
        self._require_source(caller, source_id, "get_context")
        return await self._metadata.get_context(source_id)

    async def query(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        metadata_revision: str,
        sql_policy_revision: str,
    ) -> dict[str, object]:
        self._require_source(caller, source_id, "query")
        operations.increment("query_request_started", source_id)
        query_id = str(uuid.uuid4())
        logger.info(
            "query_started",
            extra=_audit_extra(
                caller,
                query_id=query_id,
                source_id=source_id,
            ),
        )
        try:
            result = await self._queries.query(
                source_id,
                sql,
                metadata_revision,
                sql_policy_revision,
                query_id=query_id,
            )
        except asyncio.CancelledError:
            logger.info(
                "query_interrupted",
                extra=_audit_extra(
                    caller,
                    query_id=query_id,
                    source_id=source_id,
                    cancel_reason="interrupted",
                ),
            )
            raise
        except AppError as error:
            reason_code = (
                error.details.get("reason_code")
                if isinstance(error.details, dict)
                else None
            )
            logger.info(
                "query_failed",
                extra=_audit_extra(
                    caller,
                    query_id=query_id,
                    source_id=source_id,
                    error_code=error.code,
                    reason_code=reason_code,
                ),
            )
            raise
        except Exception:
            logger.exception(
                "query_failed",
                extra=_audit_extra(
                    caller,
                    query_id=query_id,
                    source_id=source_id,
                    error_code="INTERNAL_ERROR",
                ),
            )
            raise

        plan = result.get("plan_summary")
        total_cost = plan.get("total_cost") if isinstance(plan, dict) else None
        max_rows = plan.get("max_rows") if isinstance(plan, dict) else None
        node_count = plan.get("node_count") if isinstance(plan, dict) else None
        logger.info(
            "query_succeeded",
            extra=_audit_extra(
                caller,
                query_id=query_id,
                source_id=source_id,
                fingerprint=result.get("fingerprint"),
                queue_ms=result.get("queue_ms"),
                elapsed_ms=result.get("elapsed_ms"),
                row_count=result.get("row_count"),
                result_bytes=result.get("result_bytes"),
                truncated=result.get("truncated"),
                plan_total_cost=total_cost,
                plan_max_rows=max_rows,
                plan_node_count=node_count,
            ),
        )
        return result

    async def cancel_query(self, caller: CallerContext, query_id: str) -> dict[str, str]:
        if not caller.operator:
            logger.warning(
                "authorization_denied",
                extra=_audit_extra(caller, operation="cancel_query"),
            )
            raise OperatorRequiredError
        cancelled = await self._queries.cancel(query_id)
        if not cancelled:
            raise QueryNotFoundError
        logger.info(
            "query_cancel_requested",
            extra=_audit_extra(
                caller,
                query_id=query_id,
                cancel_reason="operator",
            ),
        )
        return {"status": "cancel_requested", "query_id": query_id}

    def _require_source(
        self,
        caller: CallerContext,
        source_id: str,
        operation: str,
    ) -> None:
        if self._registry.get(source_id) is not None:
            return
        logger.warning(
            "authorization_denied",
            extra=_audit_extra(caller, operation=operation),
        )
        raise SourceNotFoundError

def _audit_extra(caller: CallerContext, **fields: object) -> dict[str, object]:
    return {**caller_audit_fields(caller), **fields}
