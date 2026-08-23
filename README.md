# query-man

PostgreSQL 데이터 소스를 안전하게 조회하기 위한 Text-to-SQL gateway 프로젝트입니다.

## Local Compose Runtime

로컬 runtime은 PostgreSQL 18.6과 Query Man HTTP/MCP application을 Compose로 함께
실행합니다.

로컬 설정은 `.env`에 있으며 Git에서 제외됩니다. 공개 가능한 기본값은
`.env.example`에서 관리합니다. 최초 실행 전 reader password를 바꾸고
`QUERY_MAN_CODEX_MCP_TOKEN`은 `openssl rand -hex 32`로 생성한 값으로 교체합니다.

```bash
test -f .env || cp .env.example .env
docker compose up -d --wait postgres
./scripts/apply-db.sh
docker compose up -d --build --wait query-man
docker compose ps
curl -fsS http://127.0.0.1:${QUERY_MAN_PORT:-3000}/ready
./scripts/verify-container.sh
docker compose exec postgres \
  psql -U query_man_admin -d query_man
```

생성되는 database는 bootstrap source인 `development_issues`, `market_voc`와 no-deploy
onboarding acceptance용 `support_tickets`, `commerce_edges`입니다. 각 source의 AI reader
접속 정보는 `.env`에 있고, reader는 해당 database의 `ai` schema view만 조회할 수 있습니다.

PostgreSQL은 `127.0.0.1:${POSTGRES_PORT:-5432}`, Query Man은
`127.0.0.1:${QUERY_MAN_PORT:-3000}`에서만 접근할 수 있습니다.
데이터는 Compose named volume인 `query-man_postgres_data`에 저장됩니다. PostgreSQL
18부터 적용된 공식 image layout에 맞춰 `/var/lib/postgresql` 전체를 영속화합니다.
Query Man container는 non-root, read-only filesystem으로 실행되며 PostgreSQL administrator
password를 전달받지 않습니다. 로그는 `docker compose logs -f query-man`으로 확인합니다.

```bash
docker compose down
```

`docker compose down`은 데이터를 보존합니다. 프로젝트 DB까지 초기화할 때만
데이터 손실을 확인한 후 `docker compose down -v`를 사용합니다.

전체 설계 기준은 [docs/architecture.md](docs/architecture.md), 현재 MVP 범위는
[docs/mvp.md](docs/mvp.md), 완료된 구현 이력은
[docs/implementation-roadmap.md](docs/implementation-roadmap.md), 현재 우선순위 TODO는
[docs/development-todo.md](docs/development-todo.md), 기존 production baseline 증거는
[completion audit](docs/verification/2026-08-23-completion-audit.md), 현재 최종 회귀와 운영 경계는
[refactoring assurance](docs/verification/2026-08-23-refactoring-assurance.md), 컨테이너 실행 증거는
[container runtime audit](docs/verification/2026-08-23-container-runtime.md), 두 replica soak 증거는
[multi-replica soak audit](docs/verification/2026-08-23-mcp-multi-replica-soak.md)을 참고합니다.

## Metadata And Query API

Compose가 기본적으로 Query Man을 실행합니다. Application을 host의 Python 3.12와 `uv`로
직접 개발할 때만 PostgreSQL service를 단독으로 시작합니다.

```bash
docker compose stop query-man
docker compose up -d --wait postgres
uv sync --locked
uv run query-man
```

기본 주소는 `http://127.0.0.1:3000`입니다. Compose runtime은 source 범위가 제한된 bearer
caller를 사용합니다. 아래 예시에서는 Git에서 제외된 `.env`의 token 한 개만 현재 shell로
가져옵니다.

```bash
export QUERY_MAN_CODEX_MCP_TOKEN="$(sed -n 's/^QUERY_MAN_CODEX_MCP_TOKEN=//p' .env)"

curl http://127.0.0.1:3000/sources \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN"

curl -s http://127.0.0.1:3000/meta \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "question": "모델별 기기 수, VOC 수와 기기당 VOC 수를 보여줘"
  }'
```

`/meta`가 반환한 `metadata_revision`으로 한 개의 읽기 전용 SQL을 실행할 수 있습니다.

```bash
curl -s http://127.0.0.1:3000/query \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "sql": "SELECT count(*) AS voc_count FROM ai.voc_overview",
    "metadata_revision": "sha256:<value returned by /meta>"
  }'
```

Gateway는 현재 revision과 AST allowlist를 확인한 뒤 source별 동시 실행 수를 제한합니다.
실행 시 read-only transaction, statement/transaction/lock timeout을 강제하고, 명백히 비싼
`EXPLAIN` plan을 거부합니다. `work_mem`, temporary file, parallel worker와 JIT도
transaction-local profile 값으로 고정하며, 결과가 row 또는 UTF-8 byte 상한을 넘으면
`truncated: true`로 종료합니다. Planner cost는 보조적인 admission 신호이고 실제 실행
피해의 상한은 timeout, concurrency와 결과 제한이 담당합니다. 기본값은
[`config/budget-profiles.yaml`](config/budget-profiles.yaml)에서 관리합니다.
측정 신호, live query 조사와 budget 변경 순서는
[`docs/query-cost-control.md`](docs/query-cost-control.md)를 따릅니다.
Operator caller는 audit log에 기록된 실행 중 `query_id`를
`DELETE /queries/{query_id}`로 취소할 수 있으며, 자기 source allowlist 밖의 query는
조회하거나 취소할 수 없습니다.

Client는 DSN, host, database 또는 role을 전달할 수 없습니다. `source_id`는 bootstrap
[`config/sources`](config/sources) 또는 검증된 Control DB source generation의 server-side
registry에서만 연결 정보로 해석됩니다. Production hot-added source의 authority와 관리 목표는
[`docs/source-management-plane.md`](docs/source-management-plane.md)를 따릅니다.
Column, type과 database comment는 reader 권한으로 `pg_catalog`에서 자동 수집하고,
grain, 한국어 alias, 승인된 join, 검증된 measure와 business predicate만 manifest의
semantic overlay로 보강합니다. `/meta`의 `answerability`가 `needs_clarification` 또는
`unsupported`이면 SQL 생성을 진행하지 않아야 합니다.
기본 loopback bind에서는 로컬 개발을 위해 인증을 생략할 수 있다. 외부 주소에 bind할
때는 32자 이상의 `QUERY_MAN_API_TOKEN` 또는 `QUERY_MAN_ACCESS_POLICY_FILE`이 필수이며
`/sources`, `/meta`, `/query`에 `Authorization: Bearer ...` header를 보내야 합니다.

여러 caller/tenant가 서로 다른 source를 사용하면 `QUERY_MAN_API_TOKEN` 대신
`QUERY_MAN_ACCESS_POLICY_FILE`을 설정합니다. Policy는 token 값이 아닌 환경 변수 이름과
source allowlist만 저장하며 형식은
[`config/access-policies.example.yaml`](config/access-policies.example.yaml)을 참고합니다.

같은 service와 bearer 인증 경계가 MCP의 stateless Streamable HTTP endpoint
`http://127.0.0.1:3000/mcp`에도 적용됩니다. MCP는 `list_sources`, `get_context`, `query`
세 tool만 제공하며 host나 credential을 입력받지 않습니다. 모델 workflow에는
[`query-man-text-to-sql` Skill](skills/query-man-text-to-sql/SKILL.md)을 사용합니다.
Codex는 같은 `QUERY_MAN_CODEX_MCP_TOKEN`을 환경변수로 받은 새 session에서 연결해야 합니다.
Codex CLI 0.149.0에서는 modern MCP가 아직 opt-in 기능이므로 client project의
`.codex/config.toml`에 다음 설정이 필요합니다. `codex features list`에서
`mcp_2026_07_28`이 `true`인지 확인합니다.

```toml
[features]
mcp_2026_07_28 = true

[shell_environment_policy.filters]
QUERY_MAN_CODEX_MCP_TOKEN = "exclude"
```

Project `.env`는 Codex가 자동으로 읽지 않으므로 새 shell에서 실행할 때는 token을 먼저
export합니다. 환경 filter는 Codex 자체의 MCP 인증에는 token을 남기면서 Codex가 실행하는
shell command에는 전달하지 않습니다. Client `.env`에는 이 token만 두고 database password를
복사하지 않습니다. Codex가 이 protocol을 기본값으로 전환하거나 flag를 제거하는 release에서는
실제 `/mcp` 연결을 재검증한 뒤 feature override를 삭제합니다.
HTTP와 MCP container 경계는 `./scripts/verify-container.sh`로 함께 재검증할 수 있습니다.
Application 오류는 안전한 `structuredContent.error`와 `isError=true`를 함께 반환합니다.
MCP endpoint는 protocol version `2026-07-28`만 지원하며 이전 handshake, 누락·미지원·중복
version header를 거부합니다. 지원 중인 client가 실행 중인 `query` POST를 닫으면 gateway가
PostgreSQL 작업도 취소하고 rollback합니다. Version 경계는
[`ADR 0006`](docs/decisions/0006-mcp-transport-and-workflow.md)에 명시합니다.

개발 검증은 다음 명령으로 실행합니다.

```bash
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -m 'load and not mcp_server' -s
uv run pytest -m 'mcp_server and not soak' -s
uv run query-man-evaluate
uv run query-man-verify
```

테스트와 공통 helper는 root의 `tests/` 한 곳에서 관리하고 unit/integration/load 구분은
pytest marker로 표현합니다. `uv run pytest`는 기본적으로 단위 테스트를 실행합니다. 실행 중인
로컬 PostgreSQL을 사용하는 통합 테스트는 `uv run pytest -m integration`으로 별도 실행합니다. 신규 source 등록 절차는
[docs/source-onboarding.md](docs/source-onboarding.md)를 참고합니다.
초기 budget의 service load 검증은 `uv run pytest -m 'load and not mcp_server' -s`로,
실행 중인 Compose MCP의 전체 contract·병렬·비용 경계는
`uv run pytest -m 'mcp_server and not soak' -s`로 실행합니다. MCP server test는 안전을 위해
credential이 없는 loopback `http://` URL만 허용하고 `.env` token을 출력하지 않습니다.
두 replica의 1,000-session resource soak는 일반 개발/PR 경로와 분리해 실행합니다.

```bash
docker compose --profile soak build query-man
docker compose --profile soak up -d --no-build --wait query-man query-man-replica
uv run pytest -m soak -s
```

현재 fixture connection budget은 두 replica까지만 보장합니다. 종료할 때는
`docker compose --profile soak down`을 사용합니다.
전체 golden question의 revision, relation, SQL과 결과 invariant는
[`docs/verified-queries.md`](docs/verified-queries.md)의 계약에 따라
`uv run query-man-verify`로 검증합니다.

Production metadata revision을 재시작 후에도 유지하려면 database owner/관리자용 표준 libpq
환경에서 `scripts/apply-control-schema.sh`를 실행하고, 별도로 생성한 최소 권한 LOGIN에
`query_man_control_writer` membership을 부여합니다. `scripts/apply-db.sh`는 네 fixture
database·role·seed를 만드는 local/CI bootstrap이며 production migration이 아닙니다. Runtime은
전용 LOGIN의 TLS DSN을 `QUERY_MAN_CONTROL_DSN`에 사용합니다. 자세한 설치·복구 순서는
[source onboarding](docs/source-onboarding.md)과
[disaster recovery](docs/disaster-recovery.md)를 따릅니다.

Retrieval 품질은 [`config/quality-evaluation.yaml`](config/quality-evaluation.yaml)의
versioned case와 gate로 관리합니다. `uv run query-man-evaluate`는 golden/paraphrase relation
accuracy, unsupported/clarification recall과 context byte 상한 중 하나라도 실패하면 non-zero로
종료합니다.

각 source manifest의 `minimum_quality_level`은 L0/L1/L2 publish gate입니다. 현재 두 MVP
source는 L2이며, metadata revision과 일치하는 verified contract가 없으면 `/meta`, MCP와
query 경로가 새 revision을 활성화하지 않습니다.

`QUERY_MAN_CONTROL_DSN`과 `QUERY_MAN_SOURCE_ENCRYPTION_KEY`를 함께 설정하면 operator 전용
source admin API가 활성화됩니다. 신규 manifest는 격리 staging을 통과한 뒤 encrypted
credential, immutable metadata와 함께 원자적으로 publish되며 runtime과 다른 replica가
재시작 없이 반영합니다. 자세한 절차는
[`docs/source-onboarding.md`](docs/source-onboarding.md)를 따릅니다.
