from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from textwrap import dedent, indent
from typing import Literal, cast, get_type_hints

import pytest

import query_man.app as app_module
from query_man.access import AccessPolicy, AccessPolicyConfigurationError
from query_man.catalog import PostgresCatalog
from query_man.models import (
    CatalogSnapshot,
    PreparedMetadata,
    ResourceObservation,
    RuntimeCatalogProvider,
    SourceProfile,
)
from query_man.operations import operations
from query_man.query import PostgresQueryExecutor, RuntimeQueryExecutor
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
from query_man.source_admin import ReplicaSourceObservation, ResourceObservationSample
from query_man.verified import VerifiedQueryRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_QUERY_TOKEN = "query-token-value-with-at-least-32-characters"
_ADMIN_TOKEN = "admin-token-value-with-at-least-32-characters"


def test_runtime_composition_requires_composite_provider_contracts() -> None:
    hints = get_type_hints(app_module.build_app)

    assert hints["catalog"] == RuntimeCatalogProvider | None
    assert hints["query_executor"] == RuntimeQueryExecutor | None


@pytest.mark.parametrize(
    "method",
    ["load", "close", "invalidate", "observe_resources"],
)
def test_runtime_rejects_catalog_with_missing_required_capability(
    method: str,
) -> None:
    catalog = PostgresCatalog()
    setattr(catalog, method, None)

    with pytest.raises(
        TypeError,
        match=rf"catalog is missing required runtime capabilities: {method}",
    ):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            catalog=catalog,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "method",
    ["execute", "cancel", "close", "stop_accepting", "drain", "invalidate"],
)
def test_runtime_rejects_query_executor_with_missing_required_capability(
    method: str,
) -> None:
    executor = PostgresQueryExecutor()
    setattr(executor, method, None)

    with pytest.raises(
        TypeError,
        match=rf"query_executor is missing required runtime capabilities: {method}",
    ):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            query_executor=executor,  # type: ignore[arg-type]
        )


def test_runtime_does_not_replace_falsey_incomplete_adapters() -> None:
    class FalseyCatalog(PostgresCatalog):
        def __bool__(self) -> bool:
            return False

    class FalseyQueryExecutor(PostgresQueryExecutor):
        def __bool__(self) -> bool:
            return False

    catalog = FalseyCatalog()
    catalog.invalidate = None  # type: ignore[assignment]
    with pytest.raises(TypeError, match=r"catalog.*invalidate"):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            catalog=catalog,
        )

    executor = FalseyQueryExecutor()
    executor.drain = None  # type: ignore[assignment]
    with pytest.raises(TypeError, match=r"query_executor.*drain"):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            query_executor=executor,
        )


def _runtime(
    source_mode: Literal["bootstrap", "managed"],
    source_directory: Path,
    *,
    access_policy_file: Path | None = None,
) -> RuntimeConfig:
    managed = source_mode == "managed"
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=source_directory,
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=access_policy_file,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode=source_mode,
        control_dsn="host=control.invalid dbname=query_man" if managed else None,
        source_encryption_key=_SOURCE_KEY if managed else None,
        replica_id="runtime-test" if managed else None,
    )


def _unexpected_file_load(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("managed mode must not load bootstrap files")


def _policy(
    tmp_path: Path,
    callers: str,
    environment: dict[str, str],
) -> AccessPolicy:
    return AccessPolicy.load(_write_policy(tmp_path, callers), environment)


def _write_policy(tmp_path: Path, callers: str) -> Path:
    path = tmp_path / "access.yaml"
    body = indent(dedent(callers).strip(), "  ")
    path.write_text(f"version: 2\ncallers:\n{body}\n", encoding="utf-8")
    return path


def _shared_policy(tmp_path: Path) -> AccessPolicy:
    return _policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
        {"QUERY_TOKEN": _QUERY_TOKEN, "ADMIN_TOKEN": _ADMIN_TOKEN},
    )


def test_managed_mode_starts_empty_without_loading_source_or_verified_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(SourceRegistry, "load", _unexpected_file_load)
    monkeypatch.setattr(VerifiedQueryRegistry, "load", _unexpected_file_load)

    app = app_module.build_app(
        _runtime("managed", tmp_path / "missing" / "sources"),
        access_policy=_shared_policy(tmp_path),
    )

    assert app.state.registry.source_ids() == frozenset()
    assert app.state.source_admin is not None
    assert app.state.source_reloader is not None
    assert app.state.source_reloader._invalidators == (
        app.state.catalog,
        app.state.query_executor,
    )


def test_managed_mode_keeps_all_control_writers_on_fixed_source_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructed: list[tuple[str, object]] = []

    class ReplicaWriter:
        def __init__(self, store: object) -> None:
            constructed.append(("replica", store))

    class ResourceWriter:
        def __init__(self, store: object) -> None:
            constructed.append(("resource", store))

    class GatewayWriter:
        def __init__(self, store: object) -> None:
            constructed.append(("gateway", store))

    monkeypatch.setattr(app_module, "ControlReplicaObservationWriter", ReplicaWriter)
    monkeypatch.setattr(app_module, "ControlResourceObservationWriter", ResourceWriter)
    monkeypatch.setattr(app_module, "ControlGatewayUsageWriter", GatewayWriter)

    app = app_module.build_app(
        _runtime("managed", tmp_path / "missing" / "sources"),
        access_policy=_shared_policy(tmp_path),
    )

    reloader = app.state.source_reloader
    source_admin = app.state.source_admin
    assert reloader is not None
    assert source_admin is not None
    assert [name for name, _store in constructed] == [
        "replica",
        "resource",
        "gateway",
    ]
    assert constructed[0][1] is reloader._source_store
    assert source_admin._store is reloader._source_store
    assert constructed[1][1] is reloader._source_store
    assert constructed[2][1] is reloader._source_store


def test_managed_mode_rejects_an_injected_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not accept a bootstrap registry"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            registry=SourceRegistry([]),
            access_policy=_shared_policy(tmp_path),
        )


def test_managed_mode_rejects_anonymous_local_compatibility(tmp_path: Path) -> None:
    with pytest.raises(
        AccessPolicyConfigurationError,
        match="authenticated query and admin identities",
    ):
        app_module.build_app(_runtime("managed", tmp_path / "missing" / "sources"))


def test_managed_mode_rejects_legacy_query_only_policy(tmp_path: Path) -> None:
    with pytest.raises(AccessPolicyConfigurationError, match="admin identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=AccessPolicy.legacy(_QUERY_TOKEN),
        )


def test_managed_mode_rejects_policy_without_admin(tmp_path: Path) -> None:
    query_only = _policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
""",
        {"QUERY_TOKEN": _QUERY_TOKEN},
    )

    with pytest.raises(AccessPolicyConfigurationError, match="admin identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=query_only,
        )


def test_managed_mode_rejects_policy_without_query_identity(tmp_path: Path) -> None:
    admin_only = _policy(
        tmp_path,
        """
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
        {"ADMIN_TOKEN": _ADMIN_TOKEN},
    )

    with pytest.raises(AccessPolicyConfigurationError, match="non-admin query identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=admin_only,
        )


def test_managed_mode_loads_shared_policy_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_file = _write_policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
    )
    monkeypatch.setenv("QUERY_TOKEN", _QUERY_TOKEN)
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN_TOKEN)

    app = app_module.build_app(
        _runtime(
            "managed",
            tmp_path / "missing" / "sources",
            access_policy_file=policy_file,
        )
    )

    query_caller = app.state.access_policy.authenticate(_QUERY_TOKEN)
    admin_caller = app.state.access_policy.authenticate(_ADMIN_TOKEN)
    assert query_caller is not None and query_caller.operator is False
    assert admin_caller is not None and admin_caller.operator is True


def test_bootstrap_mode_preserves_an_explicit_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SourceRegistry([])
    monkeypatch.setattr(SourceRegistry, "load", _unexpected_file_load)
    monkeypatch.setattr(
        VerifiedQueryRegistry,
        "load",
        lambda *_args, **_kwargs: VerifiedQueryRegistry([]),
    )

    app = app_module.build_app(
        _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
        registry=registry,
    )

    assert app.state.registry is registry
    assert app.state.source_admin is None
    assert app.state.source_reloader is None


def test_bootstrap_mode_does_not_construct_control_plane_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "PostgresMetadataStore", _unexpected_file_load)
    monkeypatch.setattr(app_module, "PostgresSourceStore", _unexpected_file_load)
    monkeypatch.setattr(
        app_module,
        "ControlResourceObservationWriter",
        _unexpected_file_load,
    )
    monkeypatch.setattr(
        app_module,
        "ControlGatewayUsageWriter",
        _unexpected_file_load,
    )

    app = app_module.build_app(
        _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
        registry=load_test_registry(),
    )

    assert app.state.source_admin is None
    assert app.state.source_reloader is None


def _development_source() -> SourceProfile:
    source = load_test_registry().get("development-issues")
    assert source is not None
    return source


def _expected_definition_revision(material: dict[str, object]) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_resource_samples_have_exact_metrics_and_canonical_definition_hashes() -> None:
    source = _development_source()
    definition = source.observability
    assert definition is not None
    observation = ResourceObservation(
        representative_records=37,
        table_bytes=1_000,
        index_bytes=250,
        total_storage_bytes=1_250,
    )

    samples = app_module._resource_observation_samples(source, observation)

    assert [(sample.metric, sample.value, sample.unit, sample.method) for sample in samples] == [
        ("representative_records", 37, "rows", "postgres_catalog_estimate"),
        ("table_bytes", 1_000, "bytes", "postgres_relation_size"),
        ("index_bytes", 250, "bytes", "postgres_relation_size"),
        ("total_storage_bytes", 1_250, "bytes", "postgres_relation_size"),
    ]
    by_metric = {sample.metric: sample for sample in samples}
    representative = definition.representative_records
    migration_ref = source.provenance.database_migration_ref
    assert by_metric["representative_records"].definition_revision == (
        _expected_definition_revision(
            {
                "database_migration_ref": migration_ref,
                "grain": representative.grain,
                "method": "postgres_catalog_estimate",
                "metric": "representative_records",
                "physical_relation": representative.physical_relation,
            }
        )
    )
    sorted_relations = sorted(definition.storage_relations)
    for metric in ("table_bytes", "index_bytes", "total_storage_bytes"):
        assert by_metric[metric].definition_revision == _expected_definition_revision(
            {
                "database_migration_ref": migration_ref,
                "method": "postgres_relation_size",
                "metric": metric,
                "relations": sorted_relations,
            }
        )


@pytest.mark.asyncio
async def test_resource_collection_omits_missing_estimate_and_unconfigured_source() -> None:
    source = _development_source()
    unconfigured = replace(source, source_id="unconfigured", observability=None)
    revision = f"sha256:{'a' * 64}"

    class Metadata:
        def __init__(self) -> None:
            self.source_ids: list[str] = []

        async def get_published(self, source_id: str) -> PreparedMetadata:
            self.source_ids.append(source_id)
            return PreparedMetadata(CatalogSnapshot(), revision)

    class Catalog:
        def __init__(self) -> None:
            self.sources: list[SourceProfile] = []

        async def observe_resources(self, requested: SourceProfile) -> ResourceObservation:
            self.sources.append(requested)
            return ResourceObservation(
                representative_records=None,
                table_bytes=10,
                index_bytes=2,
                total_storage_bytes=12,
            )

    class Writer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple[ResourceObservationSample, ...]]] = []

        async def report_resource_observations(
            self,
            source_id: str,
            metadata_revision: str,
            samples: tuple[ResourceObservationSample, ...],
        ) -> None:
            self.calls.append((source_id, metadata_revision, samples))

    metadata = Metadata()
    catalog = Catalog()
    writer = Writer()

    await app_module._collect_resource_observations(  # type: ignore[arg-type]
        (source, unconfigured),
        catalog,
        metadata,
        writer,
    )

    assert metadata.source_ids == [source.source_id]
    assert catalog.sources == [source]
    assert len(writer.calls) == 1
    source_id, metadata_revision, samples = writer.calls[0]
    assert (source_id, metadata_revision) == (source.source_id, revision)
    assert [sample.metric for sample in samples] == [
        "table_bytes",
        "index_bytes",
        "total_storage_bytes",
    ]


@pytest.mark.asyncio
async def test_resource_reporting_is_immediate_for_initial_and_new_profile_identity() -> None:
    initial = replace(
        _development_source(),
        control_generation=1,
        control_state_version=1,
    )
    updated = replace(initial, control_generation=2, control_state_version=2)
    registry = SourceRegistry([initial])

    class Reloader:
        def __init__(self) -> None:
            self.syncs = 0

        async def sync(self) -> None:
            self.syncs += 1
            if self.syncs == 1:
                registry.upsert(updated)

    class Metadata:
        async def get_published(self, source_id: str) -> PreparedMetadata:
            source = registry.get(source_id)
            assert source is not None and source.control_generation is not None
            return PreparedMetadata(
                CatalogSnapshot(),
                f"sha256:{str(source.control_generation) * 64}",
            )

    class Catalog:
        def __init__(self) -> None:
            self.sources: list[SourceProfile] = []

        async def observe_resources(self, source: SourceProfile) -> ResourceObservation:
            self.sources.append(source)
            return ResourceObservation(1, 2, 3, 5)

    class Writer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.reported = asyncio.Event()

        async def report_resource_observations(
            self,
            source_id: str,
            metadata_revision: str,
            _samples: tuple[ResourceObservationSample, ...],
        ) -> None:
            self.calls.append((source_id, metadata_revision))
            if len(self.calls) == 2:
                self.reported.set()

    catalog = Catalog()
    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            Reloader(),
            1,
            registry=registry,
            catalog=catalog,
            metadata=Metadata(),
            resource_writer=writer,
        )
    )
    try:
        await asyncio.wait_for(writer.reported.wait(), timeout=1)
        assert catalog.sources == [initial, updated]
        assert writer.calls == [
            (initial.source_id, f"sha256:{'1' * 64}"),
            (updated.source_id, f"sha256:{'2' * 64}"),
        ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_resource_reporting_repeats_on_fixed_twenty_four_hour_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_RESOURCE_OBSERVATION_INTERVAL_SECONDS", 0.01)
    source = _development_source()
    registry = SourceRegistry([source])

    class Reloader:
        async def sync(self) -> None:
            raise AssertionError("reload is not due before the resource cadence")

    class Metadata:
        async def get_published(self, _source_id: str) -> PreparedMetadata:
            return PreparedMetadata(CatalogSnapshot(), f"sha256:{'a' * 64}")

    class Catalog:
        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            return ResourceObservation(1, 2, 3, 5)

    class Writer:
        def __init__(self) -> None:
            self.calls = 0
            self.reported = asyncio.Event()

        async def report_resource_observations(
            self,
            _source_id: str,
            _metadata_revision: str,
            _samples: tuple[ResourceObservationSample, ...],
        ) -> None:
            self.calls += 1
            if self.calls == 2:
                self.reported.set()

    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            Reloader(),
            60_000,
            registry=registry,
            catalog=Catalog(),
            metadata=Metadata(),
            resource_writer=writer,
        )
    )
    try:
        await asyncio.wait_for(writer.reported.wait(), timeout=1)
        assert writer.calls == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_new_profile_gets_its_own_full_resource_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_RESOURCE_OBSERVATION_INTERVAL_SECONDS", 0.5)
    development = _development_source()
    market = load_test_registry().get("market-voc")
    assert market is not None
    registry = SourceRegistry([development])

    class Metadata:
        async def get_published(self, source_id: str) -> PreparedMetadata:
            digit = "a" if source_id == development.source_id else "b"
            return PreparedMetadata(CatalogSnapshot(), f"sha256:{digit * 64}")

    class Catalog:
        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            return ResourceObservation(1, 2, 3, 5)

    class Writer:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self.order: list[str] = []
            self.initial_development = asyncio.Event()
            self.initial_market = asyncio.Event()
            self.periodic_development = asyncio.Event()

        async def report_resource_observations(
            self,
            source_id: str,
            _metadata_revision: str,
            _samples: tuple[ResourceObservationSample, ...],
        ) -> None:
            self.order.append(source_id)
            self.counts[source_id] = self.counts.get(source_id, 0) + 1
            if source_id == development.source_id and self.counts[source_id] == 1:
                self.initial_development.set()
            if source_id == market.source_id and self.counts[source_id] == 1:
                self.initial_market.set()
            if source_id == development.source_id and self.counts[source_id] == 2:
                self.periodic_development.set()

    writer = Writer()
    task = asyncio.create_task(
        app_module._resource_observation_loop(  # type: ignore[arg-type]
            registry,
            Catalog(),
            Metadata(),
            writer,
            0.005,
        )
    )
    try:
        await asyncio.wait_for(writer.initial_development.wait(), timeout=1)
        await asyncio.sleep(0.3)
        registry.upsert(market)
        await asyncio.wait_for(writer.initial_market.wait(), timeout=1)
        await asyncio.wait_for(writer.periodic_development.wait(), timeout=1)

        assert writer.counts == {
            development.source_id: 2,
            market.source_id: 1,
        }
        assert writer.order == [
            development.source_id,
            market.source_id,
            development.source_id,
        ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_resource_failures_are_isolated_and_cancellation_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="query_man")
    source = _development_source()

    class Metadata:
        async def get_published(self, source_id: str) -> PreparedMetadata:
            operations.set_source_health(source_id, "unavailable")
            operations.set_replica_metadata_revision(source_id, f"sha256:{'f' * 64}")
            return PreparedMetadata(CatalogSnapshot(), f"sha256:{'a' * 64}")

    class Writer:
        def __init__(self) -> None:
            self.calls = 0

        async def report_resource_observations(self, *_args: object) -> None:
            self.calls += 1

    class FailingCatalog:
        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            raise RuntimeError("credential=private-resource-secret")

    class CancelledCatalog:
        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            raise asyncio.CancelledError

    operations.reset()
    try:
        operations.reconcile_sources({source.source_id})
        operations.set_replica_source_applied(source.source_id, 1, 1, True)
        operations.set_replica_metadata_revision(source.source_id, f"sha256:{'a' * 64}")
        operations.set_source_health(source.source_id, "healthy")
        writer = Writer()
        public_before = operations.snapshot()
        replica_before = operations.replica_runtime_snapshot()
        await app_module._collect_resource_observations(  # type: ignore[arg-type]
            (source,),
            FailingCatalog(),
            Metadata(),
            writer,
        )

        assert writer.calls == 0
        assert operations.snapshot() == public_before
        assert operations.replica_runtime_snapshot() == replica_before
        assert "private-resource-secret" not in caplog.text
        with pytest.raises(asyncio.CancelledError):
            await app_module._collect_resource_observations(  # type: ignore[arg-type]
                (source,),
                CancelledCatalog(),
                Metadata(),
                writer,
            )
        assert operations.snapshot() == public_before
        assert operations.replica_runtime_snapshot() == replica_before
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_slow_reporting_children_do_not_delay_reload_or_replica_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "REPLICA_HEARTBEAT_INTERVAL_MIN_MS", 1)
    monkeypatch.setattr(app_module, "_GATEWAY_USAGE_REPORT_INTERVAL_SECONDS", 0.001)
    source = _development_source()
    registry = SourceRegistry([source])

    class Reloader:
        def __init__(self) -> None:
            self.synced = asyncio.Event()

        async def sync(self) -> None:
            self.synced.set()

    class ReplicaWriter:
        def __init__(self) -> None:
            self.reports = 0
            self.heartbeat = asyncio.Event()

        async def register_replica(self, _replica_id: str, _interval_ms: int) -> int:
            return 5

        async def report_replica(self, *_args: object, **_kwargs: object) -> None:
            self.reports += 1
            if self.reports >= 2:
                self.heartbeat.set()

    class Metadata:
        async def get_published(self, _source_id: str) -> PreparedMetadata:
            return PreparedMetadata(CatalogSnapshot(), f"sha256:{'a' * 64}")

    class BlockingCatalog:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            raise AssertionError("blocked catalog observation unexpectedly returned")

    class ResourceWriter:
        async def report_resource_observations(self, *_args: object) -> None:
            raise AssertionError("blocked catalog must not reach the resource writer")

    class BlockingGatewayWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def report_gateway_usage(self, *_args: object) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    operations.reset()
    operations.record_gateway_usage(
        source_id=source.source_id,
        budget_profile=source.budget.name,
        metadata_revision=f"sha256:{'a' * 64}",
        outcome="rejected",
    )
    reloader = Reloader()
    replica_writer = ReplicaWriter()
    catalog = BlockingCatalog()
    gateway_writer = BlockingGatewayWriter()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            reloader,
            1,
            replica_writer,
            "runtime-isolated",
            registry=registry,
            catalog=catalog,
            metadata=Metadata(),
            resource_writer=ResourceWriter(),
            gateway_writer=gateway_writer,
        )
    )
    try:
        await asyncio.wait_for(catalog.started.wait(), timeout=1)
        await asyncio.wait_for(gateway_writer.started.wait(), timeout=1)
        await asyncio.wait_for(reloader.synced.wait(), timeout=1)
        await asyncio.wait_for(replica_writer.heartbeat.wait(), timeout=1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()

    assert catalog.cancelled.is_set()
    assert gateway_writer.cancelled.is_set()


@pytest.mark.asyncio
async def test_observation_writes_are_serialized_without_blocking_authority_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "REPLICA_HEARTBEAT_INTERVAL_MIN_MS", 1)
    monkeypatch.setattr(app_module, "_GATEWAY_USAGE_REPORT_INTERVAL_SECONDS", 0.001)
    source = _development_source()

    class Reloader:
        def __init__(self) -> None:
            self.synced = asyncio.Event()

        async def sync(self) -> None:
            self.synced.set()

    class ReplicaWriter:
        def __init__(self) -> None:
            self.reports = 0
            self.heartbeat = asyncio.Event()

        async def register_replica(self, *_args: object) -> int:
            return 5

        async def report_replica(self, *_args: object, **_kwargs: object) -> None:
            self.reports += 1
            if self.reports >= 2:
                self.heartbeat.set()

    class Metadata:
        async def get_published(self, _source_id: str) -> PreparedMetadata:
            return PreparedMetadata(CatalogSnapshot(), f"sha256:{'a' * 64}")

    class Catalog:
        async def observe_resources(self, _source: SourceProfile) -> ResourceObservation:
            return ResourceObservation(
                representative_records=1,
                table_bytes=2,
                index_bytes=3,
                total_storage_bytes=5,
            )

    class ResourceWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def report_resource_observations(self, *_args: object) -> None:
            self.started.set()
            await self.release.wait()

    class GatewayWriter:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def report_gateway_usage(self, *_args: object) -> None:
            self.started.set()

    reloader = Reloader()
    replica_writer = ReplicaWriter()
    resource_writer = ResourceWriter()
    gateway_writer = GatewayWriter()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            reloader,
            1,
            replica_writer,
            "runtime-serialized",
            registry=SourceRegistry([source]),
            catalog=Catalog(),
            metadata=Metadata(),
            resource_writer=resource_writer,
            gateway_writer=gateway_writer,
        )
    )
    try:
        await asyncio.wait_for(resource_writer.started.wait(), timeout=1)
        await asyncio.sleep(0.01)
        assert not gateway_writer.started.is_set()
        await asyncio.wait_for(reloader.synced.wait(), timeout=1)
        await asyncio.wait_for(replica_writer.heartbeat.wait(), timeout=1)

        resource_writer.release.set()
        await asyncio.wait_for(gateway_writer.started.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_gateway_report_retries_same_sequence_payload_and_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(app_module, "_GATEWAY_USAGE_REPORT_INTERVAL_SECONDS", 0.01)
    caplog.set_level(logging.WARNING, logger="query_man")

    class Reloader:
        async def sync(self) -> None:
            raise AssertionError("reload is not due before the gateway cadence")

    class ReplicaWriter:
        def __init__(self) -> None:
            self.registrations = 0

        async def register_replica(self, replica_id: str, interval_ms: int) -> int:
            assert (replica_id, interval_ms) == ("runtime-gateway", 60_000)
            self.registrations += 1
            return 11

        async def report_replica(self, *_args: object, **_kwargs: object) -> None:
            pass

    class GatewayWriter:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []
            self.reported = asyncio.Event()

        async def report_gateway_usage(
            self,
            replica_id: str,
            incarnation: int,
            sequence: int,
            deltas: tuple[object, ...],
        ) -> None:
            self.calls.append((replica_id, incarnation, sequence, deltas))
            if len(self.calls) == 1:
                raise RuntimeError("credential=private-gateway-secret")
            self.reported.set()

    operations.reset()
    operations.record_gateway_usage(
        source_id="development-issues",
        budget_profile="interactive",
        metadata_revision=f"sha256:{'a' * 64}",
        outcome="success",
        queue_ms=1,
        elapsed_ms=2,
        returned_rows=3,
        result_bytes=4,
        truncated=True,
    )
    replica_writer = ReplicaWriter()
    gateway_writer = GatewayWriter()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            Reloader(),
            60_000,
            replica_writer,
            "runtime-gateway",
            gateway_writer=gateway_writer,
        )
    )
    try:
        await asyncio.wait_for(gateway_writer.reported.wait(), timeout=1)
        assert replica_writer.registrations == 1
        assert len(gateway_writer.calls) == 2
        assert gateway_writer.calls[0][:3] == ("runtime-gateway", 11, 1)
        assert gateway_writer.calls[1][:3] == ("runtime-gateway", 11, 1)
        assert gateway_writer.calls[0][3] == gateway_writer.calls[1][3]
        assert operations.gateway_usage_report_snapshot() is None
        assert "private-gateway-secret" not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()


@pytest.mark.asyncio
async def test_gateway_report_retries_an_empty_heartbeat_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_GATEWAY_USAGE_REPORT_INTERVAL_SECONDS", 0.01)

    class Writer:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []
            self.reported = asyncio.Event()

        async def report_gateway_usage(
            self,
            replica_id: str,
            incarnation: int,
            sequence: int,
            deltas: tuple[object, ...],
        ) -> None:
            self.calls.append((replica_id, incarnation, sequence, deltas))
            if len(self.calls) == 1:
                raise RuntimeError("ambiguous empty report")
            self.reported.set()

    operations.reset()
    writer = Writer()
    task = asyncio.create_task(
        app_module._gateway_usage_loop(writer, "runtime-empty", 17)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(writer.reported.wait(), timeout=1)
        assert writer.calls == [
            ("runtime-empty", 17, 1, ()),
            ("runtime-empty", 17, 1, ()),
        ]
        assert operations.gateway_usage_report_snapshot() is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()


@pytest.mark.asyncio
async def test_gateway_report_cancellation_propagates_without_ack() -> None:
    class Writer:
        async def report_gateway_usage(self, *_args: object) -> None:
            raise asyncio.CancelledError

    operations.reset()
    try:
        operations.record_gateway_usage(
            source_id="development-issues",
            budget_profile="interactive",
            metadata_revision=f"sha256:{'a' * 64}",
            outcome="rejected",
        )

        with pytest.raises(asyncio.CancelledError):
            await app_module._report_gateway_usage(  # type: ignore[arg-type]
                Writer(),
                "runtime-gateway",
                11,
                1,
            )

        outstanding = operations.gateway_usage_report_snapshot()
        assert outstanding is not None
        assert outstanding.snapshot_id == 1
    finally:
        operations.reset()


@pytest.mark.parametrize(
    ("reload_interval_ms", "heartbeat_interval_ms"),
    [(250, 5_000), (6_000, 6_000)],
)
@pytest.mark.asyncio
async def test_managed_reload_task_registers_once_and_reports_internal_snapshot(
    reload_interval_ms: int,
    heartbeat_interval_ms: int,
) -> None:
    class Reloader:
        async def sync(self) -> None:
            pass

    class Writer:
        def __init__(self) -> None:
            self.registrations: list[tuple[str, int]] = []
            self.reports: list[tuple[object, ...]] = []
            self.reported = asyncio.Event()

        async def register_replica(self, replica_id: str, interval_ms: int) -> int:
            self.registrations.append((replica_id, interval_ms))
            return 7

        async def report_replica(
            self,
            replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            self.reports.append((replica_id, incarnation, reason_code, sources))
            self.reported.set()

    operations.reset()
    try:
        operations.set_replica_source_applied("source-a", 2, 3, True)
        operations.set_replica_metadata_revision("source-a", f"sha256:{'1' * 64}")
        operations.set_source_health("source-a", "healthy")
        public_before = operations.snapshot()
        writer = Writer()
        task = asyncio.create_task(
            app_module._reload_sources(  # type: ignore[arg-type]
                Reloader(),
                reload_interval_ms,
                writer,
                "runtime-a",
            )
        )
        await asyncio.wait_for(writer.reported.wait(), timeout=1)

        assert writer.registrations == [("runtime-a", heartbeat_interval_ms)]
        assert len(writer.reports) == 1
        replica_id, incarnation, reason_code, sources = writer.reports[0]
        assert (replica_id, incarnation, reason_code) == ("runtime-a", 7, None)
        assert isinstance(sources, tuple) and len(sources) == 1
        source = cast(ReplicaSourceObservation, sources[0])
        assert source.source_id == "source-a"
        assert source.applied_generation == 2
        assert source.applied_state_version == 3
        assert source.applied_enabled is True
        assert source.applied_metadata_revision == f"sha256:{'1' * 64}"
        assert source.source_health == "healthy"
        assert source.reason_code is None
        assert operations.snapshot() == public_before
    finally:
        if "task" in locals():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        operations.reset()


@pytest.mark.asyncio
async def test_replica_report_omits_sources_during_control_scan_failure() -> None:
    class Writer:
        def __init__(self) -> None:
            self.report: tuple[object, ...] | None = None

        async def report_replica(
            self,
            replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            self.report = (replica_id, incarnation, reason_code, sources)

    operations.reset()
    try:
        operations.set_replica_source_applied("source-a", 1, 1, True)
        operations.set_replica_scan_failed(True)
        writer = Writer()

        await app_module._report_replica_observation(  # type: ignore[arg-type]
            writer,
            "runtime-a",
            4,
        )

        assert writer.report == ("runtime-a", 4, "CONTROL_SCAN_FAILED", ())
        assert len(operations.replica_runtime_snapshot().sources) == 1
    finally:
        operations.reset()


@pytest.mark.parametrize("failure", ["registration", "report"])
@pytest.mark.asyncio
async def test_replica_observation_failure_does_not_restart_registration_or_reload(
    failure: str,
) -> None:
    class Reloader:
        def __init__(self) -> None:
            self.synced = asyncio.Event()

        async def sync(self) -> None:
            self.synced.set()

    class Writer:
        def __init__(self) -> None:
            self.registrations = 0
            self.reports = 0

        async def register_replica(self, _replica_id: str, _interval_ms: int) -> int:
            self.registrations += 1
            if failure == "registration":
                raise RuntimeError("registration unavailable")
            return 3

        async def report_replica(
            self,
            _replica_id: str,
            _incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            del reason_code, sources
            self.reports += 1
            raise RuntimeError("report unavailable")

    operations.reset()
    reloader = Reloader()
    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            reloader,
            1,
            writer,
            "runtime-a",
        )
    )
    try:
        await asyncio.wait_for(reloader.synced.wait(), timeout=1)
        assert writer.registrations == 1
        assert writer.reports == (0 if failure == "registration" else 1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()


@pytest.mark.asyncio
async def test_replica_report_failure_retries_same_incarnation_without_reregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reloader:
        async def sync(self) -> None:
            pass

    class Writer:
        def __init__(self) -> None:
            self.registrations = 0
            self.reports = 0
            self.retried = asyncio.Event()

        async def register_replica(self, _replica_id: str, _interval_ms: int) -> int:
            self.registrations += 1
            return 9

        async def report_replica(
            self,
            _replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            del reason_code, sources
            assert incarnation == 9
            self.reports += 1
            if self.reports >= 2:
                self.retried.set()
            raise RuntimeError("report unavailable")

    monkeypatch.setattr(app_module, "REPLICA_HEARTBEAT_INTERVAL_MIN_MS", 1)
    operations.reset()
    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            Reloader(),
            1,
            writer,
            "runtime-a",
        )
    )
    try:
        await asyncio.wait_for(writer.retried.wait(), timeout=1)
        assert writer.registrations == 1
        assert writer.reports >= 2
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()
