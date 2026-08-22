from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Protocol

from query_man.errors import (
    SourceControlUnavailableError,
    SourceValidationError,
)
from query_man.errors import SourceGenerationConflictError as SourceGenerationConflictAppError
from query_man.metadata import MetadataService
from query_man.metadata_store import MetadataStore
from query_man.models import BudgetProfile, CatalogProvider, PreparedMetadata, SourceProfile
from query_man.quality_level import assess_quality_level
from query_man.query import QueryService
from query_man.registry import (
    RegistryConfigurationError,
    SourceRegistry,
    validate_source_manifest,
)
from query_man.secrets import EncryptedSecret, SecretDecryptionError, SourceSecretCipher
from query_man.source_store import (
    SourceGenerationConflictError,
    SourcePublishPinnedError,
    StoredSource,
    StoredSourceNotFoundError,
)
from query_man.sql_validation import SqlValidationError, validate_sql
from query_man.verified import VerifiedQuery, create_result_hash

logger = logging.getLogger("query_man.source_control")


class SourceStore(Protocol):
    async def list_active(self) -> list[StoredSource]: ...

    async def get_active(self, source_id: str) -> StoredSource | None: ...

    async def get_revision(self, source_id: str, generation: int) -> StoredSource: ...

    async def next_generation(self, source_id: str) -> int: ...

    async def publish(
        self,
        source_id: str,
        expected_generation: int,
        generation: int,
        manifest: dict[str, object],
        encrypted_secret: EncryptedSecret,
        metadata: PreparedMetadata,
    ) -> StoredSource: ...

    async def deactivate(self, source_id: str, expected_generation: int) -> None: ...

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
    ) -> StoredSource: ...

    async def close(self) -> None: ...

    async def publish_verified_query(self, query: VerifiedQuery) -> None: ...

    async def verified_revision_map(self) -> dict[str, frozenset[str]]: ...


class SourcePoolInvalidator(Protocol):
    async def invalidate(self, source_id: str) -> None: ...


class SourceReloader:
    def __init__(
        self,
        registry: SourceRegistry,
        metadata: MetadataService,
        metadata_store: MetadataStore,
        source_store: SourceStore,
        cipher: SourceSecretCipher,
        budgets: Mapping[str, BudgetProfile],
        verified_revisions: Mapping[str, frozenset[str]],
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
        self._applied: dict[str, tuple[int, bool]] = {}
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        try:
            records = await self._source_store.list_active()
        except Exception:
            logger.exception("source_reload_scan_failed")
            return
        for record in records:
            if self._applied.get(record.source_id) == (record.generation, record.enabled):
                continue
            try:
                await self.apply(record)
            except Exception:
                logger.exception(
                    "source_reload_rejected source_id=%s generation=%s",
                    record.source_id,
                    record.generation,
                )

    async def apply(self, record: StoredSource) -> SourceProfile | None:
        async with self._lock:
            if not record.enabled:
                await self._invalidate(record.source_id)
                self._registry.remove(record.source_id)
                self._metadata.invalidate(record.source_id)
                self._applied[record.source_id] = (record.generation, False)
                return None
            profile = await self.validate(record)
            await self._invalidate(record.source_id)
            self._registry.upsert(profile)
            self._metadata.invalidate(record.source_id)
            self._applied[record.source_id] = (record.generation, True)
            return profile

    async def validate(self, record: StoredSource) -> SourceProfile:
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
        return validated.profile

    async def _invalidate(self, source_id: str) -> None:
        for invalidator in self._invalidators:
            await invalidator.invalidate(source_id)


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

    async def publish(
        self,
        source_id: str,
        manifest: object,
        credential: str,
    ) -> dict[str, object]:
        try:
            validated = validate_source_manifest(manifest, self._budgets, credential)
            if validated.profile.source_id != source_id:
                raise RegistryConfigurationError("Path source_id does not match manifest")
            current = await self._store.get_active(source_id)
            expected_generation = 0 if current is None else current.generation
            generation = await self._store.next_generation(source_id)
            metadata = await self._stage(validated.profile)
            encrypted = self._cipher.encrypt(
                source_id,
                generation,
                credential,
            )
            record = await self._store.publish(
                source_id,
                expected_generation,
                generation,
                validated.document,
                encrypted,
                metadata,
            )
            await self._reloader.apply(record)
        except SourceGenerationConflictError as error:
            raise SourceGenerationConflictAppError from error
        except (SourcePublishPinnedError, RegistryConfigurationError, SecretDecryptionError) as error:
            raise SourceValidationError from error
        except SourceValidationError:
            raise
        except Exception as error:
            raise SourceControlUnavailableError from error
        quality = assess_quality_level(
            validated.profile,
            metadata.snapshot,
            metadata.revision,
            self._verified_revisions,
        )
        return {
            "status": "published",
            "source_id": source_id,
            "generation": record.generation,
            "metadata_revision": record.metadata_revision,
            "quality_level": quality.level,
        }

    async def rotate_credential(self, source_id: str, credential: str) -> dict[str, object]:
        try:
            current = await self._store.get_active(source_id)
        except Exception as error:
            raise SourceControlUnavailableError from error
        if current is None:
            raise SourceValidationError
        return await self.publish(source_id, current.manifest, credential)

    async def deactivate(self, source_id: str) -> dict[str, object]:
        try:
            current = await self._store.get_active(source_id)
            if current is None:
                raise StoredSourceNotFoundError
            await self._store.deactivate(source_id, current.generation)
            inactive = StoredSource(
                current.source_id,
                current.generation,
                current.manifest,
                current.encrypted_secret,
                current.metadata_revision,
                False,
            )
            await self._reloader.apply(inactive)
        except SourceGenerationConflictError as error:
            raise SourceGenerationConflictAppError from error
        except StoredSourceNotFoundError as error:
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        return {"status": "deactivated", "source_id": source_id}

    async def publish_verified_query(
        self,
        query: VerifiedQuery,
        tenant_id: str,
    ) -> dict[str, object]:
        try:
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
            await self._store.publish_verified_query(query)
            current = self._verified_revisions.get(query.source_id, frozenset())
            self._verified_revisions[query.source_id] = current | {query.metadata_revision}
        except SourceValidationError:
            raise
        except (SqlValidationError, SourceGenerationConflictError) as error:
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        return {
            "status": "verified",
            "query_id": query.query_id,
            "source_id": query.source_id,
            "metadata_revision": query.metadata_revision,
            "row_count": query.expected.row_count,
            "result_hash": query.expected.result_hash,
        }

    async def rollback(self, source_id: str, generation: int) -> dict[str, object]:
        try:
            current = await self._store.get_active(source_id)
            if current is None:
                raise StoredSourceNotFoundError
            candidate = await self._store.get_revision(source_id, generation)
            await self._reloader.validate(candidate)
            record = await self._store.rollback(
                source_id,
                generation,
                current.generation,
            )
            await self._reloader.apply(record)
        except SourceGenerationConflictError as error:
            raise SourceGenerationConflictAppError from error
        except (StoredSourceNotFoundError, RegistryConfigurationError, SecretDecryptionError) as error:
            raise SourceValidationError from error
        except Exception as error:
            raise SourceControlUnavailableError from error
        return {
            "status": "rolled_back",
            "source_id": source_id,
            "generation": record.generation,
            "metadata_revision": record.metadata_revision,
        }

    async def _stage(self, source: SourceProfile) -> PreparedMetadata:
        catalog = self._catalog_factory()
        service = MetadataService(
            SourceRegistry([source]),
            catalog,
            verified_revisions=self._verified_revisions,
        )
        try:
            return await service.get_published(source.source_id)
        except Exception as error:
            raise SourceValidationError from error
        finally:
            await catalog.close()
