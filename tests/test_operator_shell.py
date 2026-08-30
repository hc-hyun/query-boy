from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tomllib
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from query_man.runtime.operator_backend import (
    LogQuery,
    OperatorSettings,
    OperatorShellError,
    RealOperatorBackend,
    _public_error,
)
from query_man.runtime.operator_shell import (
    QueryManShell,
    run_main,
)
from tests.helpers import ROOT_DIRECTORY


class FakeBackend:
    def __init__(self) -> None:
        self.source_calls: list[tuple[str, ...]] = []
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

    def source_list(self) -> dict[str, object]:
        self.source_calls.append(("list",))
        return {
            "authority": "yaml",
            "source_count": 1,
            "sources": [
                {
                    "path": "config/sources/known-source.yaml",
                    "source_id": "known-source",
                    "name": "Known source",
                }
            ],
        }

    def source_show(self, source_id: str) -> dict[str, object]:
        self.source_calls.append(("show", source_id))
        return {
            "authority": "yaml",
            "path": f"config/sources/{source_id}.yaml",
            "manifest": {"version": 2, "source_id": source_id},
        }

    def source_validate(self) -> dict[str, object]:
        self.source_calls.append(("validate",))
        return {
            "status": "valid",
            "authority": "yaml",
            "live_database_checked": False,
        }

    def unexpected_http_call(
        self,
        *_arguments: object,
        **_keyword_arguments: object,
    ) -> dict[str, object]:
        raise AssertionError("source command must not use HTTP")

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
    backend: Any,
    *,
    input_text: str = "",
) -> tuple[QueryManShell, io.StringIO]:
    output = io.StringIO()
    return (
        QueryManShell(
            backend,
            stdin=io.StringIO(input_text),
            stdout=output,
        ),
        output,
    )


def _real_backend(root: Path) -> RealOperatorBackend:
    return RealOperatorBackend(
        OperatorSettings(
            root=root.resolve(),
            base_url="http://127.0.0.1:3000",
            operator_token=None,
            compose_service="query-man",
            diagnostic_database=None,
            diagnostic_key=None,
            diagnostic_key_id=None,
        )
    )


def _copy_source_configuration(target_root: Path) -> None:
    target_config = target_root / "config"
    target_config.mkdir()
    shutil.copytree(ROOT_DIRECTORY / "config" / "sources", target_config / "sources")
    for name in ("budget-profiles.yaml", "verified-queries.yaml"):
        shutil.copy2(ROOT_DIRECTORY / "config" / name, target_config / name)


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


def test_source_commands_render_yaml_and_cache_listed_sources() -> None:
    backend = FakeBackend()
    shell, output = _shell(backend)

    shell.onecmd("source")

    listed = yaml.safe_load(output.getvalue())
    assert listed == {
        "authority": "yaml",
        "source_count": 1,
        "sources": [
            {
                "path": "config/sources/known-source.yaml",
                "source_id": "known-source",
                "name": "Known source",
            }
        ],
    }
    line = "source show kn"
    assert shell.complete_source("kn", line, line.index("kn"), len(line)) == [
        "known-source"
    ]

    output.seek(0)
    output.truncate()
    shell.onecmd("source show known-source")

    shown = yaml.safe_load(output.getvalue())
    assert shown["path"] == "config/sources/known-source.yaml"
    assert shown["manifest"] == {"version": 2, "source_id": "known-source"}
    assert backend.source_calls == [("list",), ("show", "known-source")]


def test_retired_source_mutation_commands_only_show_the_yaml_guide() -> None:
    backend = FakeBackend()
    shell, output = _shell(backend)

    removed = (
        "usage known-source",
        "history known-source",
        "replicas known-source",
        "changes known-source",
        "receipt 123e4567-e89b-12d3-a456-426614174000",
        "apply known-source source.yaml --reason change-123",
        "secret known-source --reason rotation-123",
        "verified known-source query.yaml --reason verify-123",
        "rollback known-source 1 --reason rollback-123",
        "resume known-source sha256:deadbeef --reason incident-123",
        "disable known-source --reason incident-123",
    )
    for command in removed:
        shell.onecmd(f"source {command}")

    assert backend.source_calls == []
    assert output.getvalue().count("사용법: source <list|show|validate>") == len(removed)
    assert "Mutation" not in output.getvalue()


def test_real_source_commands_read_git_yaml_without_secrets_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "must-not-appear-in-yaml-output"
    monkeypatch.setenv("DEVELOPMENT_ISSUES_READER_PASSWORD", private_value)
    monkeypatch.setenv("MARKET_VOC_READER_PASSWORD", private_value)

    def fail_network(*_arguments: object, **_keyword_arguments: object) -> None:
        pytest.fail("YAML source commands must not call HTTP")

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.urllib.request.urlopen",
        fail_network,
    )
    backend = _real_backend(ROOT_DIRECTORY)

    list_shell, list_output = _shell(backend)
    list_shell.onecmd("source list")
    listed = yaml.safe_load(list_output.getvalue())
    assert listed["authority"] == "yaml"
    assert listed["source_count"] == 2
    assert [source["source_id"] for source in listed["sources"]] == [
        "development-issues",
        "market-voc",
    ]
    assert listed["sources"][0]["path"] == (
        "config/sources/development-issues.yaml"
    )

    show_shell, show_output = _shell(backend)
    show_shell.onecmd("source show development-issues")
    shown = yaml.safe_load(show_output.getvalue())
    assert shown["authority"] == "yaml"
    assert shown["path"] == "config/sources/development-issues.yaml"
    assert shown["manifest"]["connection"]["password_env"] == (
        "DEVELOPMENT_ISSUES_READER_PASSWORD"
    )
    assert "password" not in shown["manifest"]["connection"]

    validate_shell, validate_output = _shell(backend)
    validate_shell.onecmd("source validate")
    validated = yaml.safe_load(validate_output.getvalue())
    assert validated == {
        "status": "valid",
        "authority": "yaml",
        "source_directory": "config/sources",
        "budget_file": "config/budget-profiles.yaml",
        "verified_query_file": "config/verified-queries.yaml",
        "source_count": 2,
        "source_ids": ["development-issues", "market-voc"],
        "verified_query_document": "valid",
        "live_database_checked": False,
    }

    rendered = list_output.getvalue() + show_output.getvalue() + validate_output.getvalue()
    assert private_value not in rendered
    assert "query-man-yaml-validation-placeholder" not in rendered
    assert "generation" not in rendered


def test_source_validate_rejects_unknown_verified_source_without_raw_input(
    tmp_path: Path,
) -> None:
    _copy_source_configuration(tmp_path)
    verified_path = tmp_path / "config" / "verified-queries.yaml"
    verified = yaml.safe_load(verified_path.read_text(encoding="utf-8"))
    verified["queries"][0]["source_id"] = "unknown-source"
    verified_path.write_text(
        yaml.safe_dump(verified, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    shell, output = _shell(_real_backend(tmp_path))

    shell.onecmd("source validate")

    assert shell.last_error is True
    assert "YAML verified query 설정 검증에 실패했습니다." in output.getvalue()
    assert "unknown-source" not in output.getvalue()
    assert "Traceback" not in output.getvalue()


def test_source_validation_error_does_not_echo_accidental_secret(
    tmp_path: Path,
) -> None:
    _copy_source_configuration(tmp_path)
    manifest_path = tmp_path / "config" / "sources" / "development-issues.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    accidental_secret = "accidental-plaintext-credential"
    manifest["credential"] = accidental_secret
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    shell, output = _shell(_real_backend(tmp_path))

    shell.onecmd("source validate")

    assert shell.last_error is True
    assert "YAML source 설정 검증에 실패했습니다." in output.getvalue()
    assert accidental_secret not in output.getvalue()
    assert "Traceback" not in output.getvalue()


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


def test_real_status_sends_bearer_only_to_operator_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "operator-token-must-stay-private"
    backend = RealOperatorBackend(
        OperatorSettings(
            root=tmp_path,
            base_url="https://query-man.example",
            operator_token=token,
            compose_service="query-man",
            diagnostic_database=None,
            diagnostic_key=None,
            diagnostic_key_id=None,
        )
    )
    requests: list[tuple[str, str | None]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ready"}'

    def urlopen(request: Any, *, timeout: int) -> Response:
        assert timeout == 10
        requests.append((request.full_url, request.get_header("Authorization")))
        if request.full_url.endswith("/ready"):
            return Response()
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"code":"FORBIDDEN","message":"private"}}'),
        )

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.urllib.request.urlopen",
        urlopen,
    )

    result = backend.status()

    assert requests == [
        ("https://query-man.example/ready", None),
        ("https://query-man.example/admin/health", f"Bearer {token}"),
    ]
    assert result["operator_detail"] == "unauthorized"
    assert token not in json.dumps(result, ensure_ascii=False)
    assert "private" not in json.dumps(result, ensure_ascii=False)


def test_real_logs_terminates_docker_process_when_reader_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _real_backend(tmp_path)
    commands: list[list[str]] = []

    class Process:
        def __init__(self) -> None:
            self.stdout = iter(["first line\n", "second line\n"])
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int | None = None) -> int:
            assert timeout in {None, 2}
            return 0

    process = Process()
    popen_arguments: dict[str, object] = {}

    def popen(command: list[str], **_arguments: object) -> Process:
        commands.append(command)
        popen_arguments.update(_arguments)
        return process

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.subprocess.Popen",
        popen,
    )

    lines = backend.logs(LogQuery(since="2h", limit=10, follow=True))
    assert next(lines) == "first line"
    lines.close()

    assert commands == [[
        "docker",
        "compose",
        "logs",
        "--no-color",
        "--since",
        "2h",
        "--tail",
        "10",
        "--follow",
        "query-man",
    ]]
    assert popen_arguments["stderr"] == subprocess.DEVNULL
    assert process.terminated is True


def test_real_logs_discards_stderr_and_keeps_generic_nonzero_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _real_backend(tmp_path)

    class Process:
        stdout = iter(())

        def poll(self) -> int:
            return 17

        def wait(self, timeout: int | None = None) -> int:
            assert timeout is None
            return 17

    def popen(_command: list[str], **arguments: object) -> Process:
        assert arguments["stderr"] == subprocess.DEVNULL
        return Process()

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.subprocess.Popen",
        popen,
    )

    with pytest.raises(
        OperatorShellError,
        match="Query Man container 로그를 읽지 못했습니다",
    ):
        list(backend.logs(LogQuery()))


def test_container_diagnostic_command_does_not_expose_capture_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "capture-key-must-stay-private"
    backend = RealOperatorBackend(
        OperatorSettings(
            root=tmp_path,
            base_url="http://127.0.0.1:3000",
            operator_token=None,
            compose_service="query-man",
            diagnostic_database=tmp_path / "missing.sqlite3",
            diagnostic_key=key,
            diagnostic_key_id="key-2026-08",
        )
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **arguments: object) -> SimpleNamespace:
        calls.append((command, arguments))
        return SimpleNamespace(stdout='{"deleted":2}', returncode=0)

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.subprocess.run",
        run,
    )

    assert backend.diagnostic_purge("receipt-123") == 2
    command, arguments = calls[0]
    assert command == [
        "docker",
        "compose",
        "exec",
        "-T",
        "query-man",
        "qm",
        "--internal-diag",
        "purge",
        "receipt-123",
    ]
    assert key not in " ".join(command)
    assert arguments["timeout"] == 15


def test_one_shot_help_does_not_start_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Backend:
        pass

    monkeypatch.setattr(
        "query_man.runtime.operator_backend.RealOperatorBackend",
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
        "query_man.runtime.operator_backend.RealOperatorBackend",
        lambda _settings: FakeBackend(),
    )

    assert run_main(["diag"]) == 1
    assert "예: diag list" in capsys.readouterr().out
