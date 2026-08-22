from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from query_man.catalog import PostgresCatalog
from query_man.metadata import MetadataService
from query_man.query import PostgresQueryExecutor, QueryService
from query_man.registry import SourceRegistry
from query_man.sql_validation import validate_sql

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")]
RelationName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")]
Revision = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class VerifiedQueryConfigurationError(Exception):
    pass


class VerifiedQueryMismatchError(Exception):
    pass


@dataclass(frozen=True)
class ExpectedResult:
    columns: tuple[str, ...]
    row_count: int
    result_hash: str


@dataclass(frozen=True)
class VerifiedQuery:
    query_id: str
    source_id: str
    question: str
    sql: str
    metadata_revision: str
    relations: tuple[str, ...]
    expected: ExpectedResult


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Expected(_StrictModel):
    columns: list[str] = Field(min_length=1, max_length=1_600)
    row_count: int = Field(ge=0, le=100_000)
    result_hash: Revision


class _Query(_StrictModel):
    query_id: Identifier
    source_id: Identifier
    question: str = Field(min_length=1, max_length=2_000)
    sql: str = Field(min_length=1, max_length=100_000)
    metadata_revision: Revision
    relations: list[RelationName] = Field(min_length=1, max_length=100)
    expected: _Expected


class _VerifiedFile(_StrictModel):
    version: int
    queries: list[_Query] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def valid_file(self) -> _VerifiedFile:
        if self.version != 1:
            raise ValueError("version must be 1")
        query_ids = [query.query_id for query in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise ValueError("query_id values must be unique")
        if any(len(set(query.relations)) != len(query.relations) for query in self.queries):
            raise ValueError("relations must be unique per query")
        if any(len(set(query.expected.columns)) != len(query.expected.columns) for query in self.queries):
            raise ValueError("expected columns must be unique per query")
        return self


class VerifiedQueryRegistry:
    def __init__(self, queries: list[VerifiedQuery]) -> None:
        self.queries = tuple(queries)

    @classmethod
    def load(cls, path: Path, known_sources: set[str]) -> VerifiedQueryRegistry:
        try:
            with path.open(encoding="utf-8") as stream:
                parsed = _VerifiedFile.model_validate(yaml.safe_load(stream))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise VerifiedQueryConfigurationError(f"Invalid verified queries in {path}: {error}") from error
        queries: list[VerifiedQuery] = []
        for item in parsed.queries:
            if item.source_id not in known_sources:
                raise VerifiedQueryConfigurationError(
                    f"Verified query {item.query_id} references an unknown source"
                )
            queries.append(
                VerifiedQuery(
                    query_id=item.query_id,
                    source_id=item.source_id,
                    question=item.question,
                    sql=item.sql,
                    metadata_revision=item.metadata_revision,
                    relations=tuple(item.relations),
                    expected=ExpectedResult(
                        columns=tuple(item.expected.columns),
                        row_count=item.expected.row_count,
                        result_hash=item.expected.result_hash,
                    ),
                )
            )
        return cls(queries)

    async def verify_all(
        self,
        metadata: MetadataService,
        service: QueryService,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for query in self.queries:
            published = await metadata.get_published(query.source_id)
            if published.revision != query.metadata_revision:
                raise VerifiedQueryMismatchError(
                    f"{query.query_id}: metadata revision does not match the published snapshot"
                )
            validated = validate_sql(
                query.sql,
                allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
            )
            if validated.relations != tuple(sorted(query.relations)):
                raise VerifiedQueryMismatchError(
                    f"{query.query_id}: SQL relations do not match the verified contract"
                )
            response = await service.query(
                query.source_id,
                query.sql,
                query.metadata_revision,
            )
            columns_value = response["columns"]
            rows_value = response["rows"]
            if not isinstance(columns_value, list) or not all(
                isinstance(column, str) for column in columns_value
            ):
                raise VerifiedQueryMismatchError(f"{query.query_id}: result columns are invalid")
            if not isinstance(rows_value, list):
                raise VerifiedQueryMismatchError(f"{query.query_id}: result rows are invalid")
            actual_columns = tuple(columns_value)
            actual_hash = create_result_hash(actual_columns, rows_value)
            if response["truncated"]:
                raise VerifiedQueryMismatchError(f"{query.query_id}: verified result was truncated")
            if actual_columns != query.expected.columns:
                raise VerifiedQueryMismatchError(f"{query.query_id}: result columns changed")
            if response["row_count"] != query.expected.row_count:
                raise VerifiedQueryMismatchError(f"{query.query_id}: result row count changed")
            if actual_hash != query.expected.result_hash:
                raise VerifiedQueryMismatchError(f"{query.query_id}: result values changed")
            results.append(
                {
                    "query_id": query.query_id,
                    "source_id": query.source_id,
                    "metadata_revision": query.metadata_revision,
                    "row_count": response["row_count"],
                    "result_hash": actual_hash,
                }
            )
        return results


def create_result_hash(columns: tuple[str, ...], rows: object) -> str:
    payload = {"columns": columns, "rows": rows}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify versioned golden SQL against live sources")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.root.resolve()))


async def _run(root: Path) -> None:
    load_dotenv(root / ".env")
    registry = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    verified = VerifiedQueryRegistry.load(
        root / "config" / "verified-queries.yaml",
        {source["source_id"] for source in registry.list()},
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog)
    service = QueryService(registry, metadata, executor)
    try:
        results = await verified.verify_all(metadata, service)
        print(json.dumps({"status": "ok", "verified": results}, sort_keys=True))
    finally:
        await executor.close()
        await catalog.close()
