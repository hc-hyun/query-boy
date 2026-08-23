# Container Runtime Audit — 2026-08-23

Status: Complete

> 이 감사의 source-limited Compose policy 문구는 당시 실행 증거다. 현재 Compose policy
> version 2의 query-only/shared visibility 계약은
> [shared access audit](2026-08-23-shared-access.md)이 우선한다.

## Scope And Verdict

이 감사는 [roadmap](../implementation-roadmap.md)의 `DEP-01`~`DEP-08`을 닫는다. Query Man의
HTTP API와 stateless Streamable HTTP `/mcp` endpoint는 별도 process가 아니라 하나의
Compose application container에서 같은 registry, authorization, metadata cache와 query
admission을 공유한다. PostgreSQL과 application image를 실제로 build/start한 뒤 published
loopback port에서 인증, readiness와 실제 MCP query까지 검증했다.

## Checklist Evidence

| ID | Primary evidence | Result |
|---|---|---|
| `DEP-01` | [ADR 0015](../decisions/0015-containerized-local-runtime.md)가 단일 HTTP/MCP container, Compose network, 인증·secret과 host 개발 경계를 고정한다. | PASS |
| `DEP-02` | `Dockerfile`은 locked non-dev dependency를 multi-stage로 non-editable 설치하고 UID 10001의 direct `query-man` entrypoint만 실행한다. Compose는 read-only root filesystem, `/tmp` tmpfs, capability drop과 no-new-privileges를 적용한다. | PASS |
| `DEP-03` | Source manifest의 optional `host_env`는 host에서 `127.0.0.1`, Compose에서 `postgres`로 resolve된다. Registry와 control-plane validation 회귀가 빈 override 거부 및 resolved host canonicalization을 확인한다. | PASS |
| `DEP-04` | PostgreSQL TCP health 완료 뒤 application을 시작하고 host port는 loopback에만 publish한다. `/ready` healthcheck와 30초 stop grace가 application 기본 drain 10초를 감싼다. | PASS |
| `DEP-05` | Compose가 PostgreSQL administrator secret과 application reader secret을 분리한다. 당시 source-limited caller token, 명시적 MCP Host/Origin allowlist와 악성 header 회귀가 fail-closed 경계를 확인했다. | PASS |
| `DEP-06` | `scripts/verify-container.sh`가 exact `ready`, 무인증 401, 세 MCP tool, 두 authorized source, L2 context와 `issue_count=600` guarded query를 실제 container에서 확인한다. | PASS |
| `DEP-07` | CI의 host 회귀는 PostgreSQL만 기동하고 별도 container job이 전체 stack을 검증한다. Security job은 application image를 새로 build해 수정 가능한 Critical vulnerability를 gate한다. | PASS |
| `DEP-08` | README, architecture, MVP와 operations 문서가 동일한 시작·검증·종료 절차와 외부 proxy allowlist 경계를 설명하며 documentation 회귀가 링크와 123개 roadmap ID를 검사한다. | PASS |

## Executed Evidence

모든 명령은 repository root에서 PostgreSQL 18.6 fixture와 locked `uv` 환경으로 실행했다.

```text
docker compose config --quiet
  PASS

docker compose up -d --wait postgres
./scripts/apply-db.sh
docker compose up -d --build --wait query-man
  PASS (postgres and query-man healthy)

./scripts/verify-container.sh
  PASS (exact ready; unauthenticated 401; UID 10001; read-only root;
        image excludes .env/.git/tests; MCP list/get_context/query; issue_count 600)

Trivy v0.72.0 query-man:local image
  PASS (0 fixed CRITICAL findings across Debian and Python packages)

uv run ruff check .
  PASS

uv run mypy src
  PASS (26 source files)

uv run pytest
  PASS (210 passed, 17 deselected integration tests)

uv run pytest -m integration
  PASS (17 passed, 210 deselected)

uv run pytest -m load -s
  PASS (40 queries, 2 sources, 1 passed, 226 deselected)
  service_wall_ms: p50 611, p95 836, max 874
  execution_ms:    p50 51, p95 98, max 105
  queue_ms:        p95 621, max 646
  max plan total_cost: 215.55

uv run query-man-evaluate
  PASS (16 cases; relation accuracy 1.0, answerability recall 1.0,
        average context 7,892 bytes, maximum context 13,509 bytes)

uv run query-man-verify
  PASS (9 result contracts)
```

## Deliberate Boundaries

- 기본 Compose는 loopback 개발 runtime이다. Production TLS termination, public ingress,
  horizontal replica와 control-plane writer를 배포하지 않는다.
- Environment variable secret은 image와 Git에는 포함되지 않지만 Docker daemon 권한이 있는
  운영자는 container inspection으로 볼 수 있다. Production orchestrator에서는 secret store와
  별도 credential 전달 경계를 구성해야 한다.
- `/ready`의 `degraded`도 partial availability를 위해 HTTP 200이다. Container health는 이를
  허용하지만 release smoke는 exact `ready`를 별도로 요구한다.
- Reverse proxy 배포는 실제 public Host와 HTTPS Origin을
  `QUERY_MAN_MCP_ALLOWED_HOSTS`/`QUERY_MAN_MCP_ALLOWED_ORIGINS`에 명시해야 한다.
- 현재 Compose의 version 2 caller는 모든 active bootstrap source를 보지만 operator 및
  source-admin 권한이 없는 query-only identity다.
- Container stdout은 JSON application/audit event와 Uvicorn text lifecycle/access line이 섞인
  line-oriented stream이다. Collector는 두 형식을 구분해 수집해야 한다.

이 경계 안에서 123개 roadmap checklist와 containerized HTTP/MCP local runtime의 구현,
자동 검증 및 운영 문서가 일치한다.
