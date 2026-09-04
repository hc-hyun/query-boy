#!/usr/bin/env python3
"""Make bounded, read-only Query Man administrative requests without token argv exposure."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import stat
import sys
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
TOKEN_MIN_BYTES = 32
TOKEN_MAX_BYTES = 512


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect an already-running Query Man server.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ready", help="read the unauthenticated readiness endpoint")
    commands.add_parser("status", help="read operator health status")
    commands.add_parser("metrics", help="read process-local operator metrics")
    commands.add_parser("sources", help="list sources visible to the operator")
    meta = commands.add_parser("meta", help="read metadata for one source")
    meta.add_argument("source_id")
    return parser


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _server_url() -> str:
    raw = os.environ.get("QUERY_MAN_SERVER_URL", "").strip()
    if not raw:
        raise ValueError("QUERY_MAN_SERVER_URL is required")
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("QUERY_MAN_SERVER_URL has an invalid port") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("QUERY_MAN_SERVER_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("QUERY_MAN_SERVER_URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("QUERY_MAN_SERVER_URL must not contain a path")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ValueError("plaintext HTTP is allowed only for loopback")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{authority}"


def _token_from_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("operator token path must be a regular file")
        if metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError("operator token file has an unapproved owner")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("operator token file permissions must not allow group or other access")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(TOKEN_MAX_BYTES + 2)
    finally:
        os.close(descriptor)


def _validate_token(raw: bytes) -> bytes:
    token = raw[:-1] if raw.endswith(b"\n") else raw
    if b"\n" in token or b"\r" in token:
        raise ValueError("operator token must be one line")
    if not TOKEN_MIN_BYTES <= len(token) <= TOKEN_MAX_BYTES:
        raise ValueError("operator token length is invalid")
    if any(byte < 0x21 or byte > 0x7E for byte in token):
        raise ValueError("operator token must contain visible ASCII only")
    return token


def _token() -> bytes:
    token_file = os.environ.get("QUERY_MAN_OPERATOR_TOKEN_FILE")
    token_value = os.environ.get("QUERY_MAN_OPERATOR_TOKEN")
    if bool(token_file) == bool(token_value):
        raise ValueError("set exactly one approved operator token source")
    raw = _token_from_file(Path(token_file)) if token_file else token_value.encode("ascii", errors="strict")
    return _validate_token(raw)


def _read_body(stream: BinaryIO) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("server response exceeded the size limit")
    return body


def _request(command: str, source_id: str | None) -> tuple[int, object]:
    routes = {
        "ready": ("GET", "/ready"),
        "status": ("GET", "/admin/health"),
        "metrics": ("GET", "/admin/metrics"),
        "sources": ("GET", "/sources"),
        "meta": ("POST", "/meta"),
    }
    method, path = routes[command]
    token = None if command == "ready" else _token()
    body = None
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token.decode('ascii')}"
    if command == "meta":
        if not source_id or len(source_id) > 128:
            raise ValueError("source_id is required and must not exceed 128 characters")
        body = json.dumps({"source_id": source_id}).encode("utf-8")
        headers["Content-Type"] = "application/json"

    context = None
    ca_file = os.environ.get("QUERY_MAN_SERVER_CA_FILE")
    if ca_file:
        context = ssl.create_default_context(cafile=ca_file)
    request = Request(f"{_server_url()}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
            status = response.status
            raw_response = _read_body(response)
    except HTTPError as error:
        status = error.code
        raw_response = _read_body(error)
    if token is not None:
        raw_response = raw_response.replace(token, b"[REDACTED]")
    try:
        response_body = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError):
        response_body = {"error": "server returned a non-JSON response"}
    return status, response_body


def run(arguments: list[str]) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        status, response = _request(parsed.command, getattr(parsed, "source_id", None))
    except (OSError, UnicodeError, URLError, ValueError):
        print("Query Man request failed; no credential or response details were printed.", file=sys.stderr)
        return 1
    print(json.dumps({"http_status": status, "response": response}, ensure_ascii=False, indent=2))
    return 0 if 200 <= status < 300 else 1


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
