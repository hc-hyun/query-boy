from __future__ import annotations

import os
from typing import cast

import pytest
from dotenv import load_dotenv

from query_man.assurance.quality import QualityEvaluation, QualityGateError
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY


def load_evaluation() -> tuple[SourceRegistry, QualityEvaluation]:
    load_dotenv(ROOT_DIRECTORY / ".env")
    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    evaluation = QualityEvaluation.load(
        ROOT_DIRECTORY / "config" / "quality-evaluation.yaml",
        {source["source_id"] for source in registry.list()},
    )
    return registry, evaluation


async def test_quality_gate_reports_relation_status_and_size_failures() -> None:
    _registry, evaluation = load_evaluation()

    class WrongMetadata:
        async def get_context(
            self, _source_id: str, _question: str, _max_objects: int = 2
        ) -> dict[str, object]:
            return {
                "relations": [{"name": "ai.wrong"}],
                "answerability": {"status": "best_effort"},
                "padding": "x" * 70_000,
            }

    with pytest.raises(QualityGateError) as captured:
        await evaluation.evaluate(cast(MetadataService, WrongMetadata()))
    report = captured.value.report
    assert report.relation_accuracy == 0
    assert report.answerability_recall == 0
    assert report.max_context_bytes > evaluation.gates.max_context_bytes
    assert "relation accuracy gate failed" in report.failures
    assert "answerability recall gate failed" in report.failures
    assert "context byte gate failed" in report.failures


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_metadata_passes_versioned_quality_gates() -> None:
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")
    registry, evaluation = load_evaluation()
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog)
    try:
        report = await evaluation.evaluate(metadata)
    finally:
        await catalog.close()
    assert report.case_count == 16
    assert report.relation_accuracy == 1
    assert report.answerability_recall == 1
    assert report.max_context_bytes <= evaluation.gates.max_context_bytes
