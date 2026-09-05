from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import subprocess
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.request import HTTPSHandler, Request
from urllib.response import addinfourl

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRECTORY = ROOT / ".agents" / "skills" / "query-man-admin"
HELPER = SKILL_DIRECTORY / "scripts" / "query_man_request.py"
SOURCE_VALIDATOR = SKILL_DIRECTORY / "scripts" / "validate_source_packages.py"


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_helper(*arguments: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HELPER), *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _run_source_validator(root: Path, *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SOURCE_VALIDATOR), "--root", str(root)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _copy_source_configuration(target_root: Path) -> None:
    target_config = target_root / "config"
    target_config.mkdir()
    cave_config = ROOT / "query-cave" / "config"
    shutil.copytree(cave_config / "sources", target_config / "sources")
    shutil.copy2(cave_config / "budget-profiles.yaml", target_config / "budget-profiles.yaml")
    shutil.copy2(cave_config / "database-profiles.yaml", target_config / "database-profiles.yaml")


def test_skill_is_explicit_only_and_helper_is_executable() -> None:
    configuration = yaml.safe_load((SKILL_DIRECTORY / "agents" / "openai.yaml").read_text(encoding="utf-8"))

    assert configuration["policy"]["allow_implicit_invocation"] is False
    assert "$query-man-admin" in configuration["interface"]["default_prompt"]
    assert HELPER.stat().st_mode & stat.S_IXUSR
    assert SOURCE_VALIDATOR.stat().st_mode & stat.S_IXUSR


def test_installed_admin_cli_is_removed() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["project"]["scripts"] == {"query-man": "query_man.runtime.server:main"}
    assert not (ROOT / "src" / "query_man" / "runtime" / "operator_shell.py").exists()


def test_source_validator_uses_only_versioned_configuration() -> None:
    private_value = "runtime-secret-that-must-not-appear"
    environment = {
        **os.environ,
        "QUERY_MAN_POSTGRES_HOST": private_value,
        "QUERY_MAN_OPERATOR_TOKEN": private_value,
    }

    result = _run_source_validator(ROOT / "query-cave", environment=environment)

    assert result.returncode == 0
    assert private_value not in result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "valid",
        "validation_scope": "configuration_and_package_layout",
        "views_sql_validated": False,
        "source_count": 1,
        "source_ids": ["query-cave"],
        "runtime_environment_read": False,
        "database_connected": False,
        "credential_files_read": False,
    }


def test_source_validator_fails_closed_and_redacts_invalid_package(tmp_path: Path) -> None:
    _copy_source_configuration(tmp_path)
    manifest_path = tmp_path / "config" / "sources" / "query-cave" / "source.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    private_value = "accidental-plaintext-credential"
    manifest["credential"] = private_value
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    result = _run_source_validator(tmp_path, environment=os.environ.copy())

    assert result.returncode == 1
    assert result.stdout == ""
    assert private_value not in result.stderr
    assert "Traceback" not in result.stderr


def test_helper_rejects_remote_plaintext_without_printing_token() -> None:
    token = "home-only-disposable-token-1234567890"
    environment = {
        **os.environ,
        "QUERY_MAN_SERVER_URL": "http://query-man.example.test",
        "QUERY_MAN_OPERATOR_TOKEN": token,
    }

    result = _run_helper("status", environment=environment)

    assert result.returncode == 1
    assert token not in result.stdout
    assert token not in result.stderr


def test_helper_rejects_group_readable_token_file(tmp_path: Path) -> None:
    token = "file-token-that-must-never-be-printed-123"
    token_file = tmp_path / "operator-token"
    token_file.write_text(token, encoding="ascii")
    token_file.chmod(0o640)
    environment = {
        **os.environ,
        "QUERY_MAN_SERVER_URL": "http://127.0.0.1:1",
        "QUERY_MAN_OPERATOR_TOKEN_FILE": str(token_file),
    }
    environment.pop("QUERY_MAN_OPERATOR_TOKEN", None)

    result = _run_helper("status", environment=environment)

    assert result.returncode == 1
    assert token not in result.stdout
    assert token not in result.stderr


def test_helper_sends_token_in_memory_and_redacts_reflection(tmp_path: Path) -> None:
    token = "file-token-that-must-never-be-printed-123"
    token_file = tmp_path / "operator-token"
    token_file.write_text(f"{token}\n", encoding="ascii")
    token_file.chmod(0o600)
    received_authorization: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            response = json.dumps({"reflected": token}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = {
            **os.environ,
            "QUERY_MAN_SERVER_URL": f"http://127.0.0.1:{server.server_port}",
            "QUERY_MAN_OPERATOR_TOKEN_FILE": str(token_file),
        }
        environment.pop("QUERY_MAN_OPERATOR_TOKEN", None)

        result = _run_helper("status", environment=environment)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0
    assert received_authorization == [f"Bearer {token}"]
    assert token not in result.stdout
    assert json.loads(result.stdout)["response"] == {"reflected": "[REDACTED]"}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("same_origin", [False, True])
def test_helper_blocks_redirect_without_forwarding_authorization(status: int, same_origin: bool) -> None:
    token = "redirect-token-that-must-stay-at-origin-123"
    received: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(status if self.path == "/admin/health" else 200)
            self.send_header("Location", destination)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    with _serve(Handler) as target, _serve(Handler) as origin:
        destination = f"http://127.0.0.1:{origin.server_port if same_origin else target.server_port}/elsewhere"
        environment = {
            **os.environ,
            "QUERY_MAN_SERVER_URL": f"http://127.0.0.1:{origin.server_port}",
            "QUERY_MAN_OPERATOR_TOKEN": token,
        }
        environment.pop("QUERY_MAN_OPERATOR_TOKEN_FILE", None)
        result = _run_helper("status", environment=environment)

    assert received == [("/admin/health", f"Bearer {token}")]
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"http_status": status, "response": {"error": "server redirect blocked"}}
    assert token not in result.stdout + result.stderr
    assert destination not in result.stdout + result.stderr


def test_helper_blocks_https_to_http_redirect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "downgrade-token-that-must-remain-in-memory-123"
    received: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    with _serve(Handler) as target:
        redirect_headers = Message()
        redirect_headers["Location"] = f"http://127.0.0.1:{target.server_port}/elsewhere"
        response = addinfourl(BytesIO(), redirect_headers, "https://127.0.0.1/admin/health", 302)
        response.msg = "Found"

        def https_open(_handler: HTTPSHandler, request: Request) -> addinfourl:
            assert request.get_header("Authorization") == f"Bearer {token}"
            return response

        # Supply a synthetic HTTPS response; following its Location would hit the real HTTP listener.
        monkeypatch.setattr(HTTPSHandler, "https_open", https_open)
        monkeypatch.setenv("QUERY_MAN_SERVER_URL", "https://127.0.0.1")
        monkeypatch.setenv("QUERY_MAN_OPERATOR_TOKEN", token)
        monkeypatch.delenv("QUERY_MAN_OPERATOR_TOKEN_FILE", raising=False)
        monkeypatch.delenv("QUERY_MAN_SERVER_CA_FILE", raising=False)
        helper = runpy.run_path(str(HELPER))
        exit_code = helper["run"](["status"])

    assert received == []
    assert response.closed
    assert exit_code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"http_status": 302, "response": {"error": "server redirect blocked"}}
    assert token not in output.out + output.err


@pytest.mark.parametrize("status", [200, 401, 500])
@pytest.mark.parametrize("encoding", ["unicode", "json-special-characters"])
def test_helper_never_prints_encoded_token_in_nested_keys_or_values(status: int, encoding: str) -> None:
    token = (
        "unicode-escaped-secret-token-1234567890"
        if encoding == "unicode"
        else 'secret-token-containing-a-quote-"-and-backslash-\\'
    )
    payload = {token: [{"nested": f"Bearer {token}", "count": 2, "available": True}], "detail": "private server error"}
    serialized = json.dumps(payload)
    if encoding == "unicode":
        serialized = serialized.replace(token, "".join(f"\\u{ord(character):04x}" for character in token))
    body = serialized.encode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    with _serve(Handler) as server:
        environment = {
            **os.environ,
            "QUERY_MAN_SERVER_URL": f"http://127.0.0.1:{server.server_port}",
            "QUERY_MAN_OPERATOR_TOKEN": token,
        }
        environment.pop("QUERY_MAN_OPERATOR_TOKEN_FILE", None)
        result = _run_helper("status", environment=environment)

    assert result.returncode == (0 if status == 200 else 1)
    assert token not in result.stdout + result.stderr
    output = json.loads(result.stdout)
    if status == 200:
        assert output["response"] == {
            "[REDACTED]": [{"nested": "Bearer [REDACTED]", "count": 2, "available": True}],
            "detail": "private server error",
        }
    else:
        assert output == {"http_status": status, "response": {"error": "server rejected the request"}}
        assert "private server error" not in result.stdout + result.stderr


@pytest.mark.parametrize("invalid_body", ["oversized", "too-deep"])
def test_helper_fails_safely_on_unprocessable_response(invalid_body: str) -> None:
    token = "response-limit-token-that-must-not-appear-123"
    body = (
        b" " * (1024 * 1024 + 1)
        if invalid_body == "oversized"
        else b"[" * 2000 + json.dumps(token).encode("ascii") + b"]" * 2000
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    with _serve(Handler) as server:
        environment = {
            **os.environ,
            "QUERY_MAN_SERVER_URL": f"http://127.0.0.1:{server.server_port}",
            "QUERY_MAN_OPERATOR_TOKEN": token,
        }
        environment.pop("QUERY_MAN_OPERATOR_TOKEN_FILE", None)
        result = _run_helper("status", environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Query Man request failed; no credential or response details were printed.\n"
