from __future__ import annotations

import argparse
import cmd
import difflib
import json
import re
import shlex
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

import query_man.runtime.operator_backend as operator_backend
from query_man.runtime.operator_backend import (
    LogQuery,
    OperatorBackend,
    OperatorShellError,
)

_REASON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[smhdw])$")

_QUICK_GUIDE = """바로 사용할 수 있는 명령:
  status                 현재 상태 확인
  logs                   최근 로그 50줄
  diag                   상세 진단 조회 안내
  source                 Git/YAML source 목록
  help                    전체 사용법
  exit                    종료
"""


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
            self._diag_list(positional, options, flags)
            return
        if action == "show":
            self._diag_show(positional, options, flags)
            return
        if action == "purge":
            self._diag_purge(positional, options, flags)
            return
        self._guide("사용법: diag <list|show|purge> ...", "예: diag list")

    def _diag_list(
        self,
        positional: list[str],
        options: dict[str, str],
        flags: set[str],
    ) -> None:
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

    def _diag_show(
        self,
        positional: list[str],
        options: dict[str, str],
        flags: set[str],
    ) -> None:
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

    def _diag_purge(
        self,
        positional: list[str],
        options: dict[str, str],
        flags: set[str],
    ) -> None:
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
    if operator_backend.SOURCE_ID_PATTERN.fullmatch(value) is None:
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


def _internal_diagnostic_main(arguments: list[str]) -> int:
    try:
        settings = operator_backend.OperatorSettings.load(Path.cwd(), inside_container=True)
        backend = operator_backend.RealOperatorBackend(settings)
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
    if arguments and arguments[0] == operator_backend.INTERNAL_DIAGNOSTIC_ARGUMENT:
        return _internal_diagnostic_main(arguments[1:])
    parsed = _parser().parse_args(arguments)
    try:
        settings = operator_backend.OperatorSettings.load(
            parsed.root,
            base_url=parsed.url,
            token_environment=parsed.token_env,
        )
    except OperatorShellError as error:
        print(f"[오류] {error}", file=sys.stderr)
        return 2
    shell = QueryManShell(operator_backend.RealOperatorBackend(settings))
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
