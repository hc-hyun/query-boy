from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from query_man.errors import MetadataUnavailableError, SourceNotFoundError
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    SQL_POLICY_REVISION,
)
from query_man.metadata.catalog import PostgresCatalog, _CatalogValidationError
from query_man.metadata.models import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
    PreparedMetadata,
)
from query_man.metadata.revision import (
    create_metadata_revision,
    create_view_structure_signature,
)
from query_man.runtime.operations import operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.reader_policy import ReaderSessionPolicyError
from query_man.source_catalog.registry import SourceRegistry


@dataclass
class _CacheEntry:
    value: PreparedMetadata
    loaded_at: int
    expires_at: int
    next_refresh_at: int


class MetadataService:
    def __init__(
        self,
        registry: SourceRegistry,
        catalog: PostgresCatalog,
        *,
        cache_ttl_ms: int = 30_000,
        max_stale_ms: int = 300_000,
        refresh_retry_ms: int = 5_000,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._cache_ttl_ms = cache_ttl_ms
        self._max_stale_ms = max_stale_ms
        self._refresh_retry_ms = refresh_retry_ms
        self._now = now or (lambda: time.monotonic_ns() // 1_000_000)
        self._cache: dict[str, _CacheEntry] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._view_structure_signatures: dict[tuple[str, int], str] = {}

    async def get_context(self, source_id: str) -> dict[str, object]:
        source = self._registry.get(source_id)
        if source is None:
            raise SourceNotFoundError
        prepared, stale = await self._get_prepared(source)
        return _context_response(source, prepared, stale=stale)

    async def get_published(self, source_id: str) -> PreparedMetadata:
        source = self._registry.get(source_id)
        if source is None:
            raise SourceNotFoundError
        prepared, _stale = await self._get_prepared(source)
        return prepared

    async def _get_prepared(self, source: SourceProfile) -> tuple[PreparedMetadata, bool]:
        # Keep refresh owned by its request; cancelling a lock waiter cannot cancel the loader.
        lock = self._refresh_locks.setdefault(source.source_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(source.source_id)
            now = self._now()
            if cached and cached.expires_at > now:
                operations.set_source_health(source.source_id, "healthy")
                return cached.value, False
            if cached and cached.next_refresh_at > now:
                if now - cached.loaded_at <= self._max_stale_ms:
                    operations.increment("metadata_stale_served", source.source_id)
                    operations.set_source_health(source.source_id, "stale")
                    return cached.value, True
                operations.set_source_health(source.source_id, "unavailable")
                raise MetadataUnavailableError
            try:
                return await self._load_and_validate(source), False
            except MetadataUnavailableError:
                self._cache.pop(source.source_id, None)
                operations.set_source_health(source.source_id, "unavailable")
                raise
            except (ReaderSessionPolicyError, _CatalogValidationError) as error:
                self._cache.pop(source.source_id, None)
                operations.increment("metadata_refresh_failed", source.source_id)
                operations.set_source_health(source.source_id, "unavailable")
                raise MetadataUnavailableError from error
            except Exception as error:
                failed_at = self._now()
                if cached and failed_at - cached.loaded_at <= self._max_stale_ms:
                    cached.next_refresh_at = failed_at + self._refresh_retry_ms
                    operations.increment("metadata_refresh_failed", source.source_id)
                    operations.increment("metadata_stale_served", source.source_id)
                    operations.set_source_health(source.source_id, "stale")
                    return cached.value, True
                operations.increment("metadata_refresh_failed", source.source_id)
                operations.set_source_health(source.source_id, "unavailable")
                raise MetadataUnavailableError from error

    async def _load_and_validate(
        self,
        source: SourceProfile,
    ) -> PreparedMetadata:
        operations.increment("metadata_refresh_started", source.source_id)
        snapshot = await self._catalog.load(source)
        issues = _validate_snapshot(source, snapshot)
        signature_key = (source.source_id, source.view_contract_version)
        structure_signature = create_view_structure_signature(snapshot)
        accepted_signature = self._view_structure_signatures.get(signature_key)
        if accepted_signature is not None and accepted_signature != structure_signature:
            issues.append("View structure changed without a view contract version change.")
        if issues:
            operations.increment("metadata_validation_rejected", source.source_id)
            operations.set_source_health(source.source_id, "unavailable")
            raise MetadataUnavailableError({"contract_violations": issues})

        candidate = PreparedMetadata(
            snapshot,
            create_metadata_revision(source, snapshot),
        )
        # Use the exact public projection before publishing, including startup/query-only reads.
        _context_response(source, candidate, stale=False)
        self._view_structure_signatures[signature_key] = structure_signature
        self._cache_value(source.source_id, candidate)
        operations.increment("metadata_refresh_succeeded", source.source_id)
        operations.set_source_health(source.source_id, "healthy")
        return candidate

    def _cache_value(self, source_id: str, value: PreparedMetadata) -> None:
        loaded_at = self._now()
        self._cache[source_id] = _CacheEntry(
            value=value,
            loaded_at=loaded_at,
            expires_at=loaded_at + self._cache_ttl_ms,
            next_refresh_at=loaded_at + self._cache_ttl_ms,
        )


def _context_response(source: SourceProfile, prepared: PreparedMetadata, *, stale: bool) -> dict[str, object]:
    relations = [
        _to_relation_response(relation, source.budget.max_context_columns_per_relation)
        for relation in sorted(prepared.snapshot.relations, key=lambda item: item.qualified_name)
    ]
    response: dict[str, object] = {
        "source_id": source.source_id,
        "source_name": source.name,
        "source_description": source.description,
        "metadata_revision": prepared.revision,
        "sql_policy_revision": SQL_POLICY_REVISION,
        "snapshot_status": "stale" if stale else "fresh",
        "sql_capabilities": {
            "functions": sorted(DEFAULT_ALLOWED_FUNCTIONS),
            "cast_types": sorted(DEFAULT_ALLOWED_TYPES),
            "unqualified_cast_types": sorted(DEFAULT_ALLOWED_UNQUALIFIED_TYPES),
        },
        "relations": relations,
        "truncated": any(bool(relation["columns_truncated"]) for relation in relations),
    }
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > source.budget.max_metadata_response_bytes:
        raise MetadataUnavailableError({"contract_violations": ["Metadata response exceeds its byte limit."]})
    return response


def _validate_snapshot(source: SourceProfile, snapshot: CatalogSnapshot) -> list[str]:
    if not snapshot.relations:
        return ["No selectable relations were discovered in the allowed schemas."]

    issues: list[str] = []
    relation_names = [relation.qualified_name for relation in snapshot.relations]
    if len(set(relation_names)) != len(relation_names):
        issues.append("Catalog contains duplicate relations.")
    for relation in snapshot.relations:
        if relation.schema not in source.allowed_schemas or relation.kind not in source.allowed_relation_kinds:
            issues.append(f"Catalog relation is outside the source allowlist: {relation.qualified_name}")
        if relation.view_contract_source != source.source_id:
            issues.append(f"View contract source does not match the source: {relation.qualified_name}")
        if relation.view_contract_version != source.view_contract_version:
            issues.append(f"View contract version does not match the source: {relation.qualified_name}")
        if not relation.comment:
            issues.append(f"Missing view description: {relation.qualified_name}")
    return issues


def _to_relation_response(
    relation: CatalogRelation,
    max_columns: int,
) -> dict[str, object]:
    ordered_columns = sorted(
        relation.columns,
        key=lambda column: (column.ordinal, column.name),
    )
    columns = ordered_columns[:max_columns]
    return {
        "name": relation.qualified_name,
        "sql_name": relation.sql_name,
        "kind": relation.kind,
        "description": relation.comment,
        "column_count": len(ordered_columns),
        "returned_column_count": len(columns),
        "columns_truncated": len(columns) < len(ordered_columns),
        "columns": [_to_column_response(column) for column in columns],
    }


def _to_column_response(column: CatalogColumn) -> dict[str, object]:
    return {
        "name": column.name,
        "sql_name": column.sql_name,
        "ordinal": column.ordinal,
        "data_type": column.data_type,
        "nullable": column.nullable,
        "description": column.comment,
    }
