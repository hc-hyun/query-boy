from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import yaml

from query_man.errors import SourceValidationError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, PreparedMetadata, SourceProfile
from query_man.registry import SourceRegistry, load_budget_profiles
from query_man.secrets import EncryptedSecret, SourceSecretCipher
from query_man.source_admin import SourceAdminService, SourceReloader
from query_man.source_store import StoredSource, StoredSourceNotFoundError
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


class MemoryMetadataStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], PreparedMetadata] = {}

    async def get_revision(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        return self.values[(source.source_id, revision)]

    async def get_active(self, _source: SourceProfile) -> PreparedMetadata | None:
        return None

    async def publish(self, _source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata:
        return value

    async def activate(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        return self.values[(source.source_id, revision)]

    async def unpin(self, _source: SourceProfile) -> None:
        pass

    async def close(self) -> None:
        pass


class MemorySourceStore:
    def __init__(self, metadata: MemoryMetadataStore) -> None:
        self.metadata = metadata
        self.active: dict[str, StoredSource] = {}
        self.history: dict[tuple[str, int], StoredSource] = {}

    async def list_active(self) -> list[StoredSource]:
        return list(self.active.values())

    async def get_active(self, source_id: str) -> StoredSource | None:
        return self.active.get(source_id)

    async def get_revision(self, source_id: str, generation: int) -> StoredSource:
        try:
            return self.history[(source_id, generation)]
        except KeyError as error:
            raise StoredSourceNotFoundError from error

    async def next_generation(self, source_id: str) -> int:
        generations = [
            generation
            for current_source, generation in self.history
            if current_source == source_id
        ]
        return max(generations, default=0) + 1

    async def publish(
        self,
        source_id: str,
        expected_generation: int,
        generation: int,
        manifest: dict[str, object],
        encrypted_secret: EncryptedSecret,
        metadata: PreparedMetadata,
    ) -> StoredSource:
        record = StoredSource(
            source_id,
            generation,
            manifest,
            encrypted_secret,
            metadata.revision,
            True,
        )
        self.metadata.values[(source_id, metadata.revision)] = metadata
        self.history[(source_id, generation)] = record
        self.active[source_id] = record
        return record

    async def deactivate(self, source_id: str, expected_generation: int) -> None:
        current = self.active[source_id]
        assert current.generation == expected_generation
        self.active[source_id] = replace(current, enabled=False)

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
    ) -> StoredSource:
        assert self.active[source_id].generation == expected_generation
        record = self.history[(source_id, generation)]
        self.active[source_id] = record
        return record

    async def close(self) -> None:
        pass


class StaticCatalog:
    def __init__(self, snapshot: CatalogSnapshot | None = None) -> None:
        self.snapshot = snapshot or minimal_development_snapshot()

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def close(self) -> None:
        pass


class SwitchingCatalogFactory:
    def __init__(self) -> None:
        self.snapshot = minimal_development_snapshot()

    def __call__(self) -> StaticCatalog:
        return StaticCatalog(self.snapshot)


class RecordingInvalidator:
    def __init__(self) -> None:
        self.source_ids: list[str] = []

    async def invalidate(self, source_id: str) -> None:
        self.source_ids.append(source_id)


def _manifest() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = "third-source"
    raw["connection"]["password_env"] = "THIRD_SOURCE_READER_PASSWORD"
    raw["minimum_quality_level"] = "L0"
    return raw


def _services(
    catalog_factory: object = StaticCatalog,
) -> tuple[
    SourceAdminService,
    SourceRegistry,
    MemorySourceStore,
    RecordingInvalidator,
    SourceSecretCipher,
]:
    registry = SourceRegistry([])
    metadata_store = MemoryMetadataStore()
    source_store = MemorySourceStore(metadata_store)
    metadata = MetadataService(registry, StaticCatalog(), store=metadata_store)
    cipher = SourceSecretCipher(b"a" * 32)
    invalidator = RecordingInvalidator()
    budgets = load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml")
    reloader = SourceReloader(
        registry,
        metadata,
        metadata_store,
        source_store,
        cipher,
        budgets,
        {},
        (invalidator,),
    )
    admin = SourceAdminService(
        source_store,
        reloader,
        cipher,
        budgets,
        {},
        catalog_factory,  # type: ignore[arg-type]
    )
    return admin, registry, source_store, invalidator, cipher


@pytest.mark.asyncio
async def test_publish_rotate_deactivate_and_rollback_apply_without_restart() -> None:
    admin, registry, store, invalidator, _cipher = _services()

    published = await admin.publish("third-source", _manifest(), "first-secret")
    assert published["generation"] == 1
    assert registry.get("third-source") is not None
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]

    rotated = await admin.rotate_credential("third-source", "rotated-secret")
    assert rotated["generation"] == 2
    assert registry.get("third-source").connection.password == "rotated-secret"  # type: ignore[union-attr]

    await admin.deactivate("third-source")
    assert registry.get("third-source") is None

    rolled_back = await admin.rollback("third-source", 1)
    assert rolled_back["generation"] == 1
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]
    assert store.active["third-source"].generation == 1
    assert invalidator.source_ids == ["third-source"] * 4


@pytest.mark.asyncio
async def test_failed_staging_preserves_current_source() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, registry, store, _invalidator, _cipher = _services(catalog_factory)
    await admin.publish("third-source", _manifest(), "first-secret")
    before = store.active["third-source"]

    incomplete = minimal_development_snapshot()
    incomplete.relations = incomplete.relations[:1]
    catalog_factory.snapshot = incomplete

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", _manifest(), "bad-update-secret")

    assert store.active["third-source"] == before
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reloader_applies_external_generation() -> None:
    admin, _registry, store, _invalidator, cipher = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    registry = SourceRegistry([])
    metadata = MetadataService(registry, StaticCatalog(), store=store.metadata)
    reloader = SourceReloader(
        registry,
        metadata,
        store.metadata,
        store,
        cipher,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        {},
    )

    await reloader.sync()

    assert registry.get("third-source") is not None
