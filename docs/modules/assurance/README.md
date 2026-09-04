# Assurance Module

Status: Active

## 30초 요약

Assurance는 production publication authority가 아니라 security corpus와 작은 Query Cave PostgreSQL로
실제 product safety/lifecycle을 검증하는 repository gate입니다.

## 책임과 interface

- Versioned malicious/invalid SQL corpus와 fail-closed assertions
- HTTP, PostgreSQL integration, container와 lifecycle gates
- Bounded load, cancel·rollback·disconnect evidence
- Exact result OID/canonical encoding negative tests
- Repository provenance와 protected evidence의 분리

Tests는 production Source Catalog, Metadata, Guarded Query, Delivery와 Runtime 경로를 사용합니다. Source별
business question/result corpus, production source 목록 복제나 test-only publication shortcut을 두지
않습니다. Real-DB gate는 하나의 tiny synthetic source만 사용합니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `config/security-evaluation.yaml` | Versioned security corpus |
| `tests/test_security_evaluation.py` | SQL/input fail-closed와 test-output secret redaction |
| `tests/test_documentation.py` | Current 문서 탐색, ADR index, retired reference와 local link |
| `tests/test_source_view_artifacts.py` | Reviewed source package와 desired view SQL artifact |
| `tests/test_database_integration.py` | Real PostgreSQL reader/transaction safety kernel |
| `query-cave/` | Production inventory와 분리된 certificate-authenticated source와 PostgreSQL |
| `tests/test_integration.py`, `tests/test_load.py` | Composition/disconnect와 bounded load |
| `scripts/query-cave.sh`, `scripts/verify-query-cave.sh`, `scripts/verify-container.sh`, `.github/workflows/` | Query Cave local lifecycle, built image와 repository CI |

## 불변조건과 승인

- Parser/allowlist, reader, revision, exact OID, cancel·rollback과 secret redaction gate를 약화하지 않습니다.
- Query Cave는 production DB procedure나 protected evidence가 아닙니다.
- PR/push 필수 gate는 static/unit 검사이며 Query Cave DB/container와 image scan은 수동 workflow입니다.
- Required CI/container/load pass/fail 의미 변경은 별도 승인 대상입니다.

## 검증

```bash
uv run pytest tests/test_security_evaluation.py tests/test_integration.py tests/test_load.py
./scripts/verify-query-cave.sh
./scripts/verify-container.sh
```

## 집중해서 읽을 범위

Security corpus는 policy owner tests, real DB는 Query Cave/catalog/query lifecycle, container는 Docker/Compose와
CI, load는 admission/timeout/cancel path까지 함께 읽습니다.
