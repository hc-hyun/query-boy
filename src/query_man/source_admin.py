from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final, Protocol

from query_man.errors import (
    AppError,
    MetadataRevisionMismatchError,
    MetadataUnavailableError,
    MutationNotFoundError,
    QueryInvalidError,
    QueryRejectedError,
    SourceControlUnavailableError,
    SourceNotFoundError,
    SourceValidationError,
)
from query_man.errors import (
    MutationIdempotencyConflictError as MutationIdempotencyConflictAppError,
)
from query_man.errors import SourceGenerationConflictError as SourceGenerationConflictAppError
from query_man.metadata import MetadataService
from query_man.metadata_store import MetadataStore
from query_man.models import BudgetProfile, CatalogProvider, PreparedMetadata, SourceProfile
from query_man.operations import operations
from query_man.quality_level import assess_quality_level
from query_man.query import QueryService
from query_man.reader_policy import ReaderSessionPolicyError
from query_man.registry import (
    RegistryConfigurationError,
    SourceProjectionWriter,
    SourceReader,
    SourceRegistry,
    validate_source_manifest,
)
from query_man.secrets import EncryptedSecret, SecretDecryptionError, SourceSecretCipher
from query_man.source_store import (
    POSTGRES_BIGINT_MAX,
    MutationIdempotencyConflictError,
    MutationPage,
    MutationReceipt,
    MutationReplay,
    MutationRequest,
    SourceCatalogPage,
    SourceCatalogRecord,
    SourceGenerationConflictError,
    SourceGenerationPage,
    SourcePublishPinnedError,
    StoredSource,
    StoredSourceNotFoundError,
)
from query_man.sql_validation import SQL_POLICY_REVISION, SqlValidationError, validate_sql
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash

logger = logging.getLogger("query_man.source_control")
CONTROL_SEQUENCE_MAX: Final[int] = POSTGRES_BIGINT_MAX


@dataclass(frozen=True)
class VerifiedExpectedInput:
    columns: tuple[str, ...]
    row_count: int
    result_hash: str


@dataclass(frozen=True)
class PublishVerifiedQueryInput:
    query_id: str
    source_id: str
    question: str
    sql: str
    metadata_revision: str
    relations: tuple[str, ...]
    expected: VerifiedExpectedInput


class SourceStore(Protocol):
    async def list_active(self) -> list[StoredSource]: ...

    async def get_active(self, source_id: str) -> StoredSource | None: ...

    async def get_revision(self, source_id: str, generation: int) -> StoredSource: ...

    async def list_catalog(
        self,
        *,
        after_source_id: str | None = None,
        limit: int = 50,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> SourceCatalogPage: ...

    async def get_catalog(self, source_id: str) -> SourceCatalogRecord | None: ...

    async def list_generation_history(
        self,
        source_id: str,
        *,
        before_generation: int | None = None,
        limit: int = 50,
    ) -> SourceGenerationPage | None: ...

    async def get_mutation(self, idempotency_key: str) -> MutationReceipt | None: ...

    async def list_mutations(
        self,
        source_id: str,
        *,
        before_event_id: int | None = None,
        limit: int = 50,
    ) -> MutationPage | None: ...

    async def record_mutation_rejection(
        self,
        mutation: MutationRequest,
        *,
        http_status: int,
        error_code: str,
    ) -> MutationReceipt: ...

    async def next_generation(self, source_id: str) -> int: ...

    async def publish(
        self,
        source_id: str,
        expected_generation: int,
        generation: int,
        manifest: dict[str, object],
        encrypted_secret: EncryptedSecret,
        metadata: PreparedMetadata,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> StoredSource: ...

    async def deactivate(
        self,
        source_id: str,
        expected_generation: int,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> int: ...

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> StoredSource: ...

    async def close(self) -> None: ...

    async def publish_verified_query(
        self,
        query: VerifiedQuery,
        *,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> None: ...

    async def resume_metadata_publish(
        self,
        source_id: str,
        expected_metadata_revision: str,
        *,
        mutation: MutationRequest,
        mutation_result: dict[str, object],
    ) -> None: ...

    async def verified_revision_map(self) -> dict[str, frozenset[str]]: ...


class SourcePoolInvalidator(Protocol):
    async def invalidate(self, source_id: str) -> None: ...


@dataclass(frozen=True)
class MutationContext:
    idempotency_key: str
    actor: str
    reason: str
    expected_generation: int
    expected_state_version: int
    expected_metadata_revision: str | None = None


class SourceReloader:
    def __init__(
        self,
        registry: SourceProjectionWriter,
        metadata: MetadataService,
        metadata_store: MetadataStore,
        source_store: SourceStore,
        cipher: SourceSecretCipher,
        budgets: Mapping[str, BudgetProfile],
        verified_revisions: dict[str, frozenset[str]],
        invalidators: tuple[SourcePoolInvalidator, ...] = (),
    ) -> None:
        self._registry = registry
        self._metadata = metadata
        self._metadata_store = metadata_store
        self._source_store = source_store
        self._cipher = cipher
        self._budgets = budgets
        self._verified_revisions = verified_revisions
        self._invalidators = invalidators
        self._applied: dict[str, StoredSource] = {}
        self._lock = asyncio.Lock()

    def connection_identity(self, source_id: str) -> tuple[object, ...] | None:
        source = self._registry.get(source_id)
        if source is None:
            return None
        return _profile_connection_identity(source)

    async def sync(self) -> None:
        try:
            records, stored_verified = await asyncio.gather(
                self._source_store.list_active(),
                self._source_store.verified_revision_map(),
            )
        except Exception:
            operations.increment("source_reload_scan_failed")
            operations.set_component_health("source_reload", "unavailable")
            logger.exception("source_reload_scan_failed")
            return
        self._verified_revisions.clear()
        self._verified_revisions.update(stored_verified)
        apply_failed = False
        for record in records:
            if self._applied.get(record.source_id) == record:
                continue
            try:
                await self.apply(record)
            except Exception:
                apply_failed = True
                operations.increment("source_reload_apply_failed", record.source_id)
                logger.exception(
                    "source_reload_rejected source_id=%s generation=%s",
                    record.source_id,
                    record.generation,
                )
        operations.reconcile_sources(self._registry.source_ids())
        operations.set_component_health(
            "source_reload",
            "unavailable" if apply_failed else "healthy",
        )

    async def apply(self, record: StoredSource) -> SourceProfile | None:
        async with self._lock:
            current = self._applied.get(record.source_id)
            if current is not None:
                if record.state_version < current.state_version:
                    raise SourceGenerationConflictError
                if record.state_version == current.state_version:
                    if record != current:
                        raise SourceGenerationConflictError
                    profile = self._registry.get(record.source_id) if record.enabled else None
                    operations.reconcile_sources(self._registry.source_ids())
                    return profile
            if not record.enabled:
                await self._invalidate(record.source_id)
                self._registry.remove(record.source_id)
                self._metadata.invalidate(record.source_id)
                self._applied[record.source_id] = record
                operations.reconcile_sources(self._registry.source_ids())
                return None
            profile = await self.validate(record)
            await self._invalidate(record.source_id)
            self._registry.upsert(profile)
            self._metadata.invalidate(record.source_id)
            self._applied[record.source_id] = record
            operations.reconcile_sources(self._registry.source_ids())
            operations.set_source_health(record.source_id, "initializing")
            await self._probe(profile)
            return profile

    async def validate(self, record: StoredSource) -> SourceProfile:
        self._require_connection_identity(record)
        secret = self._cipher.decrypt(
            record.source_id,
            record.generation,
            record.encrypted_secret,
        )
        validated = validate_source_manifest(
            record.manifest,
            self._budgets,
            secret,
            origin=f"stored source {record.source_id}",
        )
        if validated.profile.source_id != record.source_id:
            raise RegistryConfigurationError("Stored source_id does not match its key")
        metadata = await self._metadata_store.get_revision(
            validated.profile,
            record.metadata_revision,
        )
        quality = assess_quality_level(
            validated.profile,
            metadata.snapshot,
            metadata.revision,
            self._verified_revisions,
        )
        if not quality.publishable:
            raise RegistryConfigurationError("Stored source does not meet its quality level")
        return replace(
            validated.profile,
            control_generation=record.generation,
            control_state_version=record.state_version,
        )

    def _require_connection_identity(self, record: StoredSource) -> None:
        current_identity = self.connection_identity(record.source_id)
        if current_identity is not None and current_identity != _connection_identity(
            record.manifest
        ):
            raise RegistryConfigurationError(
                "A source_id cannot be rebound to a different connection identity"
            )

    async def _invalidate(self, source_id: str) -> None:
        for invalidator in self._invalidators:
            await invalidator.invalidate(source_id)

    async def _probe(self, source: SourceProfile) -> None:
        try:
            async with asyncio.timeout(
                max(1, source.budget.metadata_statement_timeout_ms) / 1_000
            ):
                await self._metadata.get_published(source.source_id)
        except Exception:
            operations.increment("source_reload_metadata_probe_failed", source.source_id)
            operations.set_source_health(source.source_id, "unavailable")
            logger.exception(
                "source_reload_metadata_probe_failed source_id=%s",
                source.source_id,
            )


class SourceAdminService:
    def __init__(
        self,
        store: SourceStore,
        reloader: SourceReloader,
        metadata: MetadataService,
        queries: QueryService,
        cipher: SourceSecretCipher,
        budgets: Mapping[str, BudgetProfile],
        verified_revisions: dict[str, frozenset[str]],
        catalog_factory: Callable[[], CatalogProvider],
    ) -> None:
        self._store = store
        self._reloader = reloader
        self._metadata = metadata
        self._queries = queries
        self._cipher = cipher
        self._budgets = budgets
        self._verified_revisions = verified_revisions
        self._catalog_factory = catalog_factory

    async def list_sources(
        self,
        limit: int = 50,
        after_source_id: str | None = None,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> dict[str, object]:
        try:
            page = await self._store.list_catalog(
                after_source_id=after_source_id,
                limit=limit,
                enabled=enabled,
                owner=owner,
                environment=environment,
                budget_profile=budget_profile,
            )
            return {
                "sources": [_source_summary(record) for record in page.items],
                "next_after_source_id": page.next_after_source_id,
            }
        except Exception as error:
            raise SourceControlUnavailableError from error

    async def get_source(self, source_id: str) -> dict[str, object]:
        try:
            record = await self._store.get_catalog(source_id)
        except StoredSourceNotFoundError as error:
            raise SourceNotFoundError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        if record is None:
            raise SourceNotFoundError
        try:
            return {
                **_source_summary(record),
                "database_migration_ref": record.database_migration_ref,
                "generation_created_at": record.generation_created_at,
                "metadata_activated_at": record.metadata_activated_at,
                "connection": {
                    "host": record.connection.host,
                    "port": record.connection.port,
                    "database": record.connection.database,
                    "user": record.connection.user,
                    "ssl": record.connection.ssl,
                },
                "allowed_schemas": list(record.allowed_schemas),
                "allowed_relation_kinds": list(record.allowed_relation_kinds),
                "tenant_isolation": record.tenant_isolation,
                "semantic_summary": {
                    "default_relation": record.semantic_default_relation,
                    "relation_count": record.semantic_relation_count,
                    "join_count": record.semantic_join_count,
                    "business_term_count": record.semantic_business_term_count,
                    "question_rule_count": record.semantic_question_rule_count,
                    "composition_hint_count": record.semantic_composition_hint_count,
                },
                "effective_budget_limits": self._effective_budget_limits(
                    record.budget_profile
                ),
            }
        except SourceControlUnavailableError:
            raise
        except Exception as error:
            raise SourceControlUnavailableError from error

    async def source_history(
        self,
        source_id: str,
        limit: int = 50,
        before_generation: int | None = None,
    ) -> dict[str, object]:
        try:
            page = await self._store.list_generation_history(
                source_id,
                before_generation=before_generation,
                limit=limit,
            )
            if page is None:
                raise SourceNotFoundError
            current = page.current
            return {
                "source_id": source_id,
                "current": {
                    "generation": current.generation,
                    "enabled": current.enabled,
                    "state_version": current.state_version,
                    "activated_at": current.activated_at,
                    "active_metadata_revision": current.active_metadata_revision,
                    "metadata_pinned": current.metadata_pinned,
                    "metadata_activated_at": current.metadata_activated_at,
                },
                "generations": [
                    _generation_summary(record) for record in page.items
                ],
                "next_before_generation": page.next_before_generation,
            }
        except SourceNotFoundError:
            raise
        except StoredSourceNotFoundError as error:
            raise SourceNotFoundError from error
        except Exception as error:
            raise SourceControlUnavailableError from error

    async def get_mutation(self, idempotency_key: str) -> dict[str, object]:
        try:
            receipt = await self._store.get_mutation(idempotency_key)
        except Exception as error:
            raise SourceControlUnavailableError from error
        if receipt is None:
            raise MutationNotFoundError
        return _mutation_receipt_response(receipt)

    async def source_mutations(
        self,
        source_id: str,
        limit: int = 50,
        before_event_id: int | None = None,
    ) -> dict[str, object]:
        try:
            page = await self._store.list_mutations(
                source_id,
                before_event_id=before_event_id,
                limit=limit,
            )
        except Exception as error:
            raise SourceControlUnavailableError from error
        if page is None:
            raise SourceNotFoundError
        return {
            "source_id": source_id,
            "mutations": [_mutation_receipt_response(item) for item in page.items],
            "next_before_event_id": page.next_before_event_id,
        }

    def _effective_budget_limits(self, profile_name: str) -> dict[str, object]:
        budget = self._budgets.get(profile_name)
        if budget is None:
            raise SourceControlUnavailableError
        return {
            "name": budget.name,
            "version": budget.version,
            "metadata_statement_timeout_ms": budget.metadata_statement_timeout_ms,
            "query_statement_timeout_ms": budget.query_statement_timeout_ms,
            "query_transaction_timeout_ms": budget.query_transaction_timeout_ms,
            "query_queue_timeout_ms": budget.query_queue_timeout_ms,
            "lock_timeout_ms": budget.lock_timeout_ms,
            "work_mem_kb": budget.work_mem_kb,
            "temp_file_limit_kb": budget.temp_file_limit_kb,
            "max_parallel_workers_per_gather": budget.max_parallel_workers_per_gather,
            "jit_enabled": budget.jit_enabled,
            "max_pool_size": budget.max_pool_size,
            "max_concurrent_queries": budget.max_concurrent_queries,
            "max_metadata_relations": budget.max_metadata_relations,
            "max_metadata_columns": budget.max_metadata_columns,
            "max_columns_per_relation": budget.max_columns_per_relation,
            "max_context_columns_per_relation": budget.max_context_columns_per_relation,
            "max_metadata_response_bytes": budget.max_metadata_response_bytes,
            "max_result_rows": budget.max_result_rows,
            "max_result_bytes": budget.max_result_bytes,
            "max_sql_bytes": budget.max_sql_bytes,
            "max_plan_total_cost": budget.max_plan_total_cost,
            "max_plan_rows": budget.max_plan_rows,
            "max_plan_nodes": budget.max_plan_nodes,
        }

    async def publish(
        self,
        source_id: str,
        manifest: object,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        return await self._publish(
            source_id,
            manifest,
            credential,
            mutation=mutation,
            operation="publish_source",
            canonical_payload={"manifest": manifest, "credential": credential},
        )

    async def rotate_credential(
        self,
        source_id: str,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        request = self._mutation_request(
            mutation,
            operation="rotate_credential",
            source_id=source_id,
            payload={"credential": credential},
        )
        if request is not None:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        try:
            current = await self._store.get_active(source_id)
            if current is None or not current.enabled:
                raise StoredSourceNotFoundError
            self._require_expected_state(current, mutation)
        except StoredSourceNotFoundError as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceGenerationConflictAppError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        return await self._publish(
            source_id,
            current.manifest,
            credential,
            mutation=mutation,
            operation="rotate_credential",
            canonical_payload={"credential": credential},
            request=request,
        )

    async def _publish(
        self,
        source_id: str,
        manifest: object,
        credential: str,
        *,
        mutation: MutationContext | None,
        operation: str,
        canonical_payload: dict[str, object],
        request: MutationRequest | None = None,
    ) -> dict[str, object]:
        request_was_preflighted = request is not None
        request = request or self._mutation_request(
            mutation,
            operation=operation,
            source_id=source_id,
            payload=canonical_payload,
        )
        if request is not None and not request_was_preflighted:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        try:
            validated = validate_source_manifest(manifest, self._budgets, credential)
            if validated.profile.source_id != source_id:
                raise RegistryConfigurationError("Path source_id does not match manifest")
            current = await self._store.get_active(source_id)
            self._require_expected_state(current, mutation)
            current_identity = (
                _connection_identity(current.manifest)
                if current is not None
                else self._reloader.connection_identity(source_id)
            )
            if current_identity is not None and current_identity != _profile_connection_identity(
                validated.profile
            ):
                raise RegistryConfigurationError(
                    "A source_id cannot be rebound to a different connection identity"
                )
            expected_generation = 0 if current is None else current.generation
            generation = await self._store.next_generation(source_id)
            metadata = await self._stage(validated.profile)
            quality = assess_quality_level(
                validated.profile,
                metadata.snapshot,
                metadata.revision,
                self._verified_revisions,
            )
            result: dict[str, object] = {
                "status": "published",
                "source_id": source_id,
                "generation": generation,
                "metadata_revision": metadata.revision,
                "quality_level": quality.level,
            }
            encrypted = self._cipher.encrypt(
                source_id,
                generation,
                credential,
            )
            expected_state_version = 0 if current is None else current.state_version
            if request is None:
                record = await self._store.publish(
                    source_id,
                    expected_generation,
                    generation,
                    validated.document,
                    encrypted,
                    metadata,
                    expected_state_version=expected_state_version,
                )
            else:
                record = await self._store.publish(
                    source_id,
                    expected_generation,
                    generation,
                    validated.document,
                    encrypted,
                    metadata,
                    expected_state_version=expected_state_version,
                    mutation=request,
                    mutation_result=result,
                )
        except MutationReplay as replay:
            return self._receipt_or_error(replay.receipt)
        except MutationIdempotencyConflictError as error:
            raise MutationIdempotencyConflictAppError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceGenerationConflictAppError from error
        except (SourcePublishPinnedError, RegistryConfigurationError, SecretDecryptionError) as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except SourceValidationError as error:
            if request is not None:
                return await self._reject_or_replay(request, error)
            raise
        except Exception as error:
            raise SourceControlUnavailableError from error
        await self._apply_after_commit(record)
        if request is not None:
            return await self._completed_mutation(request)
        return result

    async def deactivate(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        request = self._mutation_request(
            mutation,
            operation="deactivate_source",
            source_id=source_id,
            payload={},
        )
        if request is not None:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        result: dict[str, object] = {"status": "deactivated", "source_id": source_id}
        try:
            current = await self._store.get_active(source_id)
            if current is None:
                raise StoredSourceNotFoundError
            self._require_expected_state(current, mutation)
            if request is None:
                state_version = await self._store.deactivate(
                    source_id,
                    current.generation,
                    expected_state_version=current.state_version,
                )
            else:
                state_version = await self._store.deactivate(
                    source_id,
                    current.generation,
                    expected_state_version=current.state_version,
                    mutation=request,
                    mutation_result=result,
                )
            inactive = StoredSource(
                current.source_id,
                current.generation,
                current.manifest,
                current.encrypted_secret,
                current.metadata_revision,
                False,
                state_version,
            )
        except MutationReplay as replay:
            return self._receipt_or_error(replay.receipt)
        except MutationIdempotencyConflictError as error:
            raise MutationIdempotencyConflictAppError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceGenerationConflictAppError from error
        except StoredSourceNotFoundError as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        await self._apply_after_commit(inactive)
        if request is not None:
            return await self._completed_mutation(request)
        return result

    async def publish_verified_query(
        self,
        query_input: PublishVerifiedQueryInput,
        tenant_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        query = VerifiedQuery(
            query_id=query_input.query_id,
            source_id=query_input.source_id,
            question=query_input.question,
            sql=query_input.sql,
            metadata_revision=query_input.metadata_revision,
            relations=query_input.relations,
            expected=ExpectedResult(
                columns=query_input.expected.columns,
                row_count=query_input.expected.row_count,
                result_hash=query_input.expected.result_hash,
            ),
        )
        request = self._mutation_request(
            mutation,
            operation="publish_verified_query",
            source_id=query.source_id,
            payload={
                "query_id": query.query_id,
                "question": query.question,
                "sql": query.sql,
                "metadata_revision": query.metadata_revision,
                "relations": list(query.relations),
                "expected": {
                    "columns": list(query.expected.columns),
                    "row_count": query.expected.row_count,
                    "result_hash": query.expected.result_hash,
                },
            },
        )
        if request is not None:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        result: dict[str, object] = {
            "status": "verified",
            "query_id": query.query_id,
            "source_id": query.source_id,
            "metadata_revision": query.metadata_revision,
            "row_count": query.expected.row_count,
            "result_hash": query.expected.result_hash,
        }
        try:
            if mutation is not None:
                current_source = await self._store.get_active(query.source_id)
                if current_source is None or not current_source.enabled:
                    raise StoredSourceNotFoundError
                self._require_expected_state(current_source, mutation)
            metadata = await self._metadata.get_published(query.source_id)
            if metadata.revision != query.metadata_revision:
                raise SourceValidationError
            validated = validate_sql(
                query.sql,
                allowed_relations=(
                    relation.qualified_name for relation in metadata.snapshot.relations
                ),
            )
            if validated.relations != tuple(sorted(query.relations)):
                raise SourceValidationError
            response = await self._queries.query(
                query.source_id,
                query.sql,
                query.metadata_revision,
                SQL_POLICY_REVISION,
                tenant_id=tenant_id,
            )
            columns = response.get("columns")
            rows = response.get("rows")
            if (
                not isinstance(columns, list)
                or not all(isinstance(column, str) for column in columns)
                or not isinstance(rows, list)
                or response.get("truncated") is not False
                or tuple(columns) != query.expected.columns
                or response.get("row_count") != query.expected.row_count
                or create_result_hash(tuple(columns), rows) != query.expected.result_hash
            ):
                raise SourceValidationError
            if request is None:
                await self._store.publish_verified_query(query)
            else:
                await self._store.publish_verified_query(
                    query,
                    mutation=request,
                    mutation_result=result,
                )
            current = self._verified_revisions.get(query.source_id, frozenset())
            self._verified_revisions[query.source_id] = current | {query.metadata_revision}
        except MutationReplay as replay:
            return self._receipt_or_error(replay.receipt)
        except MutationIdempotencyConflictError as error:
            raise MutationIdempotencyConflictAppError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceValidationError from error
        except StoredSourceNotFoundError as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except SourceValidationError as error:
            if request is not None:
                return await self._reject_or_replay(request, error)
            raise
        except SqlValidationError as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except (
            MetadataRevisionMismatchError,
            QueryInvalidError,
            QueryRejectedError,
        ) as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        if request is not None:
            return await self._completed_mutation(request)
        return result

    async def rollback(
        self,
        source_id: str,
        generation: int,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        request = self._mutation_request(
            mutation,
            operation="rollback_source",
            source_id=source_id,
            payload={"target_generation": generation},
        )
        if request is not None:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        try:
            current = await self._store.get_active(source_id)
            if current is None:
                raise StoredSourceNotFoundError
            self._require_expected_state(current, mutation)
            candidate = await self._store.get_revision(source_id, generation)
            if _connection_identity(current.manifest) != _connection_identity(
                candidate.manifest
            ):
                raise RegistryConfigurationError(
                    "A source_id cannot be rebound to a different connection identity"
                )
            await self._reloader.validate(candidate)
            result: dict[str, object] = {
                "status": "rolled_back",
                "source_id": source_id,
                "generation": candidate.generation,
                "metadata_revision": candidate.metadata_revision,
            }
            if request is None:
                record = await self._store.rollback(
                    source_id,
                    generation,
                    current.generation,
                    expected_state_version=current.state_version,
                )
            else:
                record = await self._store.rollback(
                    source_id,
                    generation,
                    current.generation,
                    expected_state_version=current.state_version,
                    mutation=request,
                    mutation_result=result,
                )
        except MutationReplay as replay:
            return self._receipt_or_error(replay.receipt)
        except MutationIdempotencyConflictError as error:
            raise MutationIdempotencyConflictAppError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceGenerationConflictAppError from error
        except (StoredSourceNotFoundError, RegistryConfigurationError, SecretDecryptionError) as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        await self._apply_after_commit(record)
        if request is not None:
            return await self._completed_mutation(request)
        return result

    async def resume_automatic_publish(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        if mutation is not None and mutation.expected_metadata_revision is None:
            raise SourceValidationError
        request = self._mutation_request(
            mutation,
            operation="resume_metadata_publish",
            source_id=source_id,
            payload={
                "expected_metadata_revision": (
                    None if mutation is None else mutation.expected_metadata_revision
                )
            },
        )
        if request is not None:
            replay = await self._preflight_mutation(request)
            if replay is not None:
                return replay
        result: dict[str, object] = {"status": "resumed", "source_id": source_id}
        try:
            current = await self._store.get_active(source_id)
            if current is None or not current.enabled:
                raise StoredSourceNotFoundError
            self._require_expected_state(current, mutation)
            if request is None:
                await self._metadata.resume_automatic_publish(source_id)
            else:
                if mutation is None:
                    raise SourceValidationError
                expected_revision = mutation.expected_metadata_revision
                if expected_revision is None:
                    raise SourceValidationError
                await self._store.resume_metadata_publish(
                    source_id,
                    expected_revision,
                    mutation=request,
                    mutation_result=result,
                )
                self._metadata.invalidate(source_id)
        except MutationReplay as replay:
            return self._receipt_or_error(replay.receipt)
        except MutationIdempotencyConflictError as error:
            raise MutationIdempotencyConflictAppError from error
        except SourceGenerationConflictError as error:
            if request is not None:
                return await self._reject_or_replay(
                    request,
                    SourceGenerationConflictAppError(),
                )
            raise SourceValidationError from error
        except StoredSourceNotFoundError as error:
            if request is not None:
                return await self._reject_or_replay(request, SourceValidationError())
            raise SourceValidationError from error
        except SourceValidationError as error:
            if request is not None:
                return await self._reject_or_replay(request, error)
            raise
        except Exception as error:
            raise SourceControlUnavailableError from error
        if request is not None:
            return await self._completed_mutation(request)
        return result

    def _mutation_request(
        self,
        context: MutationContext | None,
        *,
        operation: str,
        source_id: str,
        payload: dict[str, object],
    ) -> MutationRequest | None:
        if context is None:
            return None
        envelope: dict[str, object] = {
            "version": 1,
            "idempotency_key": context.idempotency_key,
            "operation": operation,
            "source_id": source_id,
            "actor": context.actor,
            "reason": context.reason,
            "expected_state": {
                "generation": context.expected_generation,
                "state_version": context.expected_state_version,
            },
            "payload": payload,
        }
        if context.expected_metadata_revision is not None:
            envelope["expected_metadata_revision"] = context.expected_metadata_revision
        try:
            canonical = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise SourceValidationError from error
        return MutationRequest(
            idempotency_key=context.idempotency_key,
            request_hash=self._cipher.mutation_request_hash(canonical),
            operation=operation,
            source_id=source_id,
            actor=context.actor,
            reason=context.reason,
            expected_generation=context.expected_generation,
            expected_state_version=context.expected_state_version,
        )

    async def _preflight_mutation(
        self,
        request: MutationRequest,
    ) -> dict[str, object] | None:
        try:
            receipt = await self._store.get_mutation(request.idempotency_key)
        except Exception as error:
            raise SourceControlUnavailableError from error
        if receipt is None:
            return None
        self._require_same_receipt(receipt, request)
        return self._receipt_or_error(receipt)

    async def _completed_mutation(self, request: MutationRequest) -> dict[str, object]:
        try:
            receipt = await self._store.get_mutation(request.idempotency_key)
        except Exception as error:
            raise SourceControlUnavailableError from error
        if receipt is None:
            raise SourceControlUnavailableError
        self._require_same_receipt(receipt, request)
        return self._receipt_or_error(receipt)

    async def _reject_or_replay(
        self,
        request: MutationRequest,
        error: AppError,
    ) -> dict[str, object]:
        try:
            receipt = await self._store.record_mutation_rejection(
                request,
                http_status=error.status_code,
                error_code=error.code,
            )
        except MutationIdempotencyConflictError as conflict:
            raise MutationIdempotencyConflictAppError from conflict
        except Exception as store_error:
            raise SourceControlUnavailableError from store_error
        return self._receipt_or_error(receipt)

    @staticmethod
    def _require_same_receipt(
        receipt: MutationReceipt,
        request: MutationRequest,
    ) -> None:
        if (
            receipt.request_hash != request.request_hash
            or receipt.operation != request.operation
            or receipt.source_id != request.source_id
            or receipt.actor != request.actor
            or receipt.reason != request.reason
            or receipt.expected_generation != request.expected_generation
            or receipt.expected_state_version != request.expected_state_version
        ):
            raise MutationIdempotencyConflictAppError

    def _receipt_or_error(self, receipt: MutationReceipt) -> dict[str, object]:
        if receipt.outcome == "succeeded":
            if receipt.operation == "publish_verified_query":
                metadata_revision = receipt.result.get("metadata_revision")
                if not isinstance(metadata_revision, str):
                    raise SourceControlUnavailableError
                current = self._verified_revisions.get(receipt.source_id, frozenset())
                self._verified_revisions[receipt.source_id] = current | {
                    metadata_revision
                }
            return _mutation_receipt_response(receipt)
        if receipt.error_code == "SOURCE_VALIDATION_FAILED":
            raise SourceValidationError
        if receipt.error_code == "SOURCE_GENERATION_CONFLICT":
            raise SourceGenerationConflictAppError
        raise SourceControlUnavailableError

    @staticmethod
    def _require_expected_state(
        current: StoredSource | None,
        context: MutationContext | None,
    ) -> None:
        if context is None:
            return
        generation = 0 if current is None else current.generation
        state_version = 0 if current is None else current.state_version
        if (
            generation != context.expected_generation
            or state_version != context.expected_state_version
        ):
            raise SourceGenerationConflictError

    async def _apply_after_commit(self, record: StoredSource) -> None:
        try:
            await self._reloader.apply(record)
        except Exception:
            operations.increment("source_admin_apply_failed", record.source_id)
            operations.set_component_health("source_reload", "unavailable")
            logger.exception(
                "source_admin_apply_failed source_id=%s generation=%s",
                record.source_id,
                record.generation,
            )

    async def _stage(self, source: SourceProfile) -> PreparedMetadata:
        catalog = self._catalog_factory()
        registry: SourceReader = SourceRegistry([source])
        service = MetadataService(
            registry,
            catalog,
            verified_revisions=self._verified_revisions,
        )
        with operations.suppress_source_health_updates():
            try:
                return await service.get_published(source.source_id)
            except MetadataUnavailableError as error:
                details = error.details
                if (
                    isinstance(details, dict)
                    and isinstance(details.get("contract_violations"), list)
                ) or isinstance(error.__cause__, ReaderSessionPolicyError):
                    raise SourceValidationError from error
                raise SourceControlUnavailableError from error
            except Exception as error:
                raise SourceControlUnavailableError from error
            finally:
                await catalog.close()


def _connection_identity(manifest: Mapping[str, object]) -> tuple[object, ...]:
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise RegistryConfigurationError("Stored source provenance must be an object")
    connection = manifest.get("connection")
    if not isinstance(connection, dict):
        raise RegistryConfigurationError("Stored source connection must be an object")
    return (
        provenance.get("environment"),
        *(connection.get(key) for key in ("host", "port", "database", "user", "ssl")),
    )


def _profile_connection_identity(source: SourceProfile) -> tuple[object, ...]:
    connection = source.connection
    return (
        source.provenance.environment,
        connection.host,
        connection.port,
        connection.database,
        connection.user,
        connection.ssl,
    )


def _source_summary(record: SourceCatalogRecord) -> dict[str, object]:
    return {
        "source_id": record.source_id,
        "name": record.name,
        "description": record.description,
        "owner": record.owner,
        "environment": record.environment,
        "enabled": record.enabled,
        "generation": record.generation,
        "state_version": record.state_version,
        "activated_at": record.activated_at,
        "budget_profile": record.budget_profile,
        "minimum_quality_level": record.minimum_quality_level,
        "published_metadata_revision": record.published_metadata_revision,
        "active_metadata_revision": record.active_metadata_revision,
        "metadata_pinned": record.metadata_pinned,
    }


def _generation_summary(record: SourceCatalogRecord) -> dict[str, object]:
    return {
        "generation": record.generation,
        "generation_created_at": record.generation_created_at,
        "owner": record.owner,
        "environment": record.environment,
        "database_migration_ref": record.database_migration_ref,
        "budget_profile": record.budget_profile,
        "published_metadata_revision": record.published_metadata_revision,
        "minimum_quality_level": record.minimum_quality_level,
        "is_current": record.is_current,
    }


def _mutation_receipt_response(receipt: MutationReceipt) -> dict[str, object]:
    resulting_state: dict[str, int] | None = None
    if (
        receipt.resulting_generation is not None
        and receipt.resulting_state_version is not None
    ):
        resulting_state = {
            "generation": receipt.resulting_generation,
            "state_version": receipt.resulting_state_version,
        }
    return {
        "event_id": receipt.event_id,
        "idempotency_key": receipt.idempotency_key,
        "request_hash": receipt.request_hash,
        "operation": receipt.operation,
        "source_id": receipt.source_id,
        "actor": receipt.actor,
        "reason": receipt.reason,
        "outcome": receipt.outcome,
        "expected_state": {
            "generation": receipt.expected_generation,
            "state_version": receipt.expected_state_version,
        },
        "resulting_state": resulting_state,
        "http_status": receipt.http_status,
        "error_code": receipt.error_code,
        "result": dict(receipt.result),
        "recorded_at": receipt.recorded_at.isoformat(),
    }
