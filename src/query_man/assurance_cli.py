from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from query_man.catalog import PostgresCatalog
from query_man.metadata import MetadataService
from query_man.quality import QualityEvaluation, QualityGateError
from query_man.query import PostgresQueryExecutor, QueryService
from query_man.registry import SourceReader, SourceRegistry
from query_man.verified import VerifiedQueryRegistry


def evaluate_main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate metadata retrieval quality gates")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    asyncio.run(_run_evaluation(arguments.root.resolve()))


async def _run_evaluation(root: Path) -> None:
    load_dotenv(root / ".env")
    registry: SourceReader = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    evaluation = QualityEvaluation.load(
        root / "config" / "quality-evaluation.yaml",
        {source["source_id"] for source in registry.list()},
    )
    verified = VerifiedQueryRegistry.load(
        root / "config" / "verified-queries.yaml",
        {source["source_id"] for source in registry.list()},
    )
    catalog = PostgresCatalog(reject_domain_columns=True)
    metadata = MetadataService(
        registry,
        catalog,
        verified_revisions=verified.revision_map(),
    )
    try:
        try:
            report = await evaluation.evaluate(metadata)
        except QualityGateError as error:
            print(json.dumps({"status": "failed", **asdict(error.report)}, sort_keys=True))
            raise SystemExit(1) from error
        print(json.dumps({"status": "ok", **asdict(report)}, sort_keys=True))
    finally:
        await catalog.close()


def verify_main() -> None:
    parser = argparse.ArgumentParser(description="Verify versioned golden SQL against live sources")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    asyncio.run(_run_verification(arguments.root.resolve()))


async def _run_verification(root: Path) -> None:
    load_dotenv(root / ".env")
    registry: SourceReader = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    verified = VerifiedQueryRegistry.load(
        root / "config" / "verified-queries.yaml",
        {source["source_id"] for source in registry.list()},
    )
    catalog = PostgresCatalog(reject_domain_columns=True)
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog)
    service = QueryService(registry, metadata, executor)
    try:
        results = await verified.verify_all(metadata, service)
        print(json.dumps({"status": "ok", "verified": results}, sort_keys=True))
    finally:
        await executor.close()
        await catalog.close()
