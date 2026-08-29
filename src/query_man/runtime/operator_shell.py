from __future__ import annotations

import argparse
import cmd
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml
from dotenv import load_dotenv

from query_man.assurance.verified import (
    VerifiedQueryConfigurationError,
    VerifiedQueryRegistry,
)
from query_man.runtime.diagnostic_capture import (
    purge_diagnostic_consent,
    query_diagnostic_records,
)
from query_man.source_catalog.registry import (
    RegistryConfigurationError,
    SourceRegistry,
)

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
_REASON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[smhdw])$")
_MAX_DOCUMENT_BYTES = 1_048_576
_INTERNAL_DIAG = "--internal-diag"
_VALIDATION_SECRET = "query-man-yaml-validation-placeholder"

_QUICK_GUIDE = """바로 사용할 수 있는 명령:
  status                 현재 상태 확인
  logs                   최근 로그 50줄
  diag                   상세 진단 조회 안내
  source                 Git/YAML source 목록
  help                    전체 사용법
  exit                    종료
"""


class OperatorShellError(RuntimeError):
    pass


class OperatorRequestError(OperatorShellError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class LogQuery:
    since: str = "30m"
    limit: int = 50
    follow: bool = False
    level: str | None = None
    event: str | None = None
    query_id: str | None = None
    subject_id: str | None = None


@dataclass(frozen=True)
class OperatorSettings:
    root: Path
    base_url: str
    operator_token: str | None
    compose_service: str
    diagnostic_database: Path | None
    diagnostic_key: str | None
    diagnostic_key_id: str | None
    inside_container: bool = False

    @classmethod
    def load(
        cls,
        root: Path,
        *,
        base_url: str | None = None,
        token_environment: str | None = None,
        inside_container: bool = False,
    ) -> OperatorSettings:
        root = root.resolve()
        load_dotenv(root / ".env", override=False)
        port = os.environ.get("QUERY_MAN_PORT", "3000")
        selected_url = (
            base_url
            or os.environ.get("QM_URL")
            or f"http://127.0.0.1:{port}"
        ).rstrip("/")
        parsed_url = urllib.parse.urlsplit(selected_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise OperatorShellError(
                "연결 주소가 올바르지 않습니다. 예: --url http://127.0.0.1:3000"
            )
        token = _operator_token(root, token_environment)
        database = os.environ.get("QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE")
        return cls(
            root=root,
            base_url=selected_url,
            operator_token=token,
            compose_service=os.environ.get("QM_SERVICE", "query-man"),
            diagnostic_database=Path(database) if database else None,
            diagnostic_key=os.environ.get("QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY"),
            diagnostic_key_id=os.environ.get("QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID"),
            inside_container=inside_container,
        )


class OperatorBackend(Protocol):
    def status(self, *, metrics: bool = False) -> dict[str, object]: ...

    def logs(self, query: LogQuery) -> Iterator[str]: ...

    def source_list(self) -> dict[str, object]: ...

    def source_show(self, source_id: str) -> dict[str, object]: ...

    def source_validate(self) -> dict[str, object]: ...

    def diagnostic_list(
        self,
        *,
        since: datetime,
        limit: int,
    ) -> tuple[dict[str, Any], ...]: ...

    def diagnostic_show(self, capture_id: str) -> dict[str, Any] | None: ...

    def diagnostic_purge(self, receipt_id: str) -> int: ...


class RealOperatorBackend:
    def __init__(self, settings: OperatorSettings) -> None:
        self._settings = settings

    def status(self, *, metrics: bool = False) -> dict[str, object]:
        public = self._http("GET", "/ready", require_operator=False)
        if self._settings.operator_token is None:
            return {
                "ready": public,
                "operator_detail": "unavailable",
                "guide": (
                    "operator:true caller의 token 환경 변수를 설정하면 source별 상태와 "
                    "metric을 볼 수 있습니다."
                ),
            }
        path = "/admin/metrics" if metrics else "/admin/health"
        try:
            detail = self._http("GET", path)
        except OperatorRequestError as error:
            if error.status not in {401, 403}:
                raise
            return {
                "ready": public,
                "operator_detail": "unauthorized",
                "guide": str(error),
            }
        return {"ready": public, "operator": detail}

    def logs(self, query: LogQuery) -> Iterator[str]:
        command = [
            "docker",
            "compose",
            "logs",
            "--no-color",
            "--since",
            query.since,
            "--tail",
            str(query.limit),
        ]
        if query.follow:
            command.append("--follow")
        command.append(self._settings.compose_service)
        try:
            process = subprocess.Popen(
                command,
                cwd=self._settings.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            raise OperatorShellError(
                "Docker Compose를 실행할 수 없습니다. Docker가 설치되고 실행 중인지 확인하세요."
            ) from error
        assert process.stdout is not None
        try:
            for line in process.stdout:
                yield line.rstrip("\r\n")
            return_code = process.wait()
            if return_code:
                assert process.stderr is not None
                process.stderr.read()
                raise OperatorShellError(
                    "Query Man container 로그를 읽지 못했습니다. 먼저 `docker compose ps`를 확인하세요."
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def source_list(self) -> dict[str, object]:
        registry, manifests = _load_yaml_sources(self._settings.root)
        sources: list[dict[str, object]] = []
        for source_id in sorted(registry.source_ids()):
            source = registry.get(source_id)
            if source is None:
                raise OperatorShellError("YAML source 목록을 만들 수 없습니다.")
            path, _document = manifests[source_id]
            sources.append(
                {
                    "path": _root_relative(path, self._settings.root),
                    "source_id": source.source_id,
                    "name": source.name,
                    "description": source.description,
                    "owner": source.provenance.owner,
                    "environment": source.provenance.environment,
                    "budget_profile": source.budget.name,
                    "minimum_quality_level": source.minimum_quality_level,
                }
            )
        return {
            "authority": "yaml",
            "source_count": len(sources),
            "sources": sources,
        }

    def source_show(self, source_id: str) -> dict[str, object]:
        _registry, manifests = _load_yaml_sources(self._settings.root)
        current = manifests.get(source_id)
        if current is None:
            raise OperatorShellError(f"YAML source를 찾을 수 없습니다: {source_id}")
        path, document = current
        return {
            "authority": "yaml",
            "path": _root_relative(path, self._settings.root),
            "manifest": document,
        }

    def source_validate(self) -> dict[str, object]:
        registry, _manifests = _load_yaml_sources(self._settings.root)
        verified_path = self._settings.root / "config" / "verified-queries.yaml"
        try:
            VerifiedQueryRegistry.load(verified_path, set(registry.source_ids()))
        except VerifiedQueryConfigurationError as error:
            raise OperatorShellError(
                "YAML verified query 설정 검증에 실패했습니다. "
                "config/verified-queries.yaml을 확인하세요."
            ) from error
        source_ids = sorted(registry.source_ids())
        return {
            "status": "valid",
            "authority": "yaml",
            "source_directory": "config/sources",
            "budget_file": "config/budget-profiles.yaml",
            "verified_query_file": "config/verified-queries.yaml",
            "source_count": len(source_ids),
            "source_ids": source_ids,
            "verified_query_document": "valid",
            "live_database_checked": False,
        }

    def diagnostic_list(
        self,
        *,
        since: datetime,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        return self._diagnostic_records("list", since.isoformat(), str(limit))

    def diagnostic_show(self, capture_id: str) -> dict[str, Any] | None:
        records = self._diagnostic_records("show", capture_id)
        return records[0] if records else None

    def diagnostic_purge(self, receipt_id: str) -> int:
        result = self._diagnostic_call("purge", receipt_id)
        deleted = result.get("deleted")
        if isinstance(deleted, bool) or not isinstance(deleted, int):
            raise OperatorShellError("진단 삭제 결과 형식이 올바르지 않습니다.")
        return deleted

    def _http(
        self,
        method: str,
        path: str,
        *,
        require_operator: bool = True,
        headers: Mapping[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if require_operator and self._settings.operator_token is None:
            raise OperatorShellError(
                "운영자 token을 찾지 못했습니다. access policy에 operator:true caller를 추가하고 "
                "그 token 환경 변수를 .env에 설정하세요."
            )
        request_headers = {"accept": "application/json", **(headers or {})}
        if require_operator and self._settings.operator_token is not None:
            request_headers["authorization"] = (
                f"Bearer {self._settings.operator_token}"
            )
        data: bytes | None = None
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["content-type"] = "application/json"
        request = urllib.request.Request(
            f"{self._settings.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read(_MAX_DOCUMENT_BYTES + 1)
        except urllib.error.HTTPError as error:
            payload = error.read(_MAX_DOCUMENT_BYTES + 1)
            code, message = _public_error(payload)
            if error.code in {401, 403}:
                message = (
                    "운영자 인증 또는 권한이 없습니다. operator:true caller와 token을 확인하세요."
                )
            elif error.code == 404 and code == "UNKNOWN_ERROR":
                message = (
                    "요청한 운영 기능이 이 서버에 없습니다. 배포 버전과 설정을 확인하세요."
                )
            raise OperatorRequestError(error.code, code, message) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OperatorShellError(
                f"Query Man에 연결하지 못했습니다: {self._settings.base_url}"
            ) from error
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise OperatorShellError("서버 응답이 허용 크기를 초과했습니다.")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OperatorShellError("서버가 올바른 JSON 응답을 반환하지 않았습니다.") from error
        if not isinstance(document, dict):
            raise OperatorShellError("서버 응답 형식이 올바르지 않습니다.")
        return document

    def _diagnostic_records(self, action: str, *arguments: str) -> tuple[dict[str, Any], ...]:
        result = self._diagnostic_call(action, *arguments)
        records = result.get("records")
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            raise OperatorShellError("진단 조회 결과 형식이 올바르지 않습니다.")
        return tuple(records)

    def _diagnostic_call(self, action: str, *arguments: str) -> dict[str, Any]:
        database, key, key_id = self._diagnostic_configuration()
        if database.exists():
            return _run_local_diagnostic(database, key, key_id, action, arguments)
        if self._settings.inside_container:
            raise OperatorShellError(
                f"진단 저장소를 찾을 수 없습니다: {database}"
            )
        command = [
            "docker",
            "compose",
            "exec",
            "-T",
            self._settings.compose_service,
            "qm",
            _INTERNAL_DIAG,
            action,
            *arguments,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self._settings.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OperatorShellError(
                "진단 저장소에 접근하지 못했습니다. query-man container 상태를 확인하세요."
            ) from error
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise OperatorShellError(
                "container의 진단 명령이 올바른 응답을 반환하지 않았습니다. "
                "최신 image로 다시 빌드했는지 확인하세요."
            ) from error
        if not isinstance(result, dict):
            raise OperatorShellError("진단 명령 결과 형식이 올바르지 않습니다.")
        if completed.returncode:
            message = result.get("error")
            raise OperatorShellError(
                str(message) if isinstance(message, str) else "진단 명령이 실패했습니다."
            )
        return result

    def _diagnostic_configuration(self) -> tuple[Path, str, str]:
        database = self._settings.diagnostic_database
        key = self._settings.diagnostic_key
        key_id = self._settings.diagnostic_key_id
        if database is None or key is None or key_id is None:
            raise OperatorShellError(
                "진단 저장소가 설정되지 않았습니다. 세 QUERY_MAN_DIAGNOSTIC_CAPTURE_* "
                "설정을 먼저 확인하세요."
            )
        return database, key, key_id


class QueryManShell(cmd.Cmd):
    prompt = "qm> "

    def __init__(
        self,
        backend: OperatorBackend,
        *,
        stdin: Any = None,
        stdout: Any = None,
    ) -> None:
        super().__init__(stdin=stdin, stdout=stdout, completekey="tab")
        self.use_rawinput = stdin is None
        self._backend = backend
        self._source_ids: set[str] = set()
        self.last_error = False

    def onecmd(self, line: str) -> bool:
        self.last_error = False
        try:
            return super().onecmd(line)
        except KeyboardInterrupt:
            self._write("\n작업을 중단했습니다. 원격 요청이었다면 서버 상태를 다시 확인하세요.")
            self.last_error = True
            return False
        except OperatorShellError as error:
            self._write(f"[오류] {error}")
            self.last_error = True
            return False

    def emptyline(self) -> bool:
        self._write(_QUICK_GUIDE.rstrip())
        return False

    def default(self, line: str) -> None:
        command = line.split(maxsplit=1)[0]
        known = ["status", "logs", "diag", "source", "help", "clear", "exit"]
        suggestion = difflib.get_close_matches(command, known, n=1)
        suffix = f" 혹시 `{suggestion[0]}`인가요?" if suggestion else ""
        raise OperatorShellError(f"알 수 없는 명령 `{command}`입니다.{suffix} `help`를 입력하세요.")

    def do_help(self, _argument: str) -> None:
        """전체 명령과 예시를 보여줍니다."""

        self._write(
            """Query Man 운영 쉘

상태와 모니터링:
  status                         readiness와 source 상태
  status metrics                 전체 bounded metric snapshot
  logs                           최근 30분 로그 50줄
  logs --since 2h -n 100         최근 2시간 로그
  logs --event query_failed      실패 event만 조회
  logs --qid <query-id>          query 하나의 흐름 조회
  logs -f                        새 로그를 계속 보기(Ctrl-C로 중단)

상세 진단(동의 기반 암호화 저장소):
  diag list                      최근 1시간 요약 20건
  diag show <capture-id> --reason <ticket>
  diag purge <receipt-id> --reason <ticket>

Git/YAML source:
  source                         YAML source 목록
  source show <source-id>        Git에 저장된 manifest 조회
  source validate                manifest, budget, verified 설정 검증

기타:
  clear                          화면 정리
  exit                           종료

Tab을 누르면 명령과 하위 명령을 자동완성합니다. Source 변경은 YAML을 수정해
Git review로 반영합니다. 입력이 부족하면 실행하지 않고 예시를 보여줍니다.
""".rstrip()
        )

    def do_status(self, argument: str) -> None:
        """서버 상태를 확인합니다. `status metrics`로 metric을 봅니다."""

        tokens = self._tokens(argument)
        if tokens not in ([], ["metrics"]):
            self._guide("사용법: status [metrics]", "예: status metrics")
            return
        self._document(
            "Query Man 상태",
            self._backend.status(metrics=tokens == ["metrics"]),
        )

    def complete_status(
        self,
        text: str,
        _line: str,
        _begidx: int,
        _endidx: int,
    ) -> list[str]:
        return _matches(text, ["metrics"])

    def do_logs(self, argument: str) -> None:
        """일반 운영 로그를 안전한 식별자와 event로 조회합니다."""

        tokens = self._tokens(argument)
        positional, options, flags = _options(
            tokens,
            {
                "--since": "since",
                "-n": "limit",
                "--limit": "limit",
                "--level": "level",
                "--event": "event",
                "--qid": "query_id",
                "--subject": "subject_id",
            },
            {"-f": "follow", "--follow": "follow"},
        )
        if positional and positional != ["tail"]:
            self._guide(
                "사용법: logs [tail] [--since 30m] [-n 50] [-f]",
                "필터: --level warning --event query_failed --qid <id> --subject <id>",
            )
            return
        since = options.get("since", "30m")
        _parse_duration(since, maximum=timedelta(days=31))
        limit = _bounded_int(options.get("limit", "50"), 1, 1_000, "로그 개수")
        level = options.get("level")
        if level is not None and level not in {
            "debug",
            "info",
            "warning",
            "error",
            "critical",
        }:
            raise OperatorShellError(
                "로그 수준은 debug, info, warning, error, critical 중 하나여야 합니다."
            )
        query = LogQuery(
            since=since,
            limit=limit,
            follow="follow" in flags,
            level=level,
            event=options.get("event"),
            query_id=options.get("query_id"),
            subject_id=options.get("subject_id"),
        )
        shown = 0
        for line in self._backend.logs(query):
            record = _log_record(line)
            if not _matches_log(record, query):
                continue
            self._write(_render_log(record, line))
            shown += 1
        if shown == 0 and not query.follow:
            self._write("조건에 맞는 로그가 없습니다. 기간을 늘리려면 `logs --since 2h`를 사용하세요.")

    def complete_logs(
        self,
        text: str,
        _line: str,
        _begidx: int,
        _endidx: int,
    ) -> list[str]:
        return _matches(
            text,
            [
                "tail",
                "--since",
                "--limit",
                "--level",
                "--event",
                "--qid",
                "--subject",
                "--follow",
            ],
        )

    def do_diag(self, argument: str) -> None:
        """동의 기반 상세 진단 capture를 조회하거나 receipt 단위로 삭제합니다."""

        tokens = self._tokens(argument)
        if not tokens:
            self._guide(
                "진단 내용은 일반 로그보다 민감하므로 동작을 골라야 합니다.",
                "예: diag list",
                "예: diag show <capture-id> --reason incident-123",
            )
            return
        action, rest = tokens[0], tokens[1:]
        positional, options, flags = _options(
            rest,
            {
                "--since": "since",
                "-n": "limit",
                "--limit": "limit",
                "--reason": "reason",
            },
            {"--yes": "yes"},
        )
        if action == "list":
            if positional or "reason" in options or flags:
                self._guide("사용법: diag list [--since 1h] [-n 20]")
                return
            since = _since(
                options.get("since", "1h"),
                maximum=timedelta(days=7),
            )
            limit = _bounded_int(options.get("limit", "20"), 1, 100, "진단 개수")
            records = self._backend.diagnostic_list(since=since, limit=limit)
            summaries = [_diagnostic_summary(record) for record in records]
            self._document("상세 진단 요약", {"count": len(summaries), "records": summaries})
            return
        if action == "show":
            if len(positional) != 1 or "reason" not in options:
                self._guide(
                    "사용법: diag show <capture-id> --reason <ticket-or-change-ref>",
                    "예: diag show 123e4567-e89b-12d3-a456-426614174000 --reason incident-123",
                )
                return
            capture_id = _uuid(positional[0], "capture ID")
            reason = _reason(options["reason"])
            if "yes" not in flags and not self._confirm(
                f"SHOW {capture_id}",
                "질문 원문이 터미널에 표시될 수 있습니다.",
            ):
                return
            record = self._backend.diagnostic_show(capture_id)
            if record is None:
                raise OperatorShellError("해당 capture가 없거나 이미 만료됐습니다.")
            self._write(f"조회 사유: {reason}")
            self._document("상세 진단", record)
            return
        if action == "purge":
            if len(positional) != 1 or "reason" not in options:
                self._guide(
                    "사용법: diag purge <receipt-id> --reason <ticket-or-change-ref>",
                    "이 명령은 해당 동의 receipt의 capture를 삭제합니다.",
                )
                return
            receipt_id = positional[0]
            reason = _reason(options["reason"])
            if "yes" not in flags and not self._confirm(
                f"PURGE {receipt_id}",
                "삭제한 진단 capture는 복구할 수 없습니다.",
            ):
                return
            deleted = self._backend.diagnostic_purge(receipt_id)
            self._write(f"삭제 완료: {deleted}건 (사유: {reason})")
            return
        self._guide("사용법: diag <list|show|purge> ...", "예: diag list")

    def complete_diag(
        self,
        text: str,
        line: str,
        begidx: int,
        _endidx: int,
    ) -> list[str]:
        words_before_cursor = line[:begidx].split()
        if len(words_before_cursor) <= 1:
            return _matches(text, ["list", "show", "purge"])
        return _matches(text, ["--since", "--limit", "--reason", "--yes"])

    def do_source(self, argument: str) -> None:
        """Git으로 관리하는 YAML source 설정을 조회하고 검증합니다."""

        tokens = self._tokens(argument)
        if not tokens:
            tokens = ["list"]
        action, rest = tokens[0], tokens[1:]
        if action == "list":
            self._source_list(rest)
            return
        if action == "show":
            self._source_show(rest)
            return
        if action == "validate":
            self._source_validate(rest)
            return
        self._guide(
            "사용법: source <list|show|validate>",
            "예: source show example-source",
        )

    def complete_source(
        self,
        text: str,
        line: str,
        begidx: int,
        _endidx: int,
    ) -> list[str]:
        words_before_cursor = line[:begidx].split()
        actions = ["list", "show", "validate"]
        if len(words_before_cursor) <= 1:
            return _matches(text, actions)
        if len(words_before_cursor) == 2 and words_before_cursor[1] == "show":
            return _matches(text, sorted(self._source_ids))
        return []

    def do_clear(self, _argument: str) -> None:
        """터미널 화면을 정리합니다."""

        self._write("\033[2J\033[H", end="")

    def do_exit(self, _argument: str) -> bool:
        """운영 쉘을 종료합니다."""

        self._write("운영 쉘을 종료합니다.")
        return True

    def do_quit(self, argument: str) -> bool:
        return self.do_exit(argument)

    def do_EOF(self, argument: str) -> bool:
        self._write("")
        return self.do_exit(argument)

    def _source_list(self, tokens: list[str]) -> None:
        if tokens:
            self._guide("사용법: source [list]")
            return
        document = self._backend.source_list()
        sources = document.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and isinstance(source.get("source_id"), str):
                    self._source_ids.add(source["source_id"])
        self._yaml_document(document)

    def _source_show(self, tokens: list[str]) -> None:
        if len(tokens) != 1:
            self._guide("사용법: source show <source-id>")
            return
        source_id = _source_id(tokens[0])
        document = self._backend.source_show(source_id)
        self._source_ids.add(source_id)
        self._yaml_document(document)

    def _source_validate(self, tokens: list[str]) -> None:
        if tokens:
            self._guide("사용법: source validate")
            return
        self._yaml_document(self._backend.source_validate())

    def _confirm(self, phrase: str, explanation: str) -> bool:
        self._write(explanation)
        self._write(f"계속하려면 정확히 `{phrase}`를 입력하세요.")
        self.stdout.write("확인> ")
        self.stdout.flush()
        answer = self.stdin.readline() if self.stdin is not None else input()
        if answer.rstrip("\r\n") != phrase:
            self._write("취소했습니다. 아무 변경도 요청하지 않았습니다.")
            return False
        return True

    def _tokens(self, argument: str) -> list[str]:
        try:
            return shlex.split(argument)
        except ValueError as error:
            raise OperatorShellError(f"입력을 해석할 수 없습니다: {error}") from error

    def _guide(self, *lines: str) -> None:
        self.last_error = True
        self._write("\n".join(lines))

    def _document(self, title: str, document: object) -> None:
        self._write(f"\n[{title}]")
        self._write(json.dumps(document, ensure_ascii=False, indent=2, default=str))

    def _yaml_document(self, document: object) -> None:
        self._write(
            yaml.safe_dump(
                document,
                allow_unicode=True,
                sort_keys=False,
            ).rstrip()
        )

    def _write(self, value: str, *, end: str = "\n") -> None:
        self.stdout.write(f"{value}{end}")
        self.stdout.flush()


def _operator_token(root: Path, token_environment: str | None) -> str | None:
    explicit = os.environ.get("QM_TOKEN")
    if explicit:
        return explicit
    if token_environment:
        return os.environ.get(token_environment)
    candidates: list[Path] = []
    configured = os.environ.get("QUERY_MAN_ACCESS_POLICY_FILE")
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            candidates.append(configured_path)
    candidates.append(root / "config" / "access-policies.compose.yaml")
    for path in candidates:
        if not path.exists():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("callers"), list):
            continue
        for caller in document["callers"]:
            if not isinstance(caller, dict) or caller.get("operator") is not True:
                continue
            token_env = caller.get("token_env")
            if isinstance(token_env, str) and os.environ.get(token_env):
                return os.environ[token_env]
    return None


def _public_error(payload: bytes) -> tuple[str, str]:
    if len(payload) > _MAX_DOCUMENT_BYTES:
        return "UNKNOWN_ERROR", "서버 오류 응답이 너무 큽니다."
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "UNKNOWN_ERROR", "서버 요청이 실패했습니다."
    if not isinstance(document, dict):
        return "UNKNOWN_ERROR", "서버 요청이 실패했습니다."
    error_document = document.get("error")
    if isinstance(error_document, dict):
        code = error_document.get("code")
        message = error_document.get("message")
    else:
        code = document.get("code")
        message = document.get("message")
    return (
        code if isinstance(code, str) else "UNKNOWN_ERROR",
        message if isinstance(message, str) else "서버 요청이 실패했습니다.",
    )


def _options(
    tokens: list[str],
    value_options: Mapping[str, str],
    flag_options: Mapping[str, str],
) -> tuple[list[str], dict[str, str], set[str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    flags: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in flag_options:
            name = flag_options[token]
            if name in flags:
                raise OperatorShellError(f"option을 중복해서 사용할 수 없습니다: {token}")
            flags.add(name)
            index += 1
            continue
        if token in value_options:
            name = value_options[token]
            if name in options:
                raise OperatorShellError(f"option을 중복해서 사용할 수 없습니다: {token}")
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
                raise OperatorShellError(f"{token} 뒤에 값이 필요합니다.")
            options[name] = tokens[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            raise OperatorShellError(f"지원하지 않는 option입니다: {token}")
        positional.append(token)
        index += 1
    return positional, options, flags


def _parse_duration(value: str, *, maximum: timedelta) -> timedelta:
    matched = _DURATION.fullmatch(value)
    if matched is None:
        raise OperatorShellError("기간은 `30m`, `2h`, `7d`처럼 입력하세요.")
    amount = int(matched.group("amount"))
    factors = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    duration = timedelta(seconds=amount * factors[matched.group("unit")])
    if duration > maximum:
        raise OperatorShellError(f"조회 기간은 최대 {maximum.days}일입니다.")
    return duration


def _since(value: str, *, maximum: timedelta) -> datetime:
    return datetime.now(UTC) - _parse_duration(value, maximum=maximum)


def _bounded_int(value: str, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise OperatorShellError(f"{label}은 숫자여야 합니다.") from error
    if not minimum <= parsed <= maximum:
        raise OperatorShellError(f"{label}은 {minimum}~{maximum} 범위여야 합니다.")
    return parsed


def _source_id(value: str) -> str:
    if _SOURCE_ID.fullmatch(value) is None:
        raise OperatorShellError(
            "source ID는 소문자로 시작하고 소문자·숫자·하이픈만 사용합니다."
        )
    return value


def _reason(value: str) -> str:
    if _REASON.fullmatch(value) is None:
        raise OperatorShellError(
            "사유는 영문·숫자로 시작하는 128자 이하 change/ticket 식별자여야 합니다."
        )
    return value


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise OperatorShellError(f"{label}는 UUID 형식이어야 합니다.") from error


def _load_document(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
        if not path.is_file() or size > _MAX_DOCUMENT_BYTES:
            raise OperatorShellError("입력 파일은 1 MiB 이하 regular file이어야 합니다.")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OperatorShellError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise OperatorShellError(f"입력 파일을 읽을 수 없습니다: {path}") from error
    if not isinstance(document, dict):
        raise OperatorShellError("입력 파일 최상위 값은 object여야 합니다.")
    return document


def _load_yaml_sources(
    root: Path,
) -> tuple[SourceRegistry, dict[str, tuple[Path, dict[str, object]]]]:
    root = root.resolve()
    source_directory = root / "config" / "sources"
    files = _source_yaml_files(source_directory)
    documents = [(path, _load_document(path)) for path in files]
    validation_environment: dict[str, str] = {}
    for _path, document in documents:
        source_id = document.get("source_id")
        if isinstance(source_id, str) and _SOURCE_ID.fullmatch(source_id):
            secret_name = f"{source_id.replace('-', '_').upper()}_READER_PASSWORD"
            validation_environment[secret_name] = _VALIDATION_SECRET

    try:
        registry = SourceRegistry.load(
            source_directory,
            root / "config" / "budget-profiles.yaml",
            validation_environment,
        )
    except RegistryConfigurationError as error:
        raise OperatorShellError(
            "YAML source 설정 검증에 실패했습니다. "
            "config/sources와 config/budget-profiles.yaml을 확인하세요."
        ) from error

    current_files = _source_yaml_files(source_directory)
    current_documents = [(path, _load_document(path)) for path in current_files]
    if current_documents != documents:
        raise OperatorShellError(
            "YAML source 설정이 검증 중 변경되었습니다. 다시 시도하세요."
        )

    manifests: dict[str, tuple[Path, dict[str, object]]] = {}
    for path, document in documents:
        source_id = document.get("source_id")
        if not isinstance(source_id, str):
            raise OperatorShellError("검증된 YAML source ID를 읽을 수 없습니다.")
        manifests[source_id] = (path, document)
    if frozenset(manifests) != registry.source_ids():
        raise OperatorShellError("검증된 YAML source 목록이 일치하지 않습니다.")
    return registry, manifests


def _source_yaml_files(source_directory: Path) -> list[Path]:
    try:
        return sorted(
            path
            for path in source_directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
        )
    except OSError as error:
        raise OperatorShellError(
            "YAML source 설정을 읽을 수 없습니다. config/sources를 확인하세요."
        ) from error


def _root_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise OperatorShellError("YAML source 경로가 repository root 밖에 있습니다.") from error


def _log_record(line: str) -> dict[str, object] | None:
    start = line.find("{")
    if start < 0:
        return None
    try:
        document = json.loads(line[start:])
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _matches_log(record: dict[str, object] | None, query: LogQuery) -> bool:
    filters = (
        ("level", query.level),
        ("event", query.event),
        ("query_id", query.query_id),
        ("subject_id", query.subject_id),
    )
    if record is None:
        return all(expected is None for _, expected in filters)
    return all(expected is None or record.get(field) == expected for field, expected in filters)


def _render_log(record: dict[str, object] | None, original: str) -> str:
    if record is None:
        return original
    timestamp = str(record.get("timestamp", "-"))
    level = str(record.get("level", "info")).upper()
    event = str(record.get("event", "-"))
    details = []
    for field in (
        "source_id",
        "query_id",
        "subject_id",
        "status_code",
        "outcome",
        "error_code",
        "reason_code",
        "queue_ms",
        "elapsed_ms",
        "row_count",
        "result_bytes",
    ):
        if field in record:
            details.append(f"{field}={record[field]}")
    suffix = f" {' '.join(details)}" if details else ""
    return f"{timestamp} {level:<8} {event}{suffix}"


def _diagnostic_summary(record: Mapping[str, Any]) -> dict[str, object]:
    request = record.get("request")
    request = request if isinstance(request, dict) else {}
    summary: dict[str, object] = {
        field: record[field]
        for field in (
            "capture_id",
            "captured_at",
            "expires_at",
            "subject_id",
            "source_id",
        )
        if field in record
    }
    for field in ("operation", "query_id", "sql_parseable", "sql_bytes"):
        if field in request:
            summary[field] = request[field]
    if "question" in request:
        summary["question_bytes"] = len(str(request["question"]).encode("utf-8"))
    return summary


def _matches(text: str, values: list[str]) -> list[str]:
    return [value for value in values if value.startswith(text)]


def _run_local_diagnostic(
    database: Path,
    key: str,
    key_id: str,
    action: str,
    arguments: tuple[str, ...],
) -> dict[str, Any]:
    if action == "list" and len(arguments) == 2:
        since = datetime.fromisoformat(arguments[0])
        limit = int(arguments[1])
        return {
            "records": list(
                query_diagnostic_records(
                    database,
                    key,
                    key_id,
                    since=since,
                    limit=limit,
                )
            )
        }
    if action == "show" and len(arguments) == 1:
        records = query_diagnostic_records(
            database,
            key,
            key_id,
            since=datetime.now(UTC) - timedelta(days=7),
            limit=1,
            capture_id=arguments[0],
        )
        return {"records": list(records)}
    if action == "purge" and len(arguments) == 1:
        return {
            "deleted": purge_diagnostic_consent(
                database,
                key,
                key_id,
                arguments[0],
            )
        }
    raise OperatorShellError("지원하지 않는 내부 진단 명령입니다.")


def _internal_diagnostic_main(arguments: list[str]) -> int:
    try:
        settings = OperatorSettings.load(Path.cwd(), inside_container=True)
        backend = RealOperatorBackend(settings)
        result = backend._diagnostic_call(arguments[0], *arguments[1:])
    except (IndexError, OperatorShellError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qm",
        description="Interactive Query Man operations shell",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--url", help="Query Man base URL")
    parser.add_argument("--token-env", help="operator token environment variable")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def run_main(arguments: list[str]) -> int:
    if arguments and arguments[0] == _INTERNAL_DIAG:
        return _internal_diagnostic_main(arguments[1:])
    parsed = _parser().parse_args(arguments)
    try:
        settings = OperatorSettings.load(
            parsed.root,
            base_url=parsed.url,
            token_environment=parsed.token_env,
        )
    except OperatorShellError as error:
        print(f"[오류] {error}", file=sys.stderr)
        return 2
    shell = QueryManShell(RealOperatorBackend(settings))
    if parsed.command:
        shell.onecmd(shlex.join(parsed.command))
        return 1 if shell.last_error else 0
    intro = (
        "Query Man 운영 쉘입니다. Tab으로 자동완성하고 `help`로 예시를 볼 수 있습니다.\n"
        "빈 줄을 입력하면 빠른 안내를 다시 보여줍니다."
    )
    try:
        shell.cmdloop(intro=intro)
    except KeyboardInterrupt:
        print("\n운영 쉘을 종료합니다.")
    return 0


def main() -> None:
    raise SystemExit(run_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
