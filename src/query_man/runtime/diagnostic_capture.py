from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from query_man.delivery.access import CallerContext
from query_man.guarded_query.diagnostics import redact_sql_literals
from query_man.runtime.operations import operations

logger = logging.getLogger("query_man")

DIAGNOSTIC_CAPTURE_SCHEMA_VERSION = 1
DIAGNOSTIC_CAPTURE_TTL_DAYS = 7
DIAGNOSTIC_CAPTURE_QUEUE_SIZE = 64
DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES = 100 * 1024 * 1024
_MAX_QUESTION_BYTES = 8_000
_KEY_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RETENTION_SWEEP_SECONDS = 3_600


class DiagnosticCaptureConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class _PendingCapture:
    capture_id: str
    consent_handle: str
    captured_at: datetime
    expires_at: datetime
    payload: dict[str, object]


class _CaptureLifecycle(Enum):
    STOPPED = auto()
    ACCEPTING = auto()
    DRAINING = auto()
    STOPPING = auto()


class EncryptedDiagnosticCapture:
    def __init__(
        self,
        database: Path,
        key: bytes,
        key_id: str,
        *,
        daily_byte_budget: int = DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES,
    ) -> None:
        if len(key) != 32:
            raise DiagnosticCaptureConfigurationError(
                "The diagnostic capture key must contain 32 bytes"
            )
        if not 1 <= len(key_id) <= 80 or _KEY_ID.fullmatch(key_id) is None:
            raise DiagnosticCaptureConfigurationError(
                "The diagnostic capture key ID must be a 1-80 character lowercase slug"
            )
        if not 1_048_576 <= daily_byte_budget <= 10_737_418_240:
            raise DiagnosticCaptureConfigurationError(
                "The diagnostic capture daily byte budget must be between 1 MiB and 10 GiB"
            )
        self._database = database
        self._key_id = key_id
        self._daily_byte_budget = daily_byte_budget
        self._cipher = AESGCM(
            hmac.new(
                key,
                b"query-man/diagnostic-capture/encryption-key/v1",
                hashlib.sha256,
            ).digest()
        )
        self._subject_key = hmac.new(
            key,
            b"query-man/diagnostic-capture/subject-key/v1",
            hashlib.sha256,
        ).digest()
        self._queue: queue.Queue[_PendingCapture | None] = queue.Queue(
            maxsize=DIAGNOSTIC_CAPTURE_QUEUE_SIZE
        )
        self._lifecycle_lock = threading.Lock()
        self._lifecycle = _CaptureLifecycle.STOPPED
        self._worker: threading.Thread | None = None
        self._active_connection: sqlite3.Connection | None = None

    @classmethod
    def from_base64(
        cls,
        database: Path,
        encoded_key: str,
        key_id: str,
        *,
        daily_byte_budget: int = DEFAULT_DIAGNOSTIC_CAPTURE_DAILY_BYTES,
    ) -> EncryptedDiagnosticCapture:
        try:
            key = base64.b64decode(
                encoded_key.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise DiagnosticCaptureConfigurationError(
                "The diagnostic capture key is invalid"
            ) from error
        return cls(database, key, key_id, daily_byte_budget=daily_byte_budget)

    def subject_id(self, tenant_id: str, caller_id: str) -> str:
        digest = hmac.new(
            self._subject_key,
            b"query-man/diagnostic-subject/v1\x00"
            + tenant_id.encode("utf-8")
            + b"\x00"
            + caller_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()[:18]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def consent_handle(self, receipt_id: str) -> str:
        digest = hmac.new(
            self._subject_key,
            b"query-man/diagnostic-consent/v1\x00" + receipt_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()[:18]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def start(self) -> None:
        with self._lifecycle_lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            connection = self._connect()
            connection.close()
            worker = threading.Thread(
                target=self._run,
                name="query-man-diagnostic-capture",
                daemon=True,
            )
            self._lifecycle = _CaptureLifecycle.ACCEPTING
            self._worker = worker
            try:
                worker.start()
            except BaseException:
                self._worker = None
                self._lifecycle = _CaptureLifecycle.STOPPED
                raise

    async def close(self, timeout_ms: int) -> None:
        timeout_seconds = max(0, timeout_ms) / 1_000
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        interrupt_window = min(0.1, timeout_seconds / 2)
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return
            if self._lifecycle is _CaptureLifecycle.ACCEPTING:
                self._lifecycle = _CaptureLifecycle.DRAINING
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        await self._wait_for_worker(worker, deadline - interrupt_window)
        if worker.is_alive():
            with self._lifecycle_lock:
                self._lifecycle = _CaptureLifecycle.STOPPING
                if self._active_connection is not None:
                    self._active_connection.interrupt()
                dropped = 0
                while True:
                    try:
                        queued = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._queue.task_done()
                    if queued is not None:
                        dropped += 1
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass
            if dropped:
                operations.increment("diagnostic_capture_dropped", value=dropped)
                operations.increment("diagnostic_capture_shutdown_dropped", value=dropped)
            await self._wait_for_worker(worker, deadline)
        # NOTE: bounded in-process shutdown cannot stop a worker stalled
        # in filesystem open/commit; use a killable worker process if all
        # post-deadline storage activity must become impossible.

    async def _wait_for_worker(
        self,
        worker: threading.Thread,
        deadline: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        while worker.is_alive():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.01, remaining))

    def capture_question(
        self,
        caller: CallerContext,
        source_id: str,
        question: str,
    ) -> None:
        if len(question.encode("utf-8")) > _MAX_QUESTION_BYTES:
            operations.increment("diagnostic_capture_dropped")
            return
        self._submit(
            caller,
            source_id,
            {
                "operation": "get_context",
                "question": question,
            },
        )

    def capture_sql(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        query_id: str,
    ) -> None:
        redacted_sql = redact_sql_literals(sql)
        self._submit(
            caller,
            source_id,
            {
                "operation": "query",
                "query_id": query_id,
                "sql_literal_redacted": redacted_sql,
                "sql_parseable": redacted_sql is not None,
                "sql_bytes": len(sql.encode("utf-8")),
            },
        )

    def _submit(
        self,
        caller: CallerContext,
        source_id: str,
        request: dict[str, object],
    ) -> None:
        consent = caller.diagnostic_consent
        now = datetime.now(UTC)
        if consent is None or not consent.is_active(now):
            return
        pending = _PendingCapture(
            capture_id=str(uuid.uuid4()),
            consent_handle=self.consent_handle(consent.receipt_id),
            captured_at=now,
            expires_at=min(
                now + timedelta(days=DIAGNOSTIC_CAPTURE_TTL_DAYS),
                consent.expires_at,
            ),
            payload={
                "schema_version": DIAGNOSTIC_CAPTURE_SCHEMA_VERSION,
                "subject_id": self.subject_id(caller.tenant_id, caller.caller_id),
                "subject_key_id": self._key_id,
                "consent": {
                    "version": consent.version,
                    "receipt_id": consent.receipt_id,
                    "expires_at": consent.expires_at.isoformat(),
                },
                "source_id": source_id,
                "request": request,
            },
        )
        queue_full = False
        with self._lifecycle_lock:
            if self._lifecycle is not _CaptureLifecycle.ACCEPTING:
                return
            try:
                self._queue.put_nowait(pending)
            except queue.Full:
                queue_full = True
        if queue_full:
            operations.increment("diagnostic_capture_dropped")
            operations.increment("diagnostic_capture_queue_dropped")
            return
        operations.increment("diagnostic_capture_enqueued")

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=_RETENTION_SWEEP_SECONDS)
                except queue.Empty:
                    if self._worker_should_exit():
                        return
                    try:
                        self._cleanup_expired(datetime.now(UTC), worker_owned=True)
                    except Exception:
                        operations.increment("diagnostic_capture_storage_failed")
                        logger.exception("diagnostic_capture_retention_failed")
                    continue
                if item is None:
                    self._queue.task_done()
                    if self._worker_should_exit():
                        return
                    continue
                try:
                    outcome, stored_bytes = self._store(item)
                    if outcome == "stored":
                        operations.increment("diagnostic_capture_stored")
                        operations.observe("diagnostic_capture_bytes", stored_bytes)
                    elif outcome == "budget":
                        operations.increment("diagnostic_capture_dropped")
                        operations.increment("diagnostic_capture_budget_dropped")
                    else:
                        operations.increment("diagnostic_capture_dropped")
                        operations.increment("diagnostic_capture_shutdown_dropped")
                except Exception:
                    operations.increment("diagnostic_capture_dropped")
                    if self._stopping_requested():
                        operations.increment("diagnostic_capture_shutdown_dropped")
                    else:
                        operations.increment("diagnostic_capture_storage_failed")
                        logger.exception("diagnostic_capture_storage_failed")
                finally:
                    self._queue.task_done()
                if self._worker_should_exit():
                    return
        finally:
            with self._lifecycle_lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                    self._active_connection = None
                    self._lifecycle = _CaptureLifecycle.STOPPED

    def _worker_should_exit(self) -> bool:
        with self._lifecycle_lock:
            return (
                self._lifecycle is not _CaptureLifecycle.ACCEPTING
                and self._queue.empty()
            )

    def _stopping_requested(self) -> bool:
        with self._lifecycle_lock:
            return self._lifecycle is _CaptureLifecycle.STOPPING

    def _store(self, pending: _PendingCapture) -> tuple[str, int]:
        plaintext = json.dumps(
            {
                "capture_id": pending.capture_id,
                "captured_at": pending.captured_at.isoformat(),
                "expires_at": pending.expires_at.isoformat(),
                **pending.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        associated_data = _associated_data(
            pending.capture_id,
            self._key_id,
            pending.consent_handle,
            pending.captured_at,
            pending.expires_at,
        )
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, associated_data)
        stored_bytes = len(nonce) + len(ciphertext)
        if self._stopping_requested():
            return "shutdown", 0

        connection = self._connect(worker_owned=True)
        try:
            if self._stopping_requested():
                return "shutdown", 0
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM diagnostic_capture WHERE expires_at <= ?",
                (pending.captured_at.isoformat(),),
            )
            day = pending.captured_at.date().isoformat()
            connection.execute(
                "DELETE FROM diagnostic_daily_usage WHERE day < ?",
                (day,),
            )
            row = connection.execute(
                "SELECT stored_bytes FROM diagnostic_daily_usage WHERE day = ?",
                (day,),
            ).fetchone()
            used_bytes = 0 if row is None else int(row[0])
            if used_bytes + stored_bytes > self._daily_byte_budget:
                connection.rollback()
                return "budget", 0
            if self._stopping_requested():
                connection.rollback()
                return "shutdown", 0
            connection.execute(
                """
                INSERT INTO diagnostic_capture(
                    capture_id, key_id, consent_handle, captured_at, expires_at,
                    nonce, ciphertext
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.capture_id,
                    self._key_id,
                    pending.consent_handle,
                    pending.captured_at.isoformat(),
                    pending.expires_at.isoformat(),
                    nonce,
                    ciphertext,
                ),
            )
            connection.execute(
                """
                INSERT INTO diagnostic_daily_usage(day, stored_bytes)
                VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE
                SET stored_bytes = diagnostic_daily_usage.stored_bytes + excluded.stored_bytes
                """,
                (day, stored_bytes),
            )
            if self._stopping_requested():
                connection.rollback()
                return "shutdown", 0
            # The preceding locked state read is the commit-admission point.
            # sqlite3 has no atomic commit-vs-interrupt gate, so an admitted
            # commit may outlive bounded close once it is in progress.
            connection.commit()
            return "stored", stored_bytes
        finally:
            self._release_active_connection(connection)
            connection.close()

    def _cleanup_expired(self, now: datetime, *, worker_owned: bool = False) -> None:
        if not self._database.exists():
            return
        connection = self._connect(worker_owned=worker_owned)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM diagnostic_capture WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            connection.execute(
                "DELETE FROM diagnostic_daily_usage WHERE day < ?",
                (now.date().isoformat(),),
            )
            connection.commit()
        finally:
            if worker_owned:
                self._release_active_connection(connection)
            connection.close()

    def _release_active_connection(self, connection: sqlite3.Connection) -> None:
        with self._lifecycle_lock:
            if self._active_connection is connection:
                self._active_connection = None

    def _connect(self, *, worker_owned: bool = False) -> sqlite3.Connection:
        _prepare_database_path(self._database)
        connection = sqlite3.connect(
            self._database,
            timeout=0 if worker_owned else 1,
        )
        if worker_owned:
            with self._lifecycle_lock:
                self._active_connection = connection
                if self._lifecycle is _CaptureLifecycle.STOPPING:
                    connection.interrupt()
        try:
            connection.execute(
                "PRAGMA busy_timeout=0"
                if worker_owned
                else "PRAGMA busy_timeout=1000"
            )
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, DIAGNOSTIC_CAPTURE_SCHEMA_VERSION}:
                raise DiagnosticCaptureConfigurationError(
                    "The diagnostic capture database schema version is unsupported"
                )
            if version == 0:
                connection.execute("PRAGMA auto_vacuum=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_capture(
                    capture_id TEXT PRIMARY KEY,
                    key_id TEXT NOT NULL,
                    consent_handle TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    nonce BLOB NOT NULL CHECK(length(nonce) = 12),
                    ciphertext BLOB NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS diagnostic_capture_expires_at_idx
                ON diagnostic_capture(expires_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS diagnostic_capture_consent_handle_idx
                ON diagnostic_capture(key_id, consent_handle)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_daily_usage(
                    day TEXT PRIMARY KEY,
                    stored_bytes INTEGER NOT NULL CHECK(stored_bytes >= 0)
                ) STRICT
                """
            )
            if version == 0:
                connection.execute(
                    f"PRAGMA user_version={DIAGNOSTIC_CAPTURE_SCHEMA_VERSION}"
                )
            connection.commit()
            _verify_schema(connection)
            return connection
        except BaseException:
            if worker_owned:
                self._release_active_connection(connection)
            connection.close()
            raise


def decrypt_diagnostic_records(
    database: Path,
    encoded_key: str,
    key_id: str,
) -> tuple[dict[str, Any], ...]:
    """Decrypt records for an explicitly authorized offline operator workflow."""

    capture = EncryptedDiagnosticCapture.from_base64(database, encoded_key, key_id)
    connection = capture._connect()
    try:
        now = datetime.now(UTC).isoformat()
        rows = connection.execute(
            """
            SELECT capture_id, key_id, consent_handle, captured_at, expires_at,
                   nonce, ciphertext
            FROM diagnostic_capture
            WHERE key_id = ? AND expires_at > ?
            ORDER BY captured_at, capture_id
            """,
            (key_id, now),
        ).fetchall()
    finally:
        connection.close()
    return _decrypt_rows(capture, rows)


def query_diagnostic_records(
    database: Path,
    encoded_key: str,
    key_id: str,
    *,
    since: datetime,
    limit: int = 20,
    capture_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read a bounded recent diagnostic window for an authorized operator."""

    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("Diagnostic query time must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Diagnostic query limit must be between 1 and 100")
    if capture_id is not None:
        try:
            capture_id = str(uuid.UUID(capture_id))
        except ValueError as error:
            raise ValueError("Diagnostic capture ID must be a UUID") from error

    capture = EncryptedDiagnosticCapture.from_base64(database, encoded_key, key_id)
    connection = capture._connect()
    try:
        now = datetime.now(UTC).isoformat()
        parameters: list[object] = [key_id, now, since.astimezone(UTC).isoformat()]
        capture_clause = ""
        if capture_id is not None:
            capture_clause = " AND capture_id = ?"
            parameters.append(capture_id)
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT capture_id, key_id, consent_handle, captured_at, expires_at,
                   nonce, ciphertext
            FROM diagnostic_capture
            WHERE key_id = ? AND expires_at > ? AND captured_at >= ?
                  {capture_clause}
            ORDER BY captured_at DESC, capture_id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    finally:
        connection.close()
    return _decrypt_rows(capture, rows)


def _decrypt_rows(
    capture: EncryptedDiagnosticCapture,
    rows: list[tuple[Any, ...]],
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for (
        capture_id,
        stored_key_id,
        consent_handle,
        captured_at,
        expires_at,
        nonce,
        ciphertext,
    ) in rows:
        try:
            plaintext = capture._cipher.decrypt(
                nonce,
                ciphertext,
                _associated_data(
                    capture_id,
                    stored_key_id,
                    consent_handle,
                    datetime.fromisoformat(captured_at),
                    datetime.fromisoformat(expires_at),
                ),
            )
            decoded = json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise DiagnosticCaptureConfigurationError(
                "A diagnostic capture record could not be decrypted"
            ) from error
        if not isinstance(decoded, dict):
            raise DiagnosticCaptureConfigurationError(
                "A diagnostic capture record has an invalid payload"
            )
        records.append(decoded)
    return tuple(records)


def purge_diagnostic_consent(
    database: Path,
    encoded_key: str,
    key_id: str,
    receipt_id: str,
) -> int:
    """Delete captures for one consent receipt in an authorized offline workflow."""

    capture = EncryptedDiagnosticCapture.from_base64(database, encoded_key, key_id)
    connection = capture._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "DELETE FROM diagnostic_capture WHERE key_id = ? AND consent_handle = ?",
            (key_id, capture.consent_handle(receipt_id)),
        )
        deleted = cursor.rowcount
        connection.commit()
        return deleted
    finally:
        connection.close()


def _associated_data(
    capture_id: str,
    key_id: str,
    consent_handle: str,
    captured_at: datetime,
    expires_at: datetime,
) -> bytes:
    return (
        f"query-man/diagnostic-capture/v{DIAGNOSTIC_CAPTURE_SCHEMA_VERSION}/"
        f"{key_id}/{capture_id}/{consent_handle}/"
        f"{captured_at.isoformat()}/{expires_at.isoformat()}"
    ).encode("ascii")


def _prepare_database_path(database: Path) -> None:
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or database.is_symlink():
        raise DiagnosticCaptureConfigurationError(
            "The diagnostic capture database must be a regular file"
        )
    if metadata.st_mode & 0o077:
        raise DiagnosticCaptureConfigurationError(
            "The diagnostic capture database must not grant group or other permissions"
        )


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "diagnostic_capture": (
            ("capture_id", "TEXT", 1),
            ("key_id", "TEXT", 0),
            ("consent_handle", "TEXT", 0),
            ("captured_at", "TEXT", 0),
            ("expires_at", "TEXT", 0),
            ("nonce", "BLOB", 0),
            ("ciphertext", "BLOB", 0),
        ),
        "diagnostic_daily_usage": (
            ("day", "TEXT", 1),
            ("stored_bytes", "INTEGER", 0),
        ),
    }
    for table, expected_columns in expected.items():
        columns = tuple(
            (str(row[1]), str(row[2]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if columns != expected_columns:
            raise DiagnosticCaptureConfigurationError(
                "The diagnostic capture database schema is invalid"
            )
