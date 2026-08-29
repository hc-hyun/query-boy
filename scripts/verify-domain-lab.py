from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from query_man.assurance.quality import QualityEvaluation, QualityGateError
from query_man.assurance.verified import VerifiedQueryMismatchError, VerifiedQueryRegistry
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.registry import SourceRegistry

ROOT_DIRECTORY = Path(__file__).resolve().parents[1]
DOMAIN_CONFIG = ROOT_DIRECTORY / "config" / "domain-lab"


async def _verify() -> dict[str, object]:
    registry = SourceRegistry.load(
        DOMAIN_CONFIG / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    source_ids = set(registry.source_ids())
    verified = VerifiedQueryRegistry.load(
        DOMAIN_CONFIG / "verified-queries.yaml",
        source_ids,
    )
    evaluation = QualityEvaluation.load(
        DOMAIN_CONFIG / "quality-evaluation.yaml",
        source_ids,
    )
    catalog = PostgresCatalog(reject_domain_columns=True)
    executor = PostgresQueryExecutor()
    metadata = MetadataService(
        registry,
        catalog,
        verified_revisions=verified.revision_map(),
    )
    query = QueryService(registry, metadata, executor)
    try:
        quality = await evaluation.evaluate(metadata)
        results = await verified.verify_all(metadata, query)
    finally:
        await executor.close()
        await catalog.close()
    verified_by_source = Counter(str(result["source_id"]) for result in results)
    return {
        "status": "ok",
        "quality": asdict(quality),
        "verified_count": len(results),
        "verified_by_source": dict(sorted(verified_by_source.items())),
    }


def main() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    if os.environ.get("QUERY_MAN_DOMAIN_LAB") != "1":
        print(json.dumps({"status": "failed", "reason": "domain_lab_marker_required"}))
        raise SystemExit(2)
    os.environ["QUERY_MAN_POSTGRES_HOST"] = os.environ.get(
        "QUERY_MAN_DOMAIN_DB_HOST",
        "127.0.0.1",
    )
    os.environ["POSTGRES_PORT"] = os.environ.get(
        "QUERY_MAN_DOMAIN_DB_PORT",
        "55434",
    )
    try:
        report = asyncio.run(_verify())
    except QualityGateError as error:
        print(
            json.dumps(
                {"status": "failed", "stage": "quality", **asdict(error.report)},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    except VerifiedQueryMismatchError as error:
        print(
            json.dumps(
                {"status": "failed", "stage": "verified", "reason": str(error)},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "reason": "domain_lab_verification_failed"},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
