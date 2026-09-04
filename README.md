# Query Man

Query Man은 AI가 만든 SQL을 그대로 데이터베이스에 전달하지 않고, 승인된 PostgreSQL view만 제한된
reader로 조회하게 하는 안전한 metadata gateway입니다. AI 모델이나 데이터베이스 관리 기능은
포함하지 않습니다.

현재 문서의 시작점은 [독자별 문서 안내](docs/README.md#독자별-시작점)입니다.

## 핵심 흐름

HTTP client는 다음 세 endpoint를 순서대로 사용합니다.

1. `GET /sources`로 호출자가 사용할 수 있는 source를 확인합니다.
2. `POST /meta`에 `source_id`를 보내 현재 relation·column과 두 revision을 받습니다.
3. `POST /query`에 SQL과 같은 두 revision을 보내 한 source의 읽기 전용 SQL을 실행합니다.

`get_context`는 PostgreSQL에서 직접 admission한 전체 curated-view catalog를 hard limit 안에서
결정적으로 반환합니다. Runtime은 자연어 질문의 관련도를 추측하거나 여러 데이터베이스를 federation하지
않습니다.

Application 상태는 `/health`, `/ready`, operator용 상세 상태와 process-local 지표는
`/admin/health`, `/admin/metrics`로 제공합니다.

## 안전 경계

- Source는 `config/sources/<source-id>/source.yaml`과 `views.sql` 두 파일로 review합니다.
- Process 시작 때 모든 package를 load하며 별도 source 등록 목록은 없습니다.
- RLS source, 허용하지 않은 schema·relation kind와 reader 권한은 DB query 전에 거부합니다.
- SQL은 PostgreSQL AST로 검사하고 relation·function·operator·cast를 allowlist로 제한합니다.
- Query는 최소 권한 reader의 `REPEATABLE READ READ ONLY` transaction에서 실행합니다.
- Timeout, concurrency, plan, row와 byte 상한을 PostgreSQL과 gateway가 함께 강제합니다.
- Timeout, client disconnect와 shutdown은 query cancel·rollback·connection cleanup을 수행합니다.
- Password, bearer token, SQL literal과 내부 database 오류는 응답이나 일반 log에 노출하지 않습니다.
- Metadata와 SQL policy revision이 달라지면 fail-closed하고 새 context를 요구합니다.

정확한 launch 제한은 [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md), source package 계약은
[ADR 0034](docs/decisions/0034-source-view-package-and-direct-admission.md), startup inventory는
[ADR 0035](docs/decisions/0035-reviewed-source-package-inventory.md)를 따릅니다.

## 로컬 실행

필요한 것은 Docker Compose와 각 example source의 reader password입니다.

```bash
cp .env.example .env
```

`.env`에서 다음 값을 실제 로컬 값으로 바꿉니다.

```dotenv
QUERY_MAN_POSTGRES_HOST=127.0.0.1
DEVELOPMENT_ISSUES_READER_PASSWORD=...
MARKET_VOC_READER_PASSWORD=...
QUERY_MAN_QUERY_TOKEN=...
QUERY_MAN_OPERATOR_TOKEN=...
```

Password와 token은 Git에 commit하지 않습니다. Source database가 따로 준비되어 있지 않다면 작은 CI용
PostgreSQL fixture로 실제 DB와 container 경계를 검증할 수 있습니다.

```bash
./scripts/verify-database.sh
./scripts/verify-container.sh
```

두 명령은 `query-man-fixture` project에 synthetic DB를 새로 만들며, 성공·실패와 관계없이 종료할 때
container와 fixture volume을 삭제합니다. 별도 `.env` 복사나 수동 정리가 필요하지 않습니다.

실제 source를 연결한 local API는 다음처럼 실행합니다.

```bash
docker compose --env-file .env up --build -d
curl -fsS http://127.0.0.1:3000/ready
```

Compose의 access-policy 설정에서는 `QUERY_MAN_QUERY_TOKEN`을 bearer token으로 사용합니다.

```bash
curl -fsS http://127.0.0.1:3000/sources \
  -H "Authorization: Bearer $QUERY_MAN_QUERY_TOKEN"

curl -fsS http://127.0.0.1:3000/meta \
  -H "Authorization: Bearer $QUERY_MAN_QUERY_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"source_id":"development-issues"}'
```

`/meta`가 반환한 exact `metadata_revision`과 `sql_policy_revision`을 `/query` 요청에 그대로 전달합니다.

사용을 마치면 다음과 같이 종료합니다.

```bash
docker compose --env-file .env down
```

Fixture는 개발·CI 재현용이며 production DB apply나 운영 증거가 아닙니다. 자동 삭제되는 volume에는
synthetic test data만 두어야 합니다.

## 인증

- Loopback bind에서는 인증 설정이 없을 때 local anonymous query를 허용합니다.
- 단일 consumer는 `QUERY_MAN_API_TOKEN`을 사용할 수 있습니다.
- 여러 caller와 operator 권한은 `QUERY_MAN_ACCESS_POLICY_FILE`의 opaque token policy를 사용합니다.
- Non-loopback bind는 API token 또는 access-policy가 없으면 시작을 거부합니다.

모든 mode에서 token 원문은 log에 남기지 않으며 query caller가 operator endpoint를 호출할 수 없습니다.

## 새 source 추가

새 PostgreSQL source의 repository 변경은 다음 두 파일만 추가합니다.

```text
config/sources/<source-id>/
├── source.yaml
└── views.sql
```

`source.yaml`에는 secret 값이 아니라 password environment key, view-only allowlist, budget과 provenance를
둡니다. `views.sql`은 explicit output column, source/version marker, dedicated owner와 exact reader grant를
가진 desired artifact입니다. Runtime은 이 SQL을 실행하지 않습니다.

DB/data owner 검토와 DBA apply는 repository 변경과 별도이며 traffic 밖에서 승인받아 수행합니다. 자세한
stop·rollback 조건은 [Source extension checklist](docs/source-extension-checklist.md)를 따릅니다.

## 개발

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
```

작업 전 [활성 개발 지침](docs/development-guidelines.md)과
[Module index](docs/modules/README.md)에서 primary module의 관련 범위만 읽습니다. SQL parser,
allowlist, reader, cancel·rollback과 secret redaction 테스트는 단순화를 이유로 줄이지 않습니다.

현재 protected-environment 작업은 [Active TODO](docs/development-todo.md), 운영 절차는
[Operations](docs/operations.md), 현재 gate와 삭제한 기록을 찾는 법은
[Verification and Git history](docs/verification/README.md)에 있습니다.
