from __future__ import annotations

import io
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from query_man.runtime.operator_shell import (
    LogQuery,
    OperatorSettings,
    QueryManShell,
    _public_error,
    run_main,
)
from tests.helpers import ROOT_DIRECTORY


class FakeBackend:
    def __init__(self) -> None:
        self.mutations: list[tuple[object, ...]] = []
        self.diag_shown: list[str] = []
        self.diag_purged: list[str] = []

    def status(self, *, metrics: bool = False) -> dict[str, object]:
        return {"status": "ready", "metrics": metrics}

    def logs(self, _query: LogQuery) -> Any:
        yield (
            'query-man | {"timestamp":"2026-08-28T00:00:00+00:00",'
            '"level":"info","event":"query_succeeded","query_id":"query-1"}'
        )
        yield (
            'query-man | {"timestamp":"2026-08-28T00:00:01+00:00",'
            '"level":"warning","event":"query_failed","query_id":"query-2",'
            '"error_code":"QUERY_TIMEOUT"}'
        )

    def admin_get(
        self,
        path: str,
        parameters: dict[str, str | int] | None = None,
    ) -> dict[str, object]:
        if path == "/admin/sources":
            assert parameters == {"limit": 50}
            return {
                "sources": [
                    {
                        "source_id": "known-source",
                        "generation": 3,
                        "state_version": 9,
                    }
                ]
            }
        if path == "/admin/sources/known-source":
            return {
                "source_id": "known-source",
                "generation": 3,
                "state_version": 9,
            }
        raise AssertionError(path)

    def admin_mutate(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.mutations.append((method, path, headers, body))
        return {"status": "completed", "source_id": "known-source"}

    def diagnostic_list(
        self,
        *,
        since: datetime,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        assert since.tzinfo is UTC
        assert limit == 20
        return (
            {
                "capture_id": "123e4567-e89b-12d3-a456-426614174000",
                "captured_at": "2026-08-28T00:00:00+00:00",
                "expires_at": "2026-09-04T00:00:00+00:00",
                "subject_id": "pseudonymous",
                "source_id": "known-source",
                "request": {
                    "operation": "get_context",
                    "question": "private question",
                },
            },
        )

    def diagnostic_show(self, capture_id: str) -> dict[str, Any] | None:
        self.diag_shown.append(capture_id)
        return {
            "capture_id": capture_id,
            "request": {"operation": "get_context", "question": "private question"},
        }

    def diagnostic_purge(self, receipt_id: str) -> int:
        self.diag_purged.append(receipt_id)
        return 2


def _shell(
    backend: FakeBackend,
    *,
    input_text: str = "",
) -> tuple[QueryManShell, io.StringIO]:
    output = io.StringIO()
    return (
        QueryManShell(
            backend,
            stdin=io.StringIO(input_text),
            stdout=output,
            secret_reader=lambda _prompt: "private-credential",
        ),
        output,
    )


def test_console_script_targets_runtime_operator_shell() -> None:
    configuration = tomllib.loads(
        (ROOT_DIRECTORY / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["project"]["scripts"]["qm"] == (
        "query_man.runtime.operator_shell:main"
    )
    assert "query-manctl" not in configuration["project"]["scripts"]


def test_blank_unknown_and_missing_input_are_beginner_safe() -> None:
    shell, output = _shell(FakeBackend())

    assert shell.emptyline() is False
    shell.onecmd("stats")
    shell.onecmd("diag")
    shell.onecmd("source show")

    rendered = output.getvalue()
    assert "바로 사용할 수 있는 명령" in rendered
    assert "혹시 `status`인가요?" in rendered
    assert "진단 내용은 일반 로그보다 민감" in rendered
    assert "사용법: source show <source-id>" in rendered
    assert "Traceback" not in rendered


def test_tab_completion_covers_commands_subcommands_and_cached_sources() -> None:
    shell, _output = _shell(FakeBackend())

    assert "status" in shell.completenames("st")
    assert shell.complete_diag("l", "diag l", 5, 6) == ["list"]
    assert shell.complete_source("sh", "source sh", 7, 9) == ["show"]

    shell.onecmd("source")
    line = "source show kn"
    assert shell.complete_source("kn", line, line.index("kn"), len(line)) == [
        "known-source"
    ]


def test_logs_filter_and_render_structured_events() -> None:
    shell, output = _shell(FakeBackend())

    shell.onecmd("logs --event query_failed --since 2h")

    rendered = output.getvalue()
    assert "query_failed" in rendered
    assert "query-2" in rendered
    assert "QUERY_TIMEOUT" in rendered
    assert "query_succeeded" not in rendered


def test_diag_list_hides_content_and_show_requires_reason() -> None:
    backend = FakeBackend()
    shell, output = _shell(backend)

    shell.onecmd("diag list")
    shell.onecmd("diag show 123e4567-e89b-12d3-a456-426614174000")

    rendered = output.getvalue()
    assert "question_bytes" in rendered
    assert "private question" not in rendered
    assert "--reason <ticket-or-change-ref>" in rendered
    assert backend.diag_shown == []


def test_diag_show_and_purge_require_explicit_confirmation() -> None:
    capture_id = "123e4567-e89b-12d3-a456-426614174000"
    backend = FakeBackend()
    shell, output = _shell(
        backend,
        input_text=f"SHOW {capture_id}\nPURGE consent-internal-v1\n",
    )

    shell.onecmd(f"diag show {capture_id} --reason incident-123")
    shell.onecmd("diag purge consent-internal-v1 --reason privacy-123")

    assert backend.diag_shown == [capture_id]
    assert backend.diag_purged == ["consent-internal-v1"]
    assert "private question" in output.getvalue()
    assert "삭제 완료: 2건" in output.getvalue()


def test_source_mutation_uses_current_state_receipt_and_confirmation() -> None:
    backend = FakeBackend()
    shell, output = _shell(backend, input_text="DISABLE known-source\n")

    shell.onecmd("source disable known-source --reason incident-123")

    assert len(backend.mutations) == 1
    method, path, headers, body = backend.mutations[0]
    assert method == "DELETE"
    assert path == "/admin/sources/known-source"
    assert body is None
    assert headers["X-Expected-Generation"] == "3"
    assert headers["X-Expected-State-Version"] == "9"
    assert headers["X-Query-Man-Reason"] == "incident-123"
    assert headers["Idempotency-Key"]
    assert "Mutation 결과" in output.getvalue()


def test_source_apply_keeps_credential_out_of_command_output(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "source.yaml"
    manifest.write_text("version: 2\nsource_id: known-source\n", encoding="utf-8")
    backend = FakeBackend()
    shell, output = _shell(backend, input_text="APPLY known-source\n")

    shell.onecmd(f"source apply known-source {manifest} --reason change-123")

    assert len(backend.mutations) == 1
    method, _path, _headers, body = backend.mutations[0]
    assert method == "PUT"
    assert body == {
        "manifest": {"version": 2, "source_id": "known-source"},
        "credential": "private-credential",
    }
    assert "private-credential" not in output.getvalue()


def test_settings_discovers_operator_token_without_printing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = tmp_path / "config" / "access-policies.compose.yaml"
    policy.parent.mkdir()
    policy.write_text(
        """version: 2
callers:
  - caller_id: operator
    tenant_id: operations
    token_env: TEST_OPERATOR_TOKEN
    operator: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "x" * 32)
    monkeypatch.delenv("QM_TOKEN", raising=False)

    settings = OperatorSettings.load(tmp_path)

    assert settings.operator_token == "x" * 32


def test_settings_use_qm_environment_without_legacy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUERY_MAN_PORT", "3000")
    monkeypatch.setenv("QUERY_MANCTL_URL", "https://legacy.invalid")
    monkeypatch.setenv("QUERY_MANCTL_SERVICE", "legacy-service")
    monkeypatch.setenv("QUERY_MANCTL_TOKEN", "legacy-token")
    monkeypatch.delenv("QM_URL", raising=False)
    monkeypatch.delenv("QM_SERVICE", raising=False)
    monkeypatch.delenv("QM_TOKEN", raising=False)

    legacy_ignored = OperatorSettings.load(tmp_path)

    assert legacy_ignored.base_url == "http://127.0.0.1:3000"
    assert legacy_ignored.compose_service == "query-man"
    assert legacy_ignored.operator_token is None

    monkeypatch.setenv("QM_URL", "https://query-man.example")
    monkeypatch.setenv("QM_SERVICE", "query-man-test")
    monkeypatch.setenv("QM_TOKEN", "operator-token")

    settings = OperatorSettings.load(tmp_path)

    assert settings.base_url == "https://query-man.example"
    assert settings.compose_service == "query-man-test"
    assert settings.operator_token == "operator-token"


def test_nested_public_error_envelope_is_bounded() -> None:
    assert _public_error(
        json.dumps(
            {"error": {"code": "SOURCE_NOT_FOUND", "message": "Source not found."}}
        ).encode()
    ) == ("SOURCE_NOT_FOUND", "Source not found.")


def test_one_shot_help_does_not_start_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Backend:
        pass

    monkeypatch.setattr(
        "query_man.runtime.operator_shell.RealOperatorBackend",
        lambda _settings: Backend(),
    )
    monkeypatch.setattr(sys, "argv", ["qm", "help"])

    assert run_main(["help"]) == 0
    rendered = capsys.readouterr().out
    assert "Query Man 운영 쉘" in rendered
    assert "qm>" not in rendered


def test_one_shot_incomplete_command_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "query_man.runtime.operator_shell.RealOperatorBackend",
        lambda _settings: FakeBackend(),
    )

    assert run_main(["diag"]) == 1
    assert "예: diag list" in capsys.readouterr().out
