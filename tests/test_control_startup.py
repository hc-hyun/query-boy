from __future__ import annotations

import base64
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from dotenv import load_dotenv

from query_man.app import build_app
from query_man.runtime_config import RuntimeConfig
from query_man.source_store import PostgresSourceStore
from query_man.verified import ExpectedResult, VerifiedQuery
from tests.helpers import ROOT_DIRECTORY

_SOURCE_ID = "support-tickets"
_QUESTION = "지원 queue별 ticket 수를 보여줘"


def _managed_runtime(
    source_directory: Path,
    control_dsn: str,
    encryption_key: str,
) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token=None,
        source_directory=source_directory,
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode="managed",
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        source_reload_interval_ms=60_000,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_managed_control_state_survives_restart_deactivate_and_rollback(
    tmp_path: Path,
    disposable_control_dsn: str,
) -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL credentials are not configured")

    encryption_key = base64.urlsafe_b64encode(b"acceptance-source-key-material!!").decode("ascii")
    assert len(base64.urlsafe_b64decode(encryption_key)) == 32
    missing_source_directory = tmp_path / "zero-bootstrap" / "sources"
    runtime = _managed_runtime(
        missing_source_directory,
        disposable_control_dsn,
        encryption_key,
    )
    l0_manifest: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets.yaml").read_text(encoding="utf-8")
    )
    semantic_manifest: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets-l2.yaml").read_text(encoding="utf-8")
    )
    verified_document: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets-verified-query.yaml").read_text(encoding="utf-8")
    )
    credential = os.environ.get(
        "SUPPORT_TICKETS_READER_PASSWORD",
        "support-tickets-local-secret",
    )
    control_store = PostgresSourceStore(disposable_control_dsn)
    try:
        first_app = build_app(runtime)
        async with first_app.router.lifespan_context(first_app):
            admin = first_app.state.source_admin
            assert admin is not None
            assert first_app.state.registry.source_ids() == frozenset()

            l0 = await admin.publish(_SOURCE_ID, l0_manifest, credential)
            assert l0["generation"] == 1
            assert l0["quality_level"] == "L0"
            l0_revision = l0["metadata_revision"]

            l1_manifest = _with_quality(semantic_manifest, "L1")
            l1 = await admin.publish(_SOURCE_ID, l1_manifest, credential)
            assert l1["generation"] == 2
            assert l1["quality_level"] == "L1"
            semantic_revision = l1["metadata_revision"]
            assert semantic_revision != l0_revision

            expected = verified_document["expected"]
            verified = await admin.publish_verified_query(
                VerifiedQuery(
                    query_id=verified_document["query_id"],
                    source_id=_SOURCE_ID,
                    question=verified_document["question"],
                    sql=verified_document["sql"],
                    metadata_revision=semantic_revision,
                    relations=tuple(verified_document["relations"]),
                    expected=ExpectedResult(
                        columns=tuple(expected["columns"]),
                        row_count=expected["row_count"],
                        result_hash=expected["result_hash"],
                    ),
                ),
                "engineering",
            )
            assert verified["status"] == "verified"
            assert verified["metadata_revision"] == semantic_revision
            assert await control_store.verified_revision_map() == {_SOURCE_ID: frozenset({semantic_revision})}

            l2_manifest = _with_quality(semantic_manifest, "L2")
            l2 = await admin.publish(_SOURCE_ID, l2_manifest, credential)
            assert l2["generation"] == 3
            assert l2["metadata_revision"] == semantic_revision
            assert l2["quality_level"] == "L2"
            profile = first_app.state.registry.get(_SOURCE_ID)
            assert profile is not None
            assert (profile.control_generation, profile.control_state_version) == (3, 3)
            context = await first_app.state.metadata.get_context(_SOURCE_ID, _QUESTION)
            assert (context["metadata_revision"], context["quality_level"]) == (
                semantic_revision,
                "L2",
            )
            active = await control_store.get_active(_SOURCE_ID)
            assert active is not None
            assert (
                active.generation,
                active.state_version,
                active.metadata_revision,
                active.enabled,
            ) == (3, 3, semantic_revision, True)

        assert not missing_source_directory.exists()
        restored_app = build_app(runtime)
        async with restored_app.router.lifespan_context(restored_app):
            profile = restored_app.state.registry.get(_SOURCE_ID)
            assert profile is not None
            assert (profile.control_generation, profile.control_state_version) == (3, 3)
            restored_context = await restored_app.state.metadata.get_context(
                _SOURCE_ID,
                _QUESTION,
            )
            assert (
                restored_context["metadata_revision"],
                restored_context["quality_level"],
            ) == (semantic_revision, "L2")

            admin = restored_app.state.source_admin
            assert admin is not None
            assert await admin.deactivate(_SOURCE_ID) == {
                "status": "deactivated",
                "source_id": _SOURCE_ID,
            }
            assert restored_app.state.registry.get(_SOURCE_ID) is None
            inactive = await control_store.get_active(_SOURCE_ID)
            assert inactive is not None
            assert (inactive.generation, inactive.state_version, inactive.enabled) == (
                3,
                4,
                False,
            )

        invalid_source_directory = tmp_path / "invalid-bootstrap" / "sources"
        invalid_source_directory.mkdir(parents=True)
        invalid_source = invalid_source_directory / "support-tickets.yaml"
        invalid_source.write_text(
            "version: 1\nsource_id: support-tickets\nunexpected: ignored\n",
            encoding="utf-8",
        )
        invalid_verified = invalid_source_directory.parent / "verified-queries.yaml"
        invalid_verified.write_text(
            "version: 1\nqueries:\n  - source_id: support-tickets\nunexpected: ignored\n",
            encoding="utf-8",
        )
        invalid_runtime = replace(runtime, source_directory=invalid_source_directory)
        deactivated_app = build_app(invalid_runtime)
        async with deactivated_app.router.lifespan_context(deactivated_app):
            assert deactivated_app.state.registry.get(_SOURCE_ID) is None
            inactive = await control_store.get_active(_SOURCE_ID)
            assert inactive is not None
            assert (inactive.generation, inactive.state_version, inactive.enabled) == (
                3,
                4,
                False,
            )

            admin = deactivated_app.state.source_admin
            assert admin is not None
            rolled_back = await admin.rollback(_SOURCE_ID, 2)
            assert rolled_back == {
                "status": "rolled_back",
                "source_id": _SOURCE_ID,
                "generation": 2,
                "metadata_revision": semantic_revision,
            }
            profile = deactivated_app.state.registry.get(_SOURCE_ID)
            assert profile is not None
            assert (profile.control_generation, profile.control_state_version) == (2, 5)
            rollback_context = await deactivated_app.state.metadata.get_context(
                _SOURCE_ID,
                _QUESTION,
            )
            assert (
                rollback_context["metadata_revision"],
                rollback_context["quality_level"],
            ) == (semantic_revision, "L2")

        assert invalid_source.read_text(encoding="utf-8").endswith("unexpected: ignored\n")
        assert invalid_verified.read_text(encoding="utf-8").endswith("unexpected: ignored\n")
        zero_bootstrap_app = build_app(runtime)
        async with zero_bootstrap_app.router.lifespan_context(zero_bootstrap_app):
            profile = zero_bootstrap_app.state.registry.get(_SOURCE_ID)
            assert profile is not None
            assert profile.minimum_quality_level == "L1"
            assert (profile.control_generation, profile.control_state_version) == (2, 5)
            context = await zero_bootstrap_app.state.metadata.get_context(
                _SOURCE_ID,
                _QUESTION,
            )
            assert (context["metadata_revision"], context["quality_level"]) == (
                semantic_revision,
                "L2",
            )
            active = await control_store.get_active(_SOURCE_ID)
            assert active is not None
            assert (
                active.generation,
                active.state_version,
                active.metadata_revision,
                active.enabled,
            ) == (2, 5, semantic_revision, True)
    finally:
        await control_store.close()


def _with_quality(
    manifest: dict[str, Any],
    minimum_quality_level: str,
) -> dict[str, Any]:
    copied = deepcopy(manifest)
    copied["minimum_quality_level"] = minimum_quality_level
    return copied
