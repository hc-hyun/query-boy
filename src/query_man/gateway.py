from __future__ import annotations

from query_man.access import AccessPolicy, CallerContext
from query_man.metadata import MetadataService
from query_man.query import QueryService
from query_man.registry import SourceRegistry


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
                if source["source_id"] in caller.allowed_sources
            ]
        }

    async def get_context(
        self,
        caller: CallerContext,
        source_id: str,
        question: str,
        max_objects: int,
    ) -> dict[str, object]:
        self._access.require_source(caller, source_id)
        return await self._metadata.get_context(source_id, question, max_objects)

    async def query(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        metadata_revision: str,
    ) -> dict[str, object]:
        self._access.require_source(caller, source_id)
        return await self._queries.query(source_id, sql, metadata_revision)
