#!/usr/bin/env python3
"""Bounded, fail-closed consumer for an operator-installed Query Passport 0.3.0.

The Admin entrypoint exposes only offline commands. The DBA skill reuses the
consumer through its separate DBA entrypoint after existing Execute approval.
This helper grants no authority and does not open credentials or executor bindings.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any, Never

VERSION = "0.3.0"
MAX_OUTPUT = 16384
MAX_INPUT = 65536
COMMAND_CAPABILITY = {
    "capabilities": None,
    "inspect": "profile.inspect.v1",
    "plan": "plan.offline.v1",
    "verify": "connection.verify.v1",
    "prepare": "lifecycle.local.v1",
    "issue": "lifecycle.local.v1",
    "apply": "lifecycle.local.v1",
    "deliver": "lifecycle.local.v1",
    "rotate": "credential.rotate.local.v1",
    "rollback": "lifecycle.local.v1",
    "status": "lifecycle.local.v1",
}
LIFECYCLE = frozenset({"prepare", "issue", "apply", "deliver", "rotate", "rollback", "status"})
PHASES = frozenset(
    {
        "prepared",
        "issuing",
        "issued",
        "applying",
        "applied",
        "delivering",
        "checking_delivery",
        "delivered",
        "verifying",
        "verified",
        "rolling_back",
        "rolled_back",
        "partial_failure",
        "unknown",
    }
)
LIFECYCLE_UNCHANGED = frozenset(
    {
        "source_inventory",
        "reader_permissions",
        "source_admission",
        "deployment",
        "application_readiness",
    }
)
LIFECYCLE_BASE = LIFECYCLE_UNCHANGED | frozenset(
    {
        "operation_id",
        "plan_digest",
        "phase",
        "intent",
        "mode",
        "source_count",
        "certificate_validation",
        "db_connectivity",
        "authentication",
        "target_identity",
        "recovery",
    }
)
NOT_CONFIRMED = {"outcome": "not_confirmed", "next_action": "status_or_scoped_recovery"}
PREPARE_ACTIONS = {
    "provision": [
        "issue_client_certificate",
        "create_restricted_check_role",
        "append_client_ca_trust",
        "install_owned_auth_rules",
        "publish_verified_credential",
    ],
    "rotate": ["issue_client_certificate", "verify_new_connections", "publish_verified_credential"],
}
PREPARE_PRESERVES = [
    "existing_database_objects",
    "existing_roles_and_grants",
    "existing_server_credentials",
    "existing_client_trust",
    "previous_credential_generations",
]
CHECKS = frozenset(
    {
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
    }
)
UNCHANGED = frozenset(
    {
        "source_inventory",
        "query_man_validation",
        "deployment",
        "reader_permissions",
        "source_admission",
        "application_readiness",
    }
)
LIVE = frozenset({"target_identity", "db_connectivity", "certificate_validation", "authentication"})
BASE = (
    frozenset({"mode", "profile_count", "source_count", "profile_validation", "profile_validation_scope"})
    | UNCHANGED
    | LIVE
)
ERROR_EXITS = {
    "INVALID_INPUT": 2,
    "INPUT_TOO_LARGE": 2,
    "UNSUPPORTED_OPERATION": 3,
    "UNSUPPORTED_VERSION": 3,
    "INPUT_ACCESS_DENIED": 4,
    "TIMEOUT": 5,
    "INTERNAL_ERROR": 1,
    "OUTPUT_TOO_LARGE": 1,
    "INTERRUPTED": 130,
    "AUTHORIZATION_REQUIRED": 6,
    "TARGET_MISMATCH": 7,
    "TARGET_DRIFT": 7,
    "EXECUTOR_FAILED": 8,
    "CREDENTIAL_ACCESS_DENIED": 8,
    "TLS_VERIFICATION_FAILED": 8,
    "CLIENT_AUTHENTICATION_FAILED": 8,
    "PERMISSION_DENIED": 8,
    "CONNECTION_FAILED": 8,
    "VERIFICATION_FAILED": 8,
    "RECOVERY_REQUIRED": 9,
}
PLAN_FIXED: dict[str, Any] = {
    "policy_revision": "m3-local-lifecycle-1",
    "executable": False,
    "target_snapshot": "unknown",
    "differences": "unknown",
    "actions": [],
    "desired_state": {"sslmode": "verify-full", "authentication": "client-certificate"},
    "required_capabilities": ["connection.verify.v1"],
    "next_action": "authorized_read_only_verification",
    "preconditions": ["query_man_profile_validation", "authorized_executor_target_binding", "live_target_snapshot"],
    "verification": ["target_identity", "db_connectivity", "certificate_validation", "authentication", "deployment"],
    "stop_conditions": ["unsupported_capability", "missing_authorization", "target_mismatch"],
    "recovery": "no_changes_performed",
}


class ConsumerError(Exception):
    def __init__(self, code: str = "PASSPORT_RESPONSE_INVALID") -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "PASSPORT_RESPONSE_INVALID") -> None:
    if not condition:
        raise ConsumerError(code)


def _object(value: Any, keys: set[str] | frozenset[str]) -> None:
    _require(type(value) is dict and value.keys() == keys)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(_: str) -> Never:
    raise ConsumerError()


def _parse(raw: bytes) -> dict[str, Any]:
    _require(0 < len(raw) <= MAX_OUTPUT and raw.endswith(b"\n") and raw.count(b"\n") == 1)
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise ConsumerError() from error
    stack = [(result, 1)]
    while stack:
        item, depth = stack.pop()
        _require(depth <= 8)
        if isinstance(item, dict):
            stack.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            stack.extend((value, depth + 1) for value in item)
    _object(result, {"contract_version", "tool_version", "command", "status", "scope", "result", "errors"})
    return dict(result)


def _capabilities(result: Any) -> None:
    _object(
        result,
        {
            "supported_contract_majors",
            "capabilities",
            "commands",
            "backend_types",
            "policy_revision",
            "profile_versions",
            "source_reference_version",
            "scopes",
            "environments",
            "live_environments",
            "limits",
        },
    )
    _require(result["supported_contract_majors"] == [1] and type(result["supported_contract_majors"][0]) is int)
    _require(result["profile_versions"] == [1] and type(result["profile_versions"][0]) is int)
    _require(type(result["source_reference_version"]) is int and result["source_reference_version"] == 6)
    _require(result["policy_revision"] == "m3-local-lifecycle-1")
    _require(result["scopes"] == ["database-only"])
    _require(result["backend_types"] == ["offline", "local-docker"])
    _require(result["environments"] == ["local-synthetic", "local", "protected"])
    _require(result["live_environments"] == ["local-synthetic", "local"])
    for name, allowed in (
        ("commands", set(COMMAND_CAPABILITY)),
        ("capabilities", set(COMMAND_CAPABILITY.values()) - {None}),
    ):
        values = result[name]
        _require(type(values) is list and all(type(item) is str for item in values))
        _require(len(values) == len(set(values)) and set(values) <= allowed)
    limits = {
        "input_bytes": MAX_INPUT,
        "output_bytes": MAX_OUTPUT,
        "json_depth": 8,
        "timeout_seconds": 5,
        "live_timeout_seconds": 60,
        "lifecycle_timeout_seconds": 180,
    }
    _object(result["limits"], set(limits))
    _require(
        all(type(result["limits"][key]) is int and result["limits"][key] == value for key, value in limits.items())
    )


def _result(command: str, result: Any, *, succeeded: bool) -> None:
    additional: set[str] = set()
    if command == "plan":
        additional = set(PLAN_FIXED) | {"input_digest", "plan_digest"}
    elif command == "verify":
        additional = {"checks", "verification_scope", "executor_target_binding", "verification_runtime"}
    _object(result, BASE | additional)
    _require(type(result["profile_count"]) is int and result["profile_count"] == 1)
    _require(type(result["source_count"]) is int and 0 <= result["source_count"] <= 1000000)
    _require(result["profile_validation"] == "passed")
    _require(result["profile_validation_scope"] == "public_projection_only")
    _require(all(result[field] == "not_checked" for field in UNCHANGED))
    if command != "verify":
        _require(result["mode"] == "offline" and all(result[field] == "not_checked" for field in LIVE))
    else:
        _require(result["mode"] == "live" and result["verification_scope"] == "database-only")
        _require(result["executor_target_binding"] == "passed")
        checks = result["checks"]
        _object(checks, CHECKS)
        _require(all(type(state) is str and state in {"passed", "failed", "not_checked"} for state in checks.values()))
        _require(all(state == "passed" for state in checks.values()) == succeeded)
        _require(result["target_identity"] == checks["target"])
        _require(result["db_connectivity"] == checks["reconnect"])
        _require(result["authentication"] == checks["client_identity"])
        _require(result["verification_runtime"] == checks["runtime_identity"])
        certificate = "passed" if checks["tls"] == checks["client_identity"] == "passed" else "not_checked"
        _require(result["certificate_validation"] == certificate)
    if command == "plan":
        _require(result["executable"] is False)
        _require(all(result[key] == value for key, value in PLAN_FIXED.items()))
        for field in ("input_digest", "plan_digest"):
            _require(type(result[field]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", result[field]) is not None)


def _operation_identity(result: dict[str, Any]) -> None:
    for name, pattern in (("operation_id", r"[0-9a-f]{32}"), ("plan_digest", r"sha256:[0-9a-f]{64}")):
        _require(type(result[name]) is str and re.fullmatch(pattern, result[name]) is not None)


def _lifecycle_result(command: str, result: Any) -> None:
    extra = (
        {"actions", "preserves", "account", "client_dn", "certificate_lifetime_days"} if command == "prepare" else set()
    )
    _object(result, LIFECYCLE_BASE | extra)
    _operation_identity(result)
    _require(type(result["source_count"]) is int and 0 <= result["source_count"] <= 1000000)
    _require(result["mode"] == "live" and result["intent"] in ("provision", "rotate"))
    _require(type(result["phase"]) is str and result["phase"] in PHASES)
    expected = {
        "prepare": "prepared",
        "issue": "issued",
        "apply": "applied",
        "deliver": "verified",
        "rotate": "verified",
        "rollback": "rolled_back",
    }
    if command != "status":
        _require(result["phase"] == expected[command])
    if command == "rotate":
        _require(result["intent"] == "rotate")
    _require(all(result[field] == "not_checked" for field in LIFECYCLE_UNCHANGED))
    _require(result["target_identity"] == "passed" and result["recovery"] == "owned_changes_only")
    observation = "passed" if command in ("deliver", "rotate") else "not_checked"
    _require(
        all(result[field] == observation for field in ("certificate_validation", "db_connectivity", "authentication"))
    )
    if command == "prepare":
        _require(
            type(result["account"]) is str and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", result["account"]) is not None
        )
        _require(
            type(result["client_dn"]) is str
            and re.fullmatch(r"CN=[a-z][a-z0-9-]{0,124}", result["client_dn"]) is not None
        )
        _require(type(result["certificate_lifetime_days"]) is int and 1 <= result["certificate_lifetime_days"] <= 90)
        # Filled from the fixed provider contract below; never forward arbitrary plan prose.
        _require(result["actions"] == PREPARE_ACTIONS[result["intent"]])
        _require(result["preserves"] == PREPARE_PRESERVES)


def _unconfirmed_result(result: Any) -> None:
    _object(result, {"operation_id", "plan_digest", *NOT_CONFIRMED})
    _operation_identity(result)
    _require(all(result[name] == value for name, value in NOT_CONFIRMED.items()))


def validate_response(raw: bytes, returncode: int, command: str) -> dict[str, Any]:
    """Validate provider bytes before any status or claim is exposed to the skill."""
    _require(command in COMMAND_CAPABILITY and type(returncode) is int)
    response = _parse(raw)
    _require(response["contract_version"] == "1" and response["tool_version"] == VERSION, "PASSPORT_INCOMPATIBLE")
    _require(response["command"] == command)
    result = response["result"]
    _require(type(result) is dict)
    if returncode == 0:
        expected_status = {
            "capabilities": "validated",
            "inspect": "validated",
            "plan": "planned",
            "verify": "succeeded",
            "prepare": "planned",
            "issue": "succeeded",
            "apply": "succeeded",
            "deliver": "succeeded",
            "rotate": "succeeded",
            "rollback": "succeeded",
            "status": "validated",
        }
        _require(response["status"] == expected_status[command] and response["errors"] == [])
        _require(response["scope"] == (None if command == "capabilities" else "database-only"))
        if command == "capabilities":
            _capabilities(result)
        elif command in LIFECYCLE:
            _lifecycle_result(command, result)
        else:
            _result(command, result, succeeded=True)
    else:
        _require(response["status"] == "failed" and response["scope"] in (None, "database-only"))
        errors = response["errors"]
        _require(type(errors) is list and len(errors) == 1)
        _object(errors[0], {"code", "message"})
        code = errors[0]["code"]
        _require(type(code) is str and code in ERROR_EXITS and ERROR_EXITS[code] == returncode)
        _require(type(errors[0]["message"]) is str and len(errors[0]["message"]) <= 512)
        if result:
            if command in LIFECYCLE - {"prepare"}:
                # CLI failures can occur before a scope is established. A validated
                # opaque operation reference is recovery context, not authorization.
                _unconfirmed_result(result)
            else:
                _require(command == "verify" and response["scope"] == "database-only")
                _result(command, result, succeeded=False)
    return response


def summarize(response: dict[str, Any]) -> dict[str, Any]:
    """Copy only validated counts and fixed statuses, never provider diagnostics."""
    result = response["result"]
    summary = {
        "tool": "query-passport",
        "tool_version": VERSION,
        "contract_version": "1",
        "command": response["command"],
        "status": response["status"],
    }
    if response["errors"]:
        summary["error"] = response["errors"][0]["code"]
        if response["command"] in LIFECYCLE - {"prepare"}:
            summary.update(NOT_CONFIRMED)
            if result:
                summary.update({name: result[name] for name in ("operation_id", "plan_digest")})
        return summary
    if response["command"] == "capabilities":
        summary["capabilities"] = sorted(result["capabilities"])
        return summary
    if response["command"] in LIFECYCLE:
        summary["scope"] = "database-only"
        summary.update(result)
        return summary
    summary.update(
        {
            "scope": "database-only",
            "mode": result["mode"],
            "source_count": result["source_count"],
            "profile_validation_scope": "public_projection_only",
        }
    )
    summary.update({field: result[field] for field in UNCHANGED | LIVE})
    if response["command"] == "plan":
        summary["executable"] = False
    if response["command"] == "verify":
        summary["checks"] = {name: result["checks"][name] for name in sorted(CHECKS)}
    return summary


def run_tool(arguments: list[str], data: bytes | None, timeout: float) -> tuple[bytes, int]:
    """Consume at most one bounded JSON response and never retain raw stderr."""
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        output = bytearray()
        remaining = memoryview(data or b"")
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if remaining:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map():
                available = deadline - time.monotonic()
                _require(available > 0, "PASSPORT_TIMEOUT")
                for key, _ in selector.select(available):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, remaining[:4096])
                            remaining = remaining[written:]
                        except BrokenPipeError:
                            remaining = memoryview(b"")
                        if not remaining:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, min(4096, MAX_OUTPUT + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    _require(key.data != "stderr")
                    output.extend(chunk)
                    _require(len(output) <= MAX_OUTPUT)
        try:
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ConsumerError("PASSPORT_TIMEOUT") from error
        return bytes(output), code
    except OSError as error:
        raise ConsumerError("PASSPORT_UNAVAILABLE") from error
    finally:
        if process is not None:
            # Kill descendants too if an executable left inherited pipe handles
            # open. This process group contains only this helper's invocation.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            for remaining_stream in (process.stdin, process.stdout, process.stderr):
                if remaining_stream is not None:
                    remaining_stream.close()


def _stdin() -> bytes:
    chunks = bytearray()
    deadline = time.monotonic() + 5
    # select() accepts redirected regular files as well as pipes; epoll does not.
    with selectors.SelectSelector() as selector:
        selector.register(sys.stdin, selectors.EVENT_READ)
        while len(chunks) <= MAX_INPUT:
            remaining = deadline - time.monotonic()
            _require(remaining > 0 and bool(selector.select(remaining)), "PASSPORT_TIMEOUT")
            chunk = os.read(sys.stdin.fileno(), min(4096, MAX_INPUT + 1 - len(chunks)))
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
    raise ConsumerError("INPUT_TOO_LARGE")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ConsumerError("INVALID_INPUT")


def run(arguments: Sequence[str], *, commands: tuple[str, ...] = ("capabilities", "inspect", "plan")) -> int:
    command = None
    code = 1
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument("command", choices=commands)
        parser.add_argument("--passport", default="query-passport")
        parser.add_argument("--request")
        parser.add_argument("--workspace", default=".")
        options = parser.parse_args(list(arguments))
        command = options.command
        _require((command == "capabilities") == (options.request is None), "INVALID_INPUT")
        raw, code = run_tool([options.passport, "capabilities", "--format", "json"], None, 10)
        capability = validate_response(raw, code, "capabilities")
        if code:
            raise ConsumerError(capability["errors"][0]["code"])
        _require(command in capability["result"]["commands"], "PASSPORT_INCOMPATIBLE")
        required = COMMAND_CAPABILITY[command]
        _require(required is None or required in capability["result"]["capabilities"], "PASSPORT_INCOMPATIBLE")
        if command in LIFECYCLE:
            _require("lifecycle.local.v1" in capability["result"]["capabilities"], "PASSPORT_INCOMPATIBLE")
        response = capability
        if command != "capabilities":
            data = _stdin() if options.request == "-" else None
            raw, code = run_tool(
                [
                    options.passport,
                    command,
                    "--request",
                    options.request,
                    "--workspace",
                    options.workspace,
                    "--format",
                    "json",
                ],
                data,
                190 if command in LIFECYCLE else 70 if command == "verify" else 10,
            )
            response = validate_response(raw, code, command)
        summary = summarize(response)
    except ConsumerError as error:
        code = ERROR_EXITS.get(error.code, 1)
        summary = {"tool": "query-passport", "command": command, "status": "failed", "error": error.code}
    except KeyboardInterrupt:
        code = 130
        summary = {"tool": "query-passport", "command": command, "status": "failed", "error": "INTERRUPTED"}
    except Exception:
        code = 1
        summary = {
            "tool": "query-passport",
            "command": command,
            "status": "failed",
            "error": "PASSPORT_RESPONSE_INVALID",
        }
    if code and command in LIFECYCLE - {"prepare"}:
        # An interrupted/invalid response cannot tell us which effects committed.
        summary.update(NOT_CONFIRMED)
    try:
        if sys.stdout is None:
            return 1
        os.write(sys.stdout.fileno(), (json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n").encode())
    except (OSError, ValueError, AttributeError):
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
