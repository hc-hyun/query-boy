"""Synthetic subprocess tests; no installed Passport, DB, credentials, or bindings."""

from __future__ import annotations

import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / ".agents/skills/query-man-admin/scripts/query_passport.py"
DBA = ROOT / ".agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py"
CANARY = "SYNTHETIC_CREDENTIAL_MUST_NOT_APPEAR"
CAPABILITY = {
    "contract_version": "1",
    "tool_version": "0.2.0",
    "command": "capabilities",
    "status": "validated",
    "scope": None,
    "errors": [],
    "result": {
        "supported_contract_majors": [1],
        "capabilities": ["profile.inspect.v1", "plan.offline.v1", "connection.verify.v1"],
        "commands": ["capabilities", "inspect", "plan", "verify"],
        "backend_types": ["offline", "local-docker"],
        "policy_revision": "m2-local-docker-1",
        "profile_versions": [1],
        "source_reference_version": 6,
        "scopes": ["database-only"],
        "environments": ["local-synthetic", "local", "protected"],
        "live_environments": ["local-synthetic", "local"],
        "limits": {
            "input_bytes": 65536,
            "output_bytes": 16384,
            "json_depth": 8,
            "timeout_seconds": 5,
            "live_timeout_seconds": 60,
        },
    },
}
INSPECT = {
    "contract_version": "1",
    "tool_version": "0.2.0",
    "command": "inspect",
    "status": "validated",
    "scope": "database-only",
    "errors": [],
    "result": {
        "mode": "offline",
        "profile_count": 1,
        "source_count": 0,
        "profile_validation": "passed",
        "profile_validation_scope": "public_projection_only",
        "source_inventory": "not_checked",
        "query_man_validation": "not_checked",
        "deployment": "not_checked",
        "reader_permissions": "not_checked",
        "source_admission": "not_checked",
        "application_readiness": "not_checked",
        "target_identity": "not_checked",
        "db_connectivity": "not_checked",
        "certificate_validation": "not_checked",
        "authentication": "not_checked",
    },
}
CHECK_NAMES = (
    "runtime_identity",
    "credential_permissions",
    "credential_mount_read_only",
    "tls",
    "client_identity",
    "target",
    "read_only_transaction",
    "statement_timeout",
    "timeout_recovery",
    "cancellation",
    "cancellation_recovery",
    "reconnect",
)


def encode(value: Any) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def live_response() -> dict[str, Any]:
    result = copy.deepcopy(INSPECT)
    result["command"] = "verify"
    result["status"] = "succeeded"
    result["result"].update(
        {
            "mode": "live",
            "checks": dict.fromkeys(CHECK_NAMES, "passed"),
            "verification_scope": "database-only",
            "executor_target_binding": "passed",
            "verification_runtime": "passed",
            "target_identity": "passed",
            "db_connectivity": "passed",
            "certificate_validation": "passed",
            "authentication": "passed",
        }
    )
    return result


def failure(command: str = "verify", code: str = "AUTHORIZATION_REQUIRED") -> dict[str, Any]:
    return {
        "contract_version": "1",
        "tool_version": "0.2.0",
        "command": command,
        "status": "failed",
        "scope": None,
        "result": {},
        "errors": [{"code": code, "message": CANARY}],
    }


@pytest.fixture
def consumer() -> dict[str, Any]:
    return runpy.run_path(str(ADMIN))


def fake_passport(
    tmp_path: Path, response: bytes, *, code: int = 0, capability: dict[str, Any] | None = None, stderr: bytes = b""
) -> Path:
    tool = tmp_path / "passport"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"log = pathlib.Path({str(tmp_path / 'calls.jsonl')!r})\n"
        "with log.open('a') as out:\n"
        "    import json\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'capabilities':\n"
        f"    sys.stdout.buffer.write({encode(capability if capability is not None else CAPABILITY)!r})\n"
        "    raise SystemExit(0)\n"
        "if '--request' in sys.argv and sys.argv[sys.argv.index('--request') + 1] == '-':\n"
        "    sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write({response!r})\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        f"raise SystemExit({code})\n",
        encoding="utf-8",
    )
    tool.chmod(0o700)
    return tool


def run_helper(helper: Path, *args: str, data: bytes = b"{}") -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [sys.executable, str(helper), *args], input=data, capture_output=True, timeout=12, cwd=ROOT, check=False
    )
    assert result.stderr == b""
    assert CANARY.encode() not in result.stdout
    assert b"Traceback" not in result.stdout
    assert len(result.stdout) < 16384
    return result


def test_admin_offline_inspection_with_source_zero(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(INSPECT))
    result = run_helper(ADMIN, "inspect", "--passport", str(tool), "--request", "-")
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["status"] == "validated"
    assert summary["source_count"] == 0
    for field in (
        "source_inventory",
        "query_man_validation",
        "db_connectivity",
        "certificate_validation",
        "reader_permissions",
        "source_admission",
        "application_readiness",
        "deployment",
    ):
        assert summary[field] == "not_checked"
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls == [
        ["capabilities", "--format", "json"],
        ["inspect", "--request", "-", "--workspace", ".", "--format", "json"],
    ]


def test_redirected_public_stdin_file_is_supported(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(INSPECT))
    request = tmp_path / "request.json"
    request.write_bytes(b"{}")
    with request.open("rb") as stream:
        result = subprocess.run(
            [sys.executable, str(ADMIN), "inspect", "--passport", str(tool), "--request", "-"],
            stdin=stream,
            capture_output=True,
            timeout=12,
            check=False,
        )
    assert result.returncode == 0
    assert result.stderr == b""


def test_public_filename_forwarded_to_passport_boundary(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(INSPECT))
    result = run_helper(ADMIN, "inspect", "--passport", str(tool), "--request", "request.json", "--workspace", "public")
    assert result.returncode == 0
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls[1] == ["inspect", "--request", "request.json", "--workspace", "public", "--format", "json"]


@pytest.mark.parametrize(
    "args",
    [
        ("verify",),
        ("issue",),
        ("apply",),
        ("rotate",),
        ("rollback",),
        ("inspect", "--approved"),
        ("inspect", "--execute"),
        ("inspect", "--yes"),
    ],
)
def test_admin_never_launches_forbidden_or_bypass_command(tmp_path: Path, args: tuple[str, ...]) -> None:
    tool = fake_passport(tmp_path, encode(live_response()))
    result = run_helper(ADMIN, *args, "--passport", str(tool), "--request", "-")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "INVALID_INPUT"
    assert not (tmp_path / "calls.jsonl").exists()


def test_dba_verification_preserves_application_and_source_boundaries(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(live_response()))
    result = run_helper(DBA, "--passport", str(tool), "--request", "-")
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["status"] == "succeeded" and summary["source_count"] == 0
    assert summary["db_connectivity"] == summary["authentication"] == summary["target_identity"] == "passed"
    assert set(summary["checks"]) == set(CHECK_NAMES)
    for field in ("source_inventory", "reader_permissions", "source_admission", "application_readiness", "deployment"):
        assert summary[field] == "not_checked"


@pytest.mark.parametrize("helper,args,command", [(ADMIN, ("inspect",), "inspect"), (DBA, (), "verify")])
def test_classified_failure_omits_provider_message(
    tmp_path: Path, helper: Path, args: tuple[str, ...], command: str
) -> None:
    tool = fake_passport(tmp_path, encode(failure(command)), code=6)
    result = run_helper(helper, *args, "--passport", str(tool), "--request", "-")
    assert result.returncode == 6
    assert json.loads(result.stdout)["error"] == "AUTHORIZATION_REQUIRED"


@pytest.mark.parametrize("change", ["version", "major", "capability", "command"])
def test_discovery_refusal_prevents_second_invocation(tmp_path: Path, change: str) -> None:
    capability = copy.deepcopy(CAPABILITY)
    if change == "version":
        capability["tool_version"] = "0.1.0"
    elif change == "major":
        capability["contract_version"] = "2"
    elif change == "capability":
        capability["result"]["capabilities"].remove("connection.verify.v1")
    else:
        capability["result"]["commands"].remove("verify")
    tool = fake_passport(tmp_path, encode(live_response()), capability=capability)
    result = run_helper(DBA, "--passport", str(tool), "--request", "-")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "PASSPORT_INCOMPATIBLE"
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"null\n",
        b"{}\n",
        encode(INSPECT)[:-1],
        b"[]\n",
        encode(INSPECT) + b"{}\n",
        b"\xff\n",
        b" " * 16385,
        encode(INSPECT).replace(b'"source_count":0', b'"source_count":NaN'),
        encode(INSPECT).replace(b'"source_count":0', b'"source_count":0,"source_count":0'),
        b"[" * 1000 + b"0" + b"]" * 1000 + b"\n",
    ],
)
def test_malformed_bounded_response_rejected(consumer: dict[str, Any], raw: bytes) -> None:
    with pytest.raises(consumer["ConsumerError"]):
        consumer["validate_response"](raw, 0, "inspect")


@pytest.mark.parametrize(
    "change",
    [
        "command",
        "status",
        "scope",
        "unknown-envelope",
        "unknown-result",
        "source-bool",
        "offline-live",
        "offline-cert",
        "source-admitted",
        "errors",
    ],
)
def test_invalid_offline_claims_fail_closed(consumer: dict[str, Any], change: str) -> None:
    response = copy.deepcopy(INSPECT)
    if change == "command":
        response["command"] = "verify"
    elif change == "status":
        response["status"] = "succeeded"
    elif change == "scope":
        response["scope"] = "application"
    elif change == "unknown-envelope":
        response[CANARY] = CANARY
    elif change == "unknown-result":
        response["result"][CANARY] = CANARY
    elif change == "source-bool":
        response["result"]["source_count"] = False
    elif change == "offline-live":
        response["result"]["db_connectivity"] = "passed"
    elif change == "offline-cert":
        response["result"]["certificate_validation"] = "passed"
    elif change == "source-admitted":
        response["result"]["source_admission"] = "passed"
    else:
        response["errors"] = [{"code": "INTERNAL_ERROR", "message": CANARY}]
    with pytest.raises(consumer["ConsumerError"]):
        consumer["validate_response"](encode(response), 0, "inspect")


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "unknown",
        "failed",
        "pending",
        "unknown-state",
        "readiness",
        "tls",
        "target_identity",
        "db_connectivity",
        "authentication",
        "verification_runtime",
    ],
)
def test_live_success_requires_exact_complete_checks(consumer: dict[str, Any], change: str) -> None:
    response = live_response()
    checks = response["result"]["checks"]
    if change == "missing":
        del checks["cancellation_recovery"]
    elif change == "unknown":
        checks[CANARY] = "passed"
    elif change in {"failed", "pending", "unknown-state"}:
        checks["statement_timeout"] = {"failed": "failed", "pending": "not_checked", "unknown-state": CANARY}[change]
    elif change == "readiness":
        response["result"]["application_readiness"] = "passed"
    elif change in {"target_identity", "db_connectivity", "authentication", "verification_runtime"}:
        response["result"][change] = "not_checked"
    else:
        response["result"]["certificate_validation"] = "not_checked"
    with pytest.raises(consumer["ConsumerError"]):
        consumer["validate_response"](encode(response), 0, "verify")


@pytest.mark.parametrize(
    "response,code", [(INSPECT, 1), (failure(), 0), (failure(), 7), (failure(code="UNKNOWN"), 1), (live_response(), -9)]
)
def test_exit_status_and_known_error_must_agree(consumer: dict[str, Any], response: dict[str, Any], code: int) -> None:
    with pytest.raises(consumer["ConsumerError"]):
        consumer["validate_response"](encode(response), code, response["command"])


def test_partial_live_failure_is_never_reported_as_complete(consumer: dict[str, Any]) -> None:
    response = live_response()
    response["status"] = "failed"
    response["errors"] = [{"code": "TIMEOUT", "message": CANARY}]
    response["result"]["checks"]["statement_timeout"] = "failed"
    valid = consumer["validate_response"](encode(response), 5, "verify")
    summary = consumer["summarize"](valid)
    assert summary["status"] == "failed" and summary["error"] == "TIMEOUT"
    assert "checks" not in summary and CANARY not in json.dumps(summary)


@pytest.mark.parametrize(
    "response,stderr", [(b"{" + CANARY.encode(), b""), (encode(INSPECT), CANARY.encode()), (b"x" * 20000, b"")]
)
def test_real_subprocess_protocol_failures_are_redacted(tmp_path: Path, response: bytes, stderr: bytes) -> None:
    tool = fake_passport(tmp_path, response, stderr=stderr)
    result = run_helper(ADMIN, "inspect", "--passport", str(tool), "--request", "-")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "PASSPORT_RESPONSE_INVALID"


def test_subprocess_timeout_is_bounded_and_reaped(consumer: dict[str, Any], tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f"import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(20)"
    with pytest.raises(consumer["ConsumerError"], match="PASSPORT_TIMEOUT"):
        consumer["run_tool"]([sys.executable, "-c", script], None, 0.2)
    if pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


def test_unavailable_executable_is_classified(tmp_path: Path) -> None:
    result = run_helper(ADMIN, "capabilities", "--passport", str(tmp_path / CANARY))
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "PASSPORT_UNAVAILABLE"


def test_closed_stdout_does_not_leak_traceback() -> None:
    result = subprocess.run(
        [sys.executable, str(ADMIN), "verify"],
        preexec_fn=lambda: os.close(1),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


def test_offline_plan_remains_unexecutable(tmp_path: Path) -> None:
    response = copy.deepcopy(INSPECT)
    response["command"] = "plan"
    response["status"] = "planned"
    response["result"].update(
        {
            "input_digest": "sha256:" + "a" * 64,
            "plan_digest": "sha256:" + "b" * 64,
            "policy_revision": "m2-local-docker-1",
            "executable": False,
            "target_snapshot": "unknown",
            "differences": "unknown",
            "actions": [],
            "desired_state": {"sslmode": "verify-full", "authentication": "client-certificate"},
            "required_capabilities": ["connection.verify.v1"],
            "next_action": "authorized_read_only_verification",
            "preconditions": [
                "query_man_profile_validation",
                "authorized_executor_target_binding",
                "live_target_snapshot",
            ],
            "verification": [
                "target_identity",
                "db_connectivity",
                "certificate_validation",
                "authentication",
                "deployment",
            ],
            "stop_conditions": ["unsupported_capability", "missing_authorization", "target_mismatch"],
            "recovery": "no_changes_performed",
        }
    )
    tool = fake_passport(tmp_path, encode(response))
    result = run_helper(ADMIN, "plan", "--passport", str(tool), "--request", "-")
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["status"] == "planned" and summary["executable"] is False
    assert summary["source_count"] == 0 and summary["db_connectivity"] == "not_checked"


def test_stdin_size_limit_prevents_requested_command(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(INSPECT))
    result = run_helper(ADMIN, "inspect", "--passport", str(tool), "--request", "-", data=b" " * 65537)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "INPUT_TOO_LARGE"
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1


def test_open_stdin_without_eof_times_out(tmp_path: Path) -> None:
    tool = fake_passport(tmp_path, encode(INSPECT))
    process = subprocess.Popen(
        [sys.executable, str(ADMIN), "inspect", "--passport", str(tool), "--request", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=8) == 1
        out, err = process.communicate()
        assert err == b""
        assert json.loads(out)["error"] == "PASSPORT_TIMEOUT"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_missing_shared_dba_resource_is_safe(tmp_path: Path) -> None:
    path = tmp_path / ".agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(DBA.read_bytes())
    result = run_helper(path, "--request", "-")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "PASSPORT_UNAVAILABLE"
