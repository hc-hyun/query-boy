from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRECTORY = ROOT / ".agents" / "skills" / "query-man-admin"
HELPER = SKILL_DIRECTORY / "scripts" / "query_man_request.py"
SOURCE_VALIDATOR = SKILL_DIRECTORY / "scripts" / "validate_source_packages.py"


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
