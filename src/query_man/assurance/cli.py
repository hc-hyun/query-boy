from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout

from query_man.assurance.quality import QualityEvaluation, QualityGateError
from query_man.assurance.verified import VerifiedQueryRegistry
from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.reader_policy import ReaderSessionPolicyError
from query_man.source_catalog.registry import SourceReader, SourceRegistry

_DATABASE_DEPENDENCY_LOGGERS = ("psycopg", "psycopg_pool")
_DATABASE_DEPENDENCY_LOG = json.dumps(
    {"event": "database_dependency_log"},
    sort_keys=True,
)
_DATABASE_UNAVAILABLE_OUTPUT = json.dumps(
    {"status": "failed", "error_code": "DATABASE_UNAVAILABLE"},
    sort_keys=True,
)
_DatabaseLoggerState = tuple[int, list[logging.Handler], bool, bool]


class _DatabaseDependencyFormatter(logging.Formatter):
    def format(self, _record: logging.LogRecord) -> str:
        return _DATABASE_DEPENDENCY_LOG


def evaluate_main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate metadata retrieval quality gates")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    _run_cli(_run_evaluation(arguments.root.resolve()))


async def _run_evaluation(root: Path) -> None:
    load_dotenv(root / ".env")
    registry: SourceReader = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    known_sources = set(registry.source_ids())
    evaluation = QualityEvaluation.load(
        root / "config" / "quality-evaluation.yaml",
        known_sources,
    )
    verified = VerifiedQueryRegistry.load(
        root / "config" / "verified-queries.yaml",
        known_sources,
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
    _run_cli(_run_verification(arguments.root.resolve()))


async def _run_verification(root: Path) -> None:
    load_dotenv(root / ".env")
    registry: SourceReader = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    verified = VerifiedQueryRegistry.load(
        root / "config" / "verified-queries.yaml",
        set(registry.source_ids()),
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


def _run_cli(operation: Coroutine[Any, Any, None]) -> None:
    with _database_dependency_logging():
        try:
            asyncio.run(operation)
        except Exception as error:
            if not _is_database_dependency_failure(error):
                raise
            print(_DATABASE_UNAVAILABLE_OUTPUT, file=sys.stderr)
            raise SystemExit(1) from None


def _is_database_dependency_failure(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(
            current,
            (PsycopgError, PoolTimeout, ReaderSessionPolicyError),
        ):
            return True
        for chained in (current.__cause__, current.__context__):
            if chained is not None:
                pending.append(chained)
    return False


def _is_database_dependency_logger(name: str) -> bool:
    return any(
        name == prefix or name.startswith(f"{prefix}.")
        for prefix in _DATABASE_DEPENDENCY_LOGGERS
    )


@contextmanager
def _database_dependency_logging() -> Iterator[None]:
    previous_factory = logging.getLogRecordFactory()

    def safe_record_factory(
        name: str,
        level: int,
        pathname: str,
        lineno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        sinfo: str | None = None,
        **kwargs: Any,
    ) -> logging.LogRecord:
        database_dependency = _is_database_dependency_logger(name)
        record = previous_factory(
            name,
            level,
            pathname,
            lineno,
            _DATABASE_DEPENDENCY_LOG if database_dependency else msg,
            () if database_dependency else args,
            None if database_dependency else exc_info,
            func,
            None if database_dependency else sinfo,
            **kwargs,
        )
        if database_dependency:
            record.msg = _DATABASE_DEPENDENCY_LOG
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record

    handler = logging.StreamHandler()
    handler.setFormatter(_DatabaseDependencyFormatter())
    states: dict[logging.Logger, _DatabaseLoggerState] = {}
    logging.setLogRecordFactory(safe_record_factory)
    try:
        for name in _DATABASE_DEPENDENCY_LOGGERS:
            logger = logging.getLogger(name)
            states[logger] = (
                logger.level,
                list(logger.handlers),
                logger.propagate,
                logger.disabled,
            )
            logger.handlers = [handler]
            logger.setLevel(logging.WARNING)
            logger.propagate = False
            logger.disabled = False
        yield
    finally:
        for logger, (level, handlers, propagate, disabled) in states.items():
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
        logging.setLogRecordFactory(previous_factory)
        handler.close()
