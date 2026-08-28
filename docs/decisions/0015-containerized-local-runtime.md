# ADR 0015: Containerized Local Runtime

> Current exception: [ADR 0028](0028-interactive-operator-shell.md)은 기존 query-only caller를 유지하면서
> local Compose에 health/metric과 operator CLI용 별도 `operator-local` caller를 추가한다. Source authority와
> managed route 활성화 범위는 바뀌지 않는다.

Status: Accepted

Date: 2026-08-23

## Context

Local Compose는 PostgreSQL만 실행하고 Query Man HTTP/MCP process는 host의 `uv run
query-man`으로 별도 실행했다. 이 방식은 application lifecycle과 readiness를 Compose가
관리하지 못한다. 단순히 application container를 추가하면 source manifest의
`127.0.0.1`이 container 자신을 가리키고, `.env` 전체 전달은 PostgreSQL administrator
credential까지 application에 노출한다.

## Decision

하나의 `query-man` container가 기존 HTTP API와 stateless Streamable HTTP `/mcp` endpoint를
함께 제공한다. Compose network에서는 source manifest의 선택적 `host_env`를
`QUERY_MAN_POSTGRES_HOST=postgres`로 resolve하고 internal PostgreSQL port는 항상 `5432`를
사용한다. Host에서 실행할 때 환경변수가 없으면 manifest의 `127.0.0.1` fallback을 유지한다.
Control-plane publish는 resolved host와 port만 저장해 replica별 환경이 published endpoint를
바꾸지 못하게 한다.

Application은 container 내부 `0.0.0.0:3000`에 bind하고 host에는
`127.0.0.1:${QUERY_MAN_PORT:-3000}`으로만 publish한다. Non-loopback bind 안전 규칙에 따라
Compose 전용 access policy와 `QUERY_MAN_CODEX_MCP_TOKEN`을 필수로 사용한다. 이 caller는 두
active bootstrap source를 모두 볼 수 있지만 operator 권한이 없는 query-only identity다.
Compose는 필요한 reader secret과
runtime setting만 명시적으로 전달하며 PostgreSQL administrator credential은 전달하지 않는다.
Source authority는 `QUERY_MAN_SOURCE_MODE=bootstrap`으로 고정하고 Control DSN/key를 전달하지
않는다. Managed production topology를 이 local default와 섞지 않는다.
MCP transport의 DNS rebinding 보호는 bind 주소와 관계없이 활성화하고 기본 Compose의 허용
Host와 Origin은 loopback으로 제한한다.

Runtime image는 locked production dependency만 non-editable로 설치하고 non-root user로
실행한다. Filesystem은 read-only이며 `/tmp`만 tmpfs다. PostgreSQL health 뒤에 application을
시작하고 `/ready`를 container healthcheck로 사용한다. Docker stop grace는 기본 application
drain grace보다 길게 유지한다.

## Consequences

- PostgreSQL 시작, database 적용, Query Man build/start 순서로 Compose가 전체 lifecycle을
  관리한다.
- Host `uv` 개발은 `docker compose up -d --wait postgres`만 실행해 기존 loopback source
  fallback을 사용한다.
- Compose token은 Git에 저장하지 않고 `.env`에서 주입한다. Codex client에도 같은 값만
  별도로 전달해야 한다.
- 기본 Compose는 production TLS, control-plane writer와 horizontal replica 구성을 제공하지
  않는다. 해당 배포는 별도 secret/orchestrator 설정과 connection-capacity 재산정이 필요하다.
