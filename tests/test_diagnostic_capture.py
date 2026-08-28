from __future__ import annotations

import base64
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from query_man.delivery.access import CallerContext, DiagnosticConsent
from query_man.delivery.gateway import GatewayService
from query_man.guarded_query.diagnostics import redact_sql_literals
from query_man.runtime.diagnostic_capture import (
    DIAGNOSTIC_CAPTURE_TTL_DAYS,
    DiagnosticCaptureConfigurationError,
    EncryptedDiagnosticCapture,
    decrypt_diagnostic_records,
    purge_diagnostic_consent,
    query_diagnostic_records,
)
from query_man.runtime.operations import operations

_KEY = base64.urlsafe_b64encode(b"diagnostic-capture-test-key-32b!").decode("ascii")


def _caller(*, expires_at: datetime | None = None) -> CallerContext:
    return CallerContext(
        caller_id="analyst-alice",
        tenant_id="engineering",
        diagnostic_consent=DiagnosticConsent(
            version=1,
            receipt_id="consent-2026-001",
            expires_at=expires_at or datetime.now(UTC) + timedelta(days=30),
        ),
    )


def _metrics() -> dict[str, int | float]:
    return {
        str(metric["name"]): metric["value"]
        for metric in operations.snapshot()["metrics"]
        if "source_id" not in metric
    }


def test_redacts_every_parsed_sql_constant_and_drops_comments() -> None:
    redacted = redact_sql_literals(
        """
        SELECT 'private-customer', 42, 3.14, DATE '2026-08-28',
               interval '7 days', B'101', X'ff', $$dollar-secret$$
        FROM ai.issue_overview -- another-secret
        WHERE issue_id = -99 AND status = 'OPEN'
        LIMIT 10
        """
    )

    assert redacted is not None
    assert "ai.issue_overview" in redacted
    assert "private-customer" not in redacted
    assert "dollar-secret" not in redacted
    assert "another-secret" not in redacted
    assert "2026-08-28" not in redacted
    assert "99" not in redacted
    assert redacted.count("NULL") >= 10
    assert redact_sql_literals("SELECT '") is None
    assert redact_sql_literals("SELECT 1; SELECT 2") is None
    assert redact_sql_literals("COPY ai.issue_overview TO '/tmp/private-file'") is None


@pytest.mark.asyncio
async def test_stores_consented_question_and_literal_redacted_sql_as_ciphertext(
    tmp_path: Path,
) -> None:
    operations.reset()
    database = tmp_path / "diagnostics" / "capture.sqlite3"
    capture = EncryptedDiagnosticCapture.from_base64(database, _KEY, "key-2026-08")
    caller = _caller()
    capture.start()

    capture.capture_question(caller, "development-issues", "고객 private-name의 문제를 보여줘")
    capture.capture_sql(
        caller,
        "development-issues",
        "SELECT 'private-literal', 42 FROM ai.issue_overview",
        "e11e8a07-d344-4651-8dd3-833c5b3c98a7",
    )
    await capture.close(2_000)

    records = decrypt_diagnostic_records(database, _KEY, "key-2026-08")
    assert len(records) == 2
    question = next(record for record in records if record["request"]["operation"] == "get_context")
    query = next(record for record in records if record["request"]["operation"] == "query")
    assert question["request"]["question"] == "고객 private-name의 문제를 보여줘"
    assert query["request"]["query_id"] == "e11e8a07-d344-4651-8dd3-833c5b3c98a7"
    assert query["request"]["sql_literal_redacted"] == (
        "SELECT NULL, NULL FROM ai.issue_overview"
    )
    assert query["request"]["sql_parseable"] is True
    assert records[0]["subject_id"] == records[1]["subject_id"]
    assert records[0]["subject_id"] not in {caller.caller_id, caller.tenant_id}
    assert records[0]["consent"] == {
        "version": 1,
        "receipt_id": "consent-2026-001",
        "expires_at": caller.diagnostic_consent.expires_at.isoformat(),
    }
    captured_at = datetime.fromisoformat(str(records[0]["captured_at"]))
    expires_at = datetime.fromisoformat(str(records[0]["expires_at"]))
    assert expires_at - captured_at == timedelta(days=DIAGNOSTIC_CAPTURE_TTL_DAYS)

    stored = database.read_bytes()
    assert b"private-name" not in stored
    assert b"private-literal" not in stored
    assert b"analyst-alice" not in stored
    assert b"engineering" not in stored
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert _metrics()["diagnostic_capture_stored"] == 2
    recent = query_diagnostic_records(
        database,
        _KEY,
        "key-2026-08",
        since=datetime.now(UTC) - timedelta(hours=1),
        limit=1,
    )
    assert len(recent) == 1
    assert recent[0]["request"]["operation"] == "query"
    with pytest.raises(ValueError, match="timezone-aware"):
        query_diagnostic_records(
            database,
            _KEY,
            "key-2026-08",
            since=datetime.now(),
        )
    with pytest.raises(ValueError, match="between 1 and 100"):
        query_diagnostic_records(
            database,
            _KEY,
            "key-2026-08",
            since=datetime.now(UTC) - timedelta(hours=1),
            limit=101,
        )
    assert purge_diagnostic_consent(
        database,
        _KEY,
        "key-2026-08",
        "consent-2026-001",
    ) == 2
    assert decrypt_diagnostic_records(database, _KEY, "key-2026-08") == ()


@pytest.mark.asyncio
async def test_expired_or_missing_consent_is_not_captured(tmp_path: Path) -> None:
    operations.reset()
    database = tmp_path / "capture.sqlite3"
    capture = EncryptedDiagnosticCapture.from_base64(database, _KEY, "key-2026-08")
    capture.start()

    capture.capture_question(
        _caller(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        "development-issues",
        "expired private question",
    )
    capture.capture_question(
        CallerContext("analyst-bob", "engineering"),
        "development-issues",
        "missing consent private question",
    )
    await capture.close(2_000)

    assert database.exists()
    assert decrypt_diagnostic_records(database, _KEY, "key-2026-08") == ()
    assert "diagnostic_capture_enqueued" not in _metrics()


@pytest.mark.asyncio
async def test_daily_budget_and_retention_are_hard_bounds(tmp_path: Path) -> None:
    operations.reset()
    database = tmp_path / "capture.sqlite3"
    capture = EncryptedDiagnosticCapture.from_base64(database, _KEY, "key-2026-08")
    capture._daily_byte_budget = 1
    capture.start()
    capture.capture_question(_caller(), "development-issues", "bounded question")
    await capture.close(2_000)

    assert decrypt_diagnostic_records(database, _KEY, "key-2026-08") == ()
    assert _metrics()["diagnostic_capture_budget_dropped"] == 1

    capture = EncryptedDiagnosticCapture.from_base64(database, _KEY, "key-2026-08")
    capture.start()
    capture.capture_question(_caller(), "development-issues", "expiring question")
    await capture.close(2_000)
    assert len(decrypt_diagnostic_records(database, _KEY, "key-2026-08")) == 1

    capture._cleanup_expired(datetime.now(UTC) + timedelta(days=8))
    assert decrypt_diagnostic_records(database, _KEY, "key-2026-08") == ()


@pytest.mark.asyncio
async def test_capture_failure_never_changes_gateway_result() -> None:
    class Metadata:
        async def get_context(
            self,
            source_id: str,
            _question: str,
            _max_objects: int,
        ) -> dict[str, object]:
            return {"source_id": source_id}

    class Registry:
        def get(self, source_id: str) -> object | None:
            return object() if source_id == "development-issues" else None

    class RaisingCapture:
        def capture_question(self, *_args: object) -> None:
            raise RuntimeError("private capture storage error")

        def capture_sql(self, *_args: object) -> None:
            raise RuntimeError("private capture storage error")

    operations.reset()
    gateway = GatewayService(
        Registry(),  # type: ignore[arg-type]
        Metadata(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        diagnostic_capture=RaisingCapture(),
    )

    result = await gateway.get_context(
        _caller(),
        "development-issues",
        "private question",
        2,
    )

    assert result == {"source_id": "development-issues"}
    assert _metrics()["diagnostic_capture_submit_failed"] == 1


@pytest.mark.asyncio
async def test_key_identity_and_envelope_tampering_fail_closed(tmp_path: Path) -> None:
    operations.reset()
    database = tmp_path / "capture.sqlite3"
    capture = EncryptedDiagnosticCapture.from_base64(database, _KEY, "key-2026-08")
    same_key = EncryptedDiagnosticCapture.from_base64(
        tmp_path / "same.sqlite3",
        _KEY,
        "key-2026-08",
    )
    other_key = base64.urlsafe_b64encode(b"another-diagnostic-capture-key!!").decode("ascii")
    different_key = EncryptedDiagnosticCapture.from_base64(
        tmp_path / "other.sqlite3",
        other_key,
        "key-2026-09",
    )
    assert capture.subject_id("engineering", "analyst") == same_key.subject_id(
        "engineering", "analyst"
    )
    assert capture.subject_id("engineering", "analyst") != different_key.subject_id(
        "engineering", "analyst"
    )

    capture.start()
    capture.capture_question(_caller(), "development-issues", "private question")
    await capture.close(2_000)
    wrong_key = base64.urlsafe_b64encode(b"wrong-diagnostic-capture-key-32!").decode("ascii")
    with pytest.raises(DiagnosticCaptureConfigurationError, match="could not be decrypted"):
        decrypt_diagnostic_records(database, wrong_key, "key-2026-08")

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE diagnostic_capture SET captured_at = '2026-01-01T00:00:00+00:00'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(DiagnosticCaptureConfigurationError, match="could not be decrypted"):
        decrypt_diagnostic_records(database, _KEY, "key-2026-08")


def test_rejects_unsupported_capture_schema_before_read(tmp_path: Path) -> None:
    database = tmp_path / "capture.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=2")
    connection.close()
    database.chmod(0o600)

    with pytest.raises(DiagnosticCaptureConfigurationError, match="version is unsupported"):
        decrypt_diagnostic_records(database, _KEY, "key-2026-08")
