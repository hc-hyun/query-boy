from __future__ import annotations

import logging
import uuid

from query_man.access import AccessPolicy, CallerContext
from query_man.errors import OperatorRequiredError, QueryNotFoundError, SourceNotFoundError
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
        )
        return await self._queries.query(
            source_id,
            sql,
            metadata_revision,
            query_id=query_id,
            tenant_id=caller.tenant_id,
        )

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
