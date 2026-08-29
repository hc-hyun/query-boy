# Query Man

Query Man은 AI나 애플리케이션이 PostgreSQL에 직접 접속하지 않고, **승인된 데이터만 안전하게
읽도록 중간에서 검사하고 제한하는 서버**입니다.

Query Man 자체에 자연어를 SQL로 바꾸는 AI 모델이 들어 있는 것은 아닙니다. 대신 질문에 필요한
데이터 설명을 제공하고, 클라이언트나 AI가 만든 SQL이 안전한지 확인한 뒤 읽기 전용으로 실행합니다.

> 현재 상태: 첫 오픈용 코드와 저장소 검증은 완료됐습니다. 실제 운영 환경의 암호화 통신(TLS), 비밀값,
> 백업, 배포와 트래픽 전환은 아직 남아 있습니다.

찾는 내용이 정해져 있다면 [목적별 문서 안내](docs/README.md#하고-싶은-일로-찾기)에서 바로
출발하세요. 모든 문서를 순서대로 읽을 필요는 없습니다.

## 먼저 알아둘 용어

전부 외울 필요는 없습니다. 뒤에서 낯선 단어가 나오면 이 표에서 뜻만 확인하면 됩니다.

| 용어 | 뜻 |
|---|---|
| Source | Query Man에 조회 대상으로 등록된 PostgreSQL 데이터베이스 하나입니다. |
| Source ID | DB 주소나 비밀번호 대신 클라이언트가 사용하는 공개 이름입니다. |
| Metadata / context | 사용할 수 있는 view(검토된 조회 화면)·column(항목), 한 행의 의미, 데이터 연결 규칙과 업무 용어를 설명하는 정보입니다. |
| Metadata revision | View 구조·설명, 업무 의미와 사용량 설정이 어느 버전인지 나타내는 변경 지문입니다. 일반 업무 데이터 행의 추가·수정을 뜻하지는 않습니다. |
| SQL policy revision | 허용 SQL 문법과 함수·결과 타입 등 안전 규칙이 어느 버전인지 나타내는 변경 지문입니다. |
| Reader | 승인된 view를 읽기만 할 수 있는 최소 권한 DB 계정입니다. |
| MCP | AI 도구가 source 목록, context와 query 기능을 호출하는 통신 방식입니다. |
| RLS | PostgreSQL이 사용자나 tenant별로 볼 수 있는 행을 제한하는 기능입니다. 현재 첫 오픈에서는 지원하지 않습니다. |
| Replica | 실행 중인 Query Man 서버 인스턴스 하나입니다. 첫 오픈 계획과 검증 범위는 하나입니다. |
| Git-reviewed YAML | `config/sources/*.yaml`을 review·test한 뒤 배포물이 시작할 때 읽는 방식입니다. |
| Fixture | 로컬·CI 테스트를 위해 만든 DB와 데이터입니다. 실제 운영 DB와 구분합니다. |

이 밖의 `grain`, `revision`, `OID` 같은 말은
[전체 용어 사전](docs/glossary.md)에서 쉽게 풀어 설명합니다.

## 어떻게 동작하나요?

```text
사용자 질문
  → Query Man이 관련 view, column과 업무 규칙을 설명
  → 클라이언트 또는 AI가 읽기 전용 SQL 작성
  → Query Man이 SQL과 권한을 검사하고 실행 시간·결과 크기 제한을 강제
  → 승인된 PostgreSQL source에서 실행
  → 제한된 결과만 반환
```

한 번의 조회는 source 하나만 사용합니다. 두 데이터베이스를 하나의 SQL로 join하지 않습니다.
클라이언트는 DB 주소, 비밀번호, database 이름이나 role을 선택할 수 없고 공개된 `source_id`만
전달합니다.

## 지금 제공하는 범위

| 항목 | 현재 범위 | 쉽게 말하면 |
|---|---|---|
| 데이터 | `development-issues`, `market-voc` | 검토가 끝난 두 업무 DB만 조회합니다. |
| 실행 구성 | 단일 Query Man replica | Query Man 서버 한 개를 첫 오픈 계획·검증 대상으로 봅니다. |
| PostgreSQL | 18.x, server/client UTF-8 | 다른 major version이나 문자 인코딩은 시작·조회 전에 거부합니다. |
| DB 접근 | `ai` schema의 검토된 view, 읽기 전용 계정 | 원본 table이나 쓰기 SQL에 접근하지 않습니다. |
| RLS | 모든 RLS source 차단 | 행 단위 권한을 사용하는 DB는 이번 첫 오픈에서 제공하지 않습니다. |
| 결과 column | 정수 3종, text, date, timezone timestamp, numeric | 그 밖의 결과 타입은 결과 행을 가져오기 전에 거부합니다. |
| Source 설정 | Git-reviewed source·verified-query·budget YAML | `config/sources/*.yaml`, `config/verified-queries.yaml`, `config/budget-profiles.yaml`이 authority입니다. 변경에는 review·test·재배포가 필요합니다. |

Boolean은 SQL의 조건식이나 중간 계산에는 사용할 수 있지만 최종 결과 column으로 반환할 수
없습니다. 내부적으로 허용하는 정확한 PostgreSQL type 번호와 정책은 ADR 0025에 기록돼 있습니다.

다음 기능은 **현재 첫 오픈 범위에는 포함되지 않습니다.**

- 실행 중 신규 DB를 바로 추가하는 hot reload 운영
- RLS source 제공
- 두 번째 replica, 장애 시 자동 전환(failover)과 고가용성(HA)
- 임의의 PostgreSQL 결과 타입
- DB 사용량을 source별 비용·금액으로 귀속하는 기능과 여러 요청을 잇는 분산 추적

정확한 현재 기준은 [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md)의
`LAUNCH-01-A`입니다. 완료된 검증은
[첫 오픈 증적](docs/verification/2026-08-26-static-first-launch.md), 운영까지 남은 일은
[개발 TODO](docs/development-todo.md)에서 확인할 수 있습니다.
Source authority의 현재 결정은 [ADR 0030](docs/decisions/0030-git-reviewed-yaml-source-authority.md)입니다.

## 제공 데이터

| Source ID | 담고 있는 데이터 | 질문 예시 |
|---|---|---|
| `development-issues` | 개발 문제, 원인, 대책, 시험기와 댓글 | 전체 개발 문제는 몇 건인가? |
| `market-voc` | 시장 VOC, 제품·기기, 처리 이력과 댓글 | VOC가 한 번도 없는 기기는 몇 대인가? |

각 source는 문제·댓글·기기처럼 “한 행이 무엇을 뜻하는지”가 다른 view를 분리해 제공합니다. 예를
들어 문제와 댓글을 무조건 합치면 댓글 수만큼 문제가 중복되어 잘못 집계될 수 있습니다. 실제 view와
예제 데이터는 [MVP 데이터 안내](docs/mvp.md)에 그림과 함께 설명돼 있습니다.

## 5분 로컬 실행

### 준비물

- Docker와 Docker Compose
- `openssl`과 `curl`
- Bash 호환 shell(Linux, macOS 또는 WSL)
- 기본 port `5432`, `3000`을 사용할 수 있는 로컬 환경

아래 명령은 모두 저장소 최상위 폴더에서 실행합니다.

Python 코드를 직접 개발할 때만 Python 3.12 이상과 `uv`가 추가로 필요합니다. Container는 고정된
Python 3.14 image를 사용합니다.

### 1. 로컬 설정 만들기

```bash
test -f .env || cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
```

두 난수 결과를 각각 `.env`의 `QUERY_MAN_CODEX_MCP_TOKEN`과 `QUERY_MAN_OPERATOR_TOKEN`에 넣습니다.
두 token은 서로 달라야 합니다. 기본 Compose가 사용하는 PostgreSQL과 current 두 reader의
`replace-with-...` 값도 각각 로컬 전용 password로 바꿉니다.

- `.env`는 Git에서 제외됩니다. commit하지 마세요.
- `.env.example`은 로컬 Compose용 예시일 뿐 운영 비밀값 관리 방법이 아닙니다.
- 기본 Compose는 loopback에만 port를 열며 TLS를 제공하지 않습니다.

### 2. PostgreSQL과 Query Man 시작하기

```bash
docker compose up -d --wait postgres
./scripts/apply-db.sh
docker compose up -d --build --wait query-man
docker compose ps
```

`apply-db.sh`는 현재 static launch와 같은 `development-issues`, `market-voc` 두 fixture DB의
role·schema·예제 데이터만 적용하고 검증합니다.
어느 스크립트도 운영 DB migration 도구가 아닙니다.

### 3. 정상 동작 확인하기

```bash
curl -fsS http://127.0.0.1:3000/ready
./scripts/verify-container.sh
```

첫 명령의 결과가 정확히 다음과 같아야 합니다.

```json
{"status":"ready"}
```

`verify-container.sh`는 `.env`와 실제 Compose port를 사용해 source 두 개, 인증, HTTP/MCP query,
non-root와 read-only container 경계까지 확인합니다.

로그는 다음 명령으로 확인합니다.

```bash
docker compose logs -f query-man
```

상태·로그·동의 기반 상세 진단을 안내와 Tab 자동완성이 있는 대화형 화면에서 보려면 repository root에서
다음을 실행합니다. 빈 줄이나 `help`를 입력하면 예시가 나오며, `diag`는 민감 정보를 표시하기 전에
조회 사유와 확인 문구를 요구합니다.

```bash
uv run qm
```

기본 Compose에는 query token과 별도의 `QUERY_MAN_OPERATOR_TOKEN`이 필요합니다. `.env.example`에서 새
random token으로 바꾸고 application image를 다시 빌드해야 container의 access policy에도 반영됩니다.
`qm source list/show/validate`는 현재 checkout의 source·verified-query·budget YAML을 조회·검증하는
local read-only 명령입니다. Source 변경은 pull request와 배포로만 반영합니다.

AuthBridge를 쓰는 배포는 opaque token 대신 [Resource Server JWT Access Token 검증 계약](docs/resource-server-jwt-auth.md)을
선택할 수 있습니다. 이때 Query Man은 JWT access token을 로컬 검증하며 client secret이나 refresh token을
보관하지 않습니다. 실제 audience/scope 발급과 protected route 전환은 별도 실행 승인 대상입니다.

## HTTP로 한 번 조회해 보기

아래 예시는 기본 port `3000` 기준이며 Query Man container가 실행 중이어야 합니다. `.env`에서
`QUERY_MAN_PORT`를 바꿨다면 URL의 port도 같은 값으로 바꾸세요.

`.env`는 Compose가 읽지만 현재 shell에는 자동으로 들어오지 않습니다. 아래 명령은 조회 전용 token
하나만 현재 shell에 가져옵니다. 이 caller는 두 source를 모두 조회할 수 있지만 관리·취소 권한은
없습니다.

```bash
export QUERY_MAN_CODEX_MCP_TOKEN="$(sed -n 's/^QUERY_MAN_CODEX_MCP_TOKEN=//p' .env)"
```

### 1. Source 목록 확인

```bash
curl -sS http://127.0.0.1:3000/sources \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN"
```

응답의 source 목록에 `development-issues`, `market-voc`만 있어야 합니다.

### 2. 질문에 필요한 데이터 설명 받기

```bash
curl -sS http://127.0.0.1:3000/meta \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "question": "전체 VOC는 몇 건인가?"
  }'
```

응답의 `metadata_revision`과 `sql_policy_revision`을 복사합니다. 이 두 값은 “어떤 데이터 설명과 안전
규칙을 보고 SQL을 만들었는지” 확인하는 영수증과 같습니다. 값을 임의로 바꾸면 안 됩니다.

`answerability`가 `needs_clarification` 또는 `unsupported`라면 SQL을 억지로 만들지 말고 질문을
명확히 하거나 지원 범위를 확인해야 합니다.

### 3. 같은 revision으로 읽기 전용 SQL 실행

```bash
curl -sS http://127.0.0.1:3000/query \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "sql": "SELECT count(*) AS voc_count FROM ai.voc_overview",
    "metadata_revision": "<meta 응답의 전체 값을 그대로 붙여넣기>",
    "sql_policy_revision": "<meta 응답의 전체 값을 그대로 붙여넣기>"
  }'
```

로컬 예제 데이터가 정상이라면 `rows`는 `[{"voc_count":1200}]`, `row_count`는 `1`입니다.

MCP endpoint는 같은 인증과 실행 경계를 사용하며 `list_sources`, `get_context`, `query` 세 tool만
제공합니다. AI workflow는 [Text-to-SQL Skill](skills/query-man-text-to-sql/SKILL.md)을 참고하세요.
Codex/MCP client 설정과 protocol version은 빠르게 바뀔 수 있으므로
[운영 문서의 현재 절차](docs/operations.md)를 따릅니다.

## 사용을 마쳤다면 종료하기

```bash
docker compose down
```

이 명령은 container를 내리지만 PostgreSQL 데이터 volume은 보존합니다.

> `docker compose down -v`는 volume과 로컬 DB 데이터를 삭제합니다. 데이터를 초기화하려는 것이
> 확실할 때만 사용하세요.

## Query Man이 지키는 안전장치

- 클라이언트가 DB host, DSN, database, role이나 비밀번호를 지정할 수 없습니다.
- 기본 Compose에서는 bearer token으로 인증된 조회 caller만 source, metadata와 query API를 사용할 수
  있습니다.
- AuthBridge mode에서는 서명, 고정 algorithm, issuer, audience, 만료와 endpoint scope/role/group을
  검증하고 ID/refresh token을 거부합니다.
- Reader는 각 source의 `ai` view만 읽을 수 있고 원본 schema에는 접근하지 못합니다.
- SQL의 문법 구조(AST)를 검사해 한 개의 허용된 읽기 전용 `SELECT`만 실행합니다.
- PostgreSQL도 read-only transaction, timeout과 최소 권한으로 다시 제한합니다.
- Source별 동시 실행 수, 반환 row 수와 UTF-8 byte 수를 제한합니다.
- Metadata나 SQL 정책이 바뀌면 예전 revision을 실행 전에 거부합니다.
- Client 연결이 끊기면 실행 중인 PostgreSQL query를 취소하고 rollback합니다.
- 인증 token, DB 접속 정보, SQL 안의 실제 값과 내부 DB 오류를 외부 오류에 노출하지 않습니다.
- 현재는 RLS source를 예외 없이 거부합니다.

기본 budget과 변경 절차는 [query 비용·제한 안내](docs/query-cost-control.md)에 있습니다.

## 모듈을 쉽게 이해하기

Query Man은 한 프로그램으로 배포하지만 내부 책임을 여섯 module로 나눈 구조, 즉 modular
monolith입니다.
여섯 책임은 `src/query_man` 아래 `source_catalog`, `metadata`, `guarded_query`,
`delivery`, `runtime`, `assurance` package로 나뉩니다.
이 구분은 별도 repository나 service가 아닙니다. 모두 같은 wheel과 하나의 Query Man process로
배포되며, package `__init__.py`는 re-export 없는 marker라 필요한 leaf module을 직접 import합니다.

| Module | 비유 | 맡은 일 |
|---|---|---|
| Source Catalog | 주소록 | 어떤 source를 어떤 reader·budget·업무 설명으로 사용할지 관리합니다. |
| Metadata | 지도 제작자 | PostgreSQL 구조를 읽어 revision이 붙은 데이터 지도와 질문별 context를 만듭니다. |
| Guarded Query | 보안 검색대 | SQL을 검사하고 제한된 read-only transaction으로 실행·취소·rollback합니다. |
| Delivery | 현관 | Caller를 인증하고 같은 기능을 HTTP와 MCP로 제공합니다. |
| Runtime | 조립·운영 담당 | 다른 module을 연결하고 설정, 시작·종료, health와 container 실행을 관리합니다. |
| Assurance | 검사소 | 품질 질문, verified query, 통합·container 테스트로 전체 흐름을 검증합니다. |

Module 하나를 수정할 때는 repository 전체를 먼저 읽지 말고
[module index](docs/modules/README.md)에서 담당 module, 읽을 코드·테스트와 사용 가능한 interface를
확인합니다. 다른 module이 사용하는 interface 의미를 바꿔야 한다면 변경 내용과 영향을 사용자에게
먼저 설명하고 승인을 받아야 합니다.

여기서 module interface는 다른 내부 module에 공개한 Python 상수·type·Protocol·함수·method와
시작·종료 같은 lifecycle capability를 포함하는 내부 연결점입니다. HTTP/MCP 같은 외부 API와는
별도 경계입니다.

## 개발과 테스트

Host에서 개발하려면 PostgreSQL fixture만 실행하고 Python 환경을 준비합니다.

```bash
docker compose stop query-man
docker compose up -d --wait postgres
uv sync --locked
```

일상적인 repository gate는 다음 세 명령입니다.

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

기본 두 source의 PostgreSQL과 결과 기준은 CI의 `core-static` DB selector를 따라 확인하고 다음
두 acceptance를 실행합니다.

```bash
uv run query-man-evaluate
uv run query-man-verify
```

전체 integration 경계는 기본 두 source fixture를 준비한 뒤 같은 repository gate의 integration
marker로 확인합니다.

```bash
uv run pytest -m integration
```

실행 중인 Compose container와 MCP 경계는 다음 명령으로 확인합니다.

```bash
./scripts/verify-container.sh
uv run pytest -m 'mcp_server and not soak' -s
```

부하, 두-replica soak과 보안 update 절차는 일반 개발 흐름과 분리돼 있습니다.
필요할 때 [운영 문서](docs/operations.md)와 [CI workflow](.github/workflows/ci.yml)를 따라 실행하세요.

## 운영까지 남은 일

저장소 구현, local container 검증과 CI(자동 검증)는 완료됐습니다. 하지만 이것은 특정 운영 환경에
실제로 배포했다는 뜻이 아닙니다.

남은 `LAUNCH-02`는 대상 환경별 작업입니다.

1. 운영 서버, 접근 권한과 운영 변경 기록 담당자를 정합니다.
2. 암호화 통신, 인증 비밀값, DB 접속 정보, 백업과 이전 버전 복구 절차를 준비합니다.
3. 실제 DB 구조·읽기 권한·PostgreSQL 설정과 RLS 0건을 확인하고, 배포 image의 내용 지문을 기록합니다.
4. 실제 요청을 연결하기 전에 `/ready`와 승인 SQL 9개를 확인한 뒤, 요청을 연결하고 오류·DB 연결을 관찰합니다.

실행 순서와 중단 조건은 [운영 runbook](docs/operations.md)의 “Static Non-RLS First Launch”를
따릅니다. 로컬 fixture 성공을 실제 운영 증거로 대신하지 않습니다.

## 새 데이터베이스를 추가하려면

현재 첫 오픈은 두 source만 승인했습니다. 아래
[source onboarding과 extension checklist](docs/source-extension-checklist.md#이-문서는-언제-사용하나요)에서
추가하려는 DB가 현재 static 경로에 맞는지와 end-to-end 영향을 함께 확인합니다. 새 DB에는 PostgreSQL
18/UTF-8, RLS를 사용하지 않는 검토된 view와 읽기 전용 계정이 필요합니다. 업무 의미·사용량 제한·결과
타입을 검토하고 품질 질문과 승인 SQL을 통과한 뒤, 변경 승인을 받아 다시 배포합니다.

현재 module interface 안에서 처리할 수 있다면 source별 Python 분기를 추가하지 않습니다. Source
추가·변경은 `config/sources/*.yaml`과 관련 verified-query/budget YAML을 같은 review에서
검증한 뒤 재배포합니다.

PostgreSQL table·column comment는 grain, 단위, 상태값과 주의사항을 설명하는
human-readable metadata로 활용합니다. Type과 numeric precision/scale은 catalog에서 수집하고,
PII 표시는 comment만 믿지 않고 curated view·reader grant·policy로 강제합니다.

## 문서 읽는 순서

전체 문서를 한 번에 읽지 마세요. [문서 안내](docs/README.md)가 목적에 맞는 다음 문서를 골라줍니다.

| 알고 싶은 내용 | 문서 |
|---|---|
| 낯선 용어와 문서 찾기 | [문서 안내](docs/README.md), [용어 사전](docs/glossary.md) |
| 현재 제공 데이터와 예제 | [MVP 데이터 안내](docs/mvp.md) |
| 전체 구조와 module 작업 범위 | [Architecture](docs/architecture.md), [Module index](docs/modules/README.md) |
| 첫 오픈 결정과 정확한 제한 | [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md) |
| Source·verified-query·budget authority | [ADR 0030](docs/decisions/0030-git-reviewed-yaml-source-authority.md) |
| AuthBridge API 인증 연동 | [Resource Server JWT Access Token 검증 계약](docs/resource-server-jwt-auth.md), [ADR 0029](docs/decisions/0029-authbridge-resource-server-jwt.md) |
| 운영 배포·rollback·관측 절차 | [Operations](docs/operations.md) |
| 남은 일과 완료 이력 | [Active TODO](docs/development-todo.md), [Implementation roadmap](docs/implementation-roadmap.md) |
| 실행 시점별 검증 기록 | [Verification evidence](docs/verification/README.md) |
| 일정에 없는 후속 연구 | [Future work](docs/future-work.md), [ADR index](docs/decisions/README.md) |

과거 verification 문서는 그 문서에 적힌 commit·환경·범위만 증명합니다. 현재 상태는 이 README,
accepted ADR, active TODO와 최신 runnable test를 함께 확인하세요.
