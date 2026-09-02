from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml
from dotenv import load_dotenv

from query_man.runtime.diagnostic_capture import (
    purge_diagnostic_consent,
    query_diagnostic_records,
)
from query_man.source_catalog.registry import (
    RegistryConfigurationError,
    SourceRegistry,
)

SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
INTERNAL_DIAGNOSTIC_ARGUMENT = "--internal-diag"
_MAX_DOCUMENT_BYTES = 1_048_576
_VALIDATION_SECRET = "query-man-yaml-validation-placeholder"


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
                stderr=subprocess.DEVNULL,
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
        registry, manifests = _load_source_packages(self._settings.root)
        sources: list[dict[str, object]] = []
        for source_id in sorted(registry.source_ids()):
            source = registry.get(source_id)
            if source is None:
                raise OperatorShellError("Source package 목록을 만들 수 없습니다.")
            path, _document = manifests[source_id]
            sources.append(
                {
                    "package_path": _root_relative(path.parent, self._settings.root),
                    "source_manifest": _root_relative(path, self._settings.root),
                    "views_sql": _root_relative(path.parent / "views.sql", self._settings.root),
                    "source_id": source.source_id,
                    "name": source.name,
                    "description": source.description,
                    "owner": source.provenance.owner,
                    "environment": source.provenance.environment,
                    "view_contract_version": source.view_contract_version,
                    "budget_profile": source.budget.name,
                }
            )
        return {
            "authority": "source-package",
            "source_count": len(sources),
            "sources": sources,
        }

    def source_show(self, source_id: str) -> dict[str, object]:
        _registry, manifests = _load_source_packages(self._settings.root)
        current = manifests.get(source_id)
        if current is None:
            raise OperatorShellError(f"Source package를 찾을 수 없습니다: {source_id}")
        path, document = current
        return {
            "authority": "source-package",
            "package_path": _root_relative(path.parent, self._settings.root),
            "source_manifest": _root_relative(path, self._settings.root),
            "views_sql": _root_relative(path.parent / "views.sql", self._settings.root),
            "manifest": document,
        }

    def source_validate(self) -> dict[str, object]:
        registry, _manifests = _load_source_packages(self._settings.root)
        source_ids = sorted(registry.source_ids())
        return {
            "status": "valid",
            "authority": "source-package",
            "source_directory": "config/sources",
            "package_layout": "config/sources/<source-id>/{source.yaml,views.sql}",
            "budget_file": "config/budget-profiles.yaml",
            "source_count": len(source_ids),
            "source_ids": source_ids,
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
            INTERNAL_DIAGNOSTIC_ARGUMENT,
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


def _load_document(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise OperatorShellError(
                "입력 파일은 1 MiB 이하 regular non-symlink file이어야 합니다."
            )
        size = path.stat().st_size
        if not path.is_file() or size > _MAX_DOCUMENT_BYTES:
            raise OperatorShellError(
                "입력 파일은 1 MiB 이하 regular non-symlink file이어야 합니다."
            )
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OperatorShellError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise OperatorShellError(f"입력 파일을 읽을 수 없습니다: {path}") from error
    if not isinstance(document, dict):
        raise OperatorShellError("입력 파일 최상위 값은 object여야 합니다.")
    return document


def _load_source_packages(
    root: Path,
) -> tuple[SourceRegistry, dict[str, tuple[Path, dict[str, object]]]]:
    root = root.resolve()
    source_directory = root / "config" / "sources"
    files = _source_manifest_files(source_directory)
    documents = [(path, _load_document(path)) for path in files]
    validation_environment: dict[str, str] = {}
    for _path, document in documents:
        source_id = document.get("source_id")
        if isinstance(source_id, str) and SOURCE_ID_PATTERN.fullmatch(source_id):
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
            "Source package 설정 검증에 실패했습니다. "
            "config/sources와 config/budget-profiles.yaml을 확인하세요."
        ) from error

    current_files = _source_manifest_files(source_directory)
    current_documents = [(path, _load_document(path)) for path in current_files]
    if current_documents != documents:
        raise OperatorShellError(
            "Source package가 검증 중 변경되었습니다. 다시 시도하세요."
        )

    manifests: dict[str, tuple[Path, dict[str, object]]] = {}
    for path, document in documents:
        source_id = document.get("source_id")
        if not isinstance(source_id, str):
            raise OperatorShellError("검증된 source package ID를 읽을 수 없습니다.")
        manifests[source_id] = (path, document)
    if frozenset(manifests) != registry.source_ids():
        raise OperatorShellError("검증된 source package 목록이 일치하지 않습니다.")
    return registry, manifests


def _source_manifest_files(source_directory: Path) -> list[Path]:
    try:
        return sorted(
            path
            for path in source_directory.glob("*/source.yaml")
            if path.is_file()
        )
    except OSError as error:
        raise OperatorShellError(
            "Source package를 읽을 수 없습니다. config/sources를 확인하세요."
        ) from error


def _root_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise OperatorShellError("Source package 경로가 repository root 밖에 있습니다.") from error


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
