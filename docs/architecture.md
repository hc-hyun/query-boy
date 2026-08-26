# Query Man Architecture

Status: ADR 0025 static non-RLS first-launch profile; protected execution pending `LAUNCH-02`

## Goal

장기 구조는 여러 PostgreSQL database를 하나의 Text-to-SQL gateway와 MCP endpoint로 제공하고,
필요할 때 중앙 management surface로 source lifecycle을 운영하는 modular monolith다.

현재 serving 기준선은 [ADR 0025](decisions/0025-static-non-rls-first-launch.md)의
`LAUNCH-01-A`다. Repository가 검토한 `development-issues`, `market-voc` 두 non-RLS source만
static bootstrap authority와 단일 replica로 제공한다. PostgreSQL 18/UTF-8, exact seven result
OID와 SQL policy v3를 강제한다. 모든 RLS source, 신규 database, managed hot onboarding, HA와
broader result type은 첫 launch 밖이다. 구현된 Control Plane과 managed lifecycle은 보존하지만
별도 운영 결정 전에는 launch composition에 참여하지 않는다. 실제 protected environment 전환은
`LAUNCH-02`이며 repository acceptance와 같은 뜻이 아니다.

완전한 무설정 자동화를 목표로 하지 않는다. 자동으로 알 수 없는 비즈니스 의미는
희소한 선언형 metadata와 curated view로 제공한다.

## Design Principles

1. Runtime safety와 business semantics를 분리한다.
2. 데이터베이스별 차이는 코드가 아니라 versioned data로 관리한다.
3. Planner cost 예측보다 실행 피해의 상한을 고정한다.
4. Skill은 workflow를 설명하고 MCP/runtime은 정책을 강제한다.
5. 전체 schema dump 대신 질문에 필요한 context만 점진적으로 제공한다.

## System Shape

```text
Text-to-SQL Skill
        |
Single MCP Server / HTTP API
  |-- list_sources
  |-- get_context
  `-- query
        |
Query Gateway + Source Registry <--- validated hot reload --- Control Plane
  |-- Physical Catalog                                  |-- Source Catalog/Generations
  |-- Semantic Overlay                                  |-- Active State/History
  |-- Resource Tier (budget_profile)                    |-- Metadata/Verified Queries
  |                                                     |-- Mutation Receipts/Audit
    `-- guarded connection                                |-- Replica Observations
                                                          `-- Measurements (target)
        |
PostgreSQL Reader / Analytics Replica
```

Static launch는 `development_issues`와 `market_voc` 독립 source database를 사용한다.
`support_tickets`, `commerce_edges`는 과거 Control Plane/onboarding/integration acceptance fixture이며
serving inventory가 아니다. 구체적인 grain, seed와 검증 범위는 [mvp.md](mvp.md)에 기록한다.

Local Compose는 PostgreSQL과 단일 `query-man` application container를 실행한다. 이 한
process가 HTTP와 `/mcp`를 함께 제공해 registry, metadata cache, authorization과 query
admission을 공유한다. Container network의 PostgreSQL service name은 manifest의 선택적
`host_env`로 resolve하되 control plane에는 resolved endpoint만 저장한다. Host publish는
loopback으로 제한하고 container 내부 non-loopback bind에는 query-only bearer policy를
강제한다. Image, secret, readiness와 shutdown 경계는
[ADR 0015](decisions/0015-containerized-local-runtime.md)를 따르며 실제 container acceptance는
[container runtime audit](verification/2026-08-23-container-runtime.md)에 기록한다. 전체
MCP external API와 병렬·포화·취소·비노출 경계의 실제 server 검증은
[MCP server assurance](verification/2026-08-23-mcp-server-assurance.md)에, 두 replica session
내구성과 resource 경계는
[multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md)에 기록한다.
Managed replica의 실제 convergence 관측 증거는
[runtime replica observation audit](verification/2026-08-25-runtime-replica-observations.md)에 기록한다.
Resource/gateway observation의 실행 증거는
[resource and gateway observation audit](verification/2026-08-25-resource-and-gateway-observations.md)에
기록하고, latest attempt와 operator usage projection의 실행 증거는
[usage projection audit](verification/2026-08-25-usage-projection.md)에 기록한다.
서로 다른 격리 PostgreSQL service 사이의 minor-version archive, key recovery, zero-bootstrap와
두 managed replica 복구 증거는
[control recovery acceptance](verification/2026-08-25-control-recovery-acceptance.md)에 기록한다.
물리적으로 다른 production host/network, source business DB와 실제 RPO/RTO는 이 fixture의 증명
범위가 아니다.

## Development Module Boundaries

배포 형태는 하나의 process지만 개발 소유권은 Source Catalog, Metadata, Guarded Query,
Control Plane, Delivery, Runtime과 Assurance의 논리 module로 나눈다. 이 경계는 microservice
분리를 뜻하지 않으며, AI agent가 repository 전체를 선행 학습하지 않고 담당 module의 역할,
직접 소비 interface, 실행 흐름과 테스트에 집중해 병렬 작업하기 위한 modular monolith 경계다.

현재 `src/query_man`은 평면 구조이므로 owner는 package 경로가 아니라
[module boundary index](modules/README.md)의 transition map으로 결정한다. 새 의존은 owner가 공개한
module interface로만 연결한다. Runtime은 production implementation을, Assurance CLI는 offline 검증
implementation을 조립하며 Control Plane은 candidate source 검증을 위한 격리 staging만 조립한다.
Module interface와 external/persisted/policy/lifecycle 의미 변경은
[ADR 0018](decisions/0018-module-ownership-and-contract-governance.md)에 따라 구현 전에 사용자
승인을 받는다. 물리 package 이동은 이 interface와 별도 경계 baseline을 보존하는 별도 refactoring이다.

## Component Boundaries

### Physical Catalog

`pg_catalog`에서 relation, column, type, primary key, foreign key, index와 comment를 공통
형식으로 생성해 revision snapshot으로 발행한다. Planner 통계의 row estimate는 fresh catalog
응답에만 포함될 수 있는 best-effort hint이며 revision 재료나 persisted snapshot format이
아니다. Restart 복원 뒤 없을 수 있으므로 안전·정확성 판단에 사용하지 않는다.

운영 규모 관측은 metadata revision과 별도 lifecycle을 사용한다. Row/storage/growth 값은 측정
방법, definition revision, 시각과 freshness를 포함한 bounded observation으로 관리하고 source
generation을 만들지 않는다. 일반 view에 무제한 `COUNT(*)`를 실행하지 않는다.

### Semantic Overlay

물리 schema만으로 알 수 없는 정보만 선택적으로 관리한다.

- relation과 column의 description 및 alias
- 한 행이 무엇을 의미하는지 나타내는 grain
- 대표 시간 column
- 승인된 join key, cardinality와 fanout 위험
- business measure와 계산 규칙
- source별 상태 predicate, enum value hint와 답변 불가능 조건
- 여러 grain을 독립 집계해야 하는 검증된 composition
- 검증된 질문 및 SQL 예제

모든 table을 코드 객체로 다시 모델링하지 않는다. 복잡한 유효기간 join, 다대다
집계, 여러 fact 사이의 계산은 `ai` schema의 view 또는 materialized view로
캡슐화한다.

### Source Registry

각 source는 opaque `source_id`로 식별한다. DSN과 credential은 서버가 관리하며
모델이나 client가 임의의 DSN을 전달할 수 없다.

Source profile에는 다음 운영 설정만 둔다.

- credential reference와 허용 schema
- reader 또는 replica 요구 조건
- statement, lock 및 queue timeout
- work memory, temporary file, parallel worker, JIT와 동시 실행 수
- 결과 row와 byte 제한
- plan admission을 포함한 중앙 `budget_profile` resource tier

Runtime은 `QUERY_MAN_SOURCE_MODE=bootstrap|managed`로 process 전체 source authority를 시작할 때
한 번 선택한다. ADR 0025 static launch의 bootstrap mode는 `config/sources/*.yaml`과 filesystem
verified-query data를 읽고 Control DSN/key를 거부한다. Credential 값은 manifest에 저장하지 않고 환경 변수
이름만 참조한다.

별도로 활성화하는 managed mode는 Control DSN/key와 stable replica ID를 모두 요구하고 empty
registry/verified map에서 Control DB의 source/verified lifecycle state만 load한다. Source/verified file을 열거나 합치지 않으므로 lifecycle row가
없는 file source는 absent이고 restart 뒤 rollback/deactivate가 유지된다. Budget profile과 access
policy는 versioned deployment configuration에 남는다. Source publish가 repository YAML이나 commit을
만들지 않으며 양방향 sync, startup import와 file fallback도 없다. 일회성 admin-API cutover와
authority 규칙은 [ADR 0016](decisions/0016-centralized-source-management-plane.md)을 따른다.
Control-plane source revision과 암호화된 credential persisted/security format은
[ADR 0012](decisions/0012-control-plane-source-revisions.md)를 따른다.

초기 운영에서는 모든 인증된 query principal이 같은 active source 목록을 본다. Source
publish/deactivate가 visibility를 한 번에 바꾸므로 caller별 grant, import marker와 dynamic
allowlist를 만들지 않는다. Source마다 관리자가 기존 `budget_profile` 하나를 선택하고 모든
사용자는 같은 profile 정의를 공유한다. Version 2 access policy에는 source scope가 없으며
version 1과 legacy scope field는 자동 확대 없이 startup에서 거부한다. Managed mode는 explicit
query/admin identity가 있는 policy file을 요구하고 single-token/anonymous caller를 금지한다.
Bootstrap local/API-token identity는 query-only다.

### Source Management Plane

Public `/sources`는 query caller에게 active source만 반환하며 admin catalog가 아니다.
목표 management surface는 비밀이 제거된 source inventory, generation/history, ownership,
effective `budget_profile`, replica convergence, size/growth와 usage/cost projection을 하나의 관리자
HTTP API로 제공한다. 실제 DB 객체, secret, raw metric과 provider bill은 각 authority에 남을 수
있다. Control Plane은 같은 `source_id`와 provenance로 이를 모아 보여준다.

현재 management slice는 strict manifest v2의 owner/environment/DB migration reference를
immutable generation에 저장하고 admin-only list/detail/generation-history API로 조회한다. 여섯
mutation은 expected state, authenticated actor/change reference와 keyed canonical request hash를
사용하며 source/verified-query state 변경과 terminal receipt를 원자적으로 commit한다. 별도 operator-only
receipt lookup과 source mutation history가 timeout reconciliation과 lifecycle chronology를 제공한다.
API는 raw manifest, encrypted secret, question/SQL을 읽지 않는 explicit projection이며 published
generation revision과 현재 active metadata revision을 구분한다. 별도 replica endpoint는 managed
slot별 desired/applied generation·state·metadata drift와 DB-clock freshness를 bounded하게 제공한다.
Optional manifest target의 daily record/storage current/previous와 privacy-safe hourly gateway usage는
Control DB에 내부 수집한다. 전용 operator usage endpoint는 latest resource attempt/last-success,
global reporter health와 inclusive 31일 lower-bound rollup을 제공하고 provider monetary cost는
`not_configured`로 명시한다.

초기 management 권한은 query user와 Query Man admin 두 종류다. 기존 boolean operator를 admin
capability superset으로 재사용하고 역할 계층, caller grant와 별도 `cost_tier`를 만들지 않는다.
Query credential은 admin endpoint와 cancel에서 거부된다. 상세 management API 구현 순서는
[source management plane](source-management-plane.md)과 `CTRL-*`가 관리한다.

### Query Gateway

모든 source에 동일한 실행 순서를 적용한다.

```text
authorize
-> validate one read-only statement and object/function allowlist
-> acquire concurrency slot
-> begin read-only transaction
-> apply local limits
-> optionally reject an obviously risky EXPLAIN plan
-> stream results with row/byte limits
-> commit or cancel and rollback
```

Pool checkout 직후 SQL 없이 PostgreSQL 18, server/client UTF-8과 driver UTF-8 codec을 확인하고
불일치 connection은 버린다. RLS source는 metadata, queue와 database access 전에 details 없는
unavailable로 끝난다. User cursor의 duplicate column 검사 다음, 첫 fetch 전에 RowDescription의
final result OID가 `20, 21, 23, 25, 1082, 1184, 1700` 중 하나인지 검사한다. Boolean을 포함한
다른 final type은 unavailable이며 predicate와 중간 계산 사용은 유지한다.

`EXPLAIN total_cost`는 시간이나 금액이 아니므로 단독 안전 기준으로 사용하지
않는다. Timeout, concurrency, result size, temporary resource 제한과 query cancel을
실제 안전 경계로 사용한다.

## MCP External API

하나의 MCP endpoint에서 고정된 tool schema를 제공한다. 데이터베이스나 table마다
tool을 생성하지 않는다.

```text
list_sources()

get_context(source_id, question, max_objects=2)  # integer 1..4
  -> source metadata, question, metadata_revision, sql_policy_revision,
     snapshot_status, quality_level,
     sql_capabilities{functions, cast_types, unqualified_cast_types},
     answerability, relations[{columns, measures, grain, keys, indexes}], joins,
     business_terms, composition_hints, ambiguities, truncated

query(source_id, sql, metadata_revision, sql_policy_revision)
  -> success: status, query_id, metadata_revision, sql_policy_revision,
              fingerprint, columns, rows,
              row_count, result_bytes, truncated, queue_ms, elapsed_ms, plan_summary
  -> query failure: error.code, error.message,
                    error.details?{reason_code, rejected_construct?, action?, retryable?}
  -> invalid arguments: error.code=INVALID_REQUEST, error.message,
                        error.details{action, retryable, issues[{path, reason_code, message}],
                                      truncated}
```

Source가 사용자 session에 고정되는 환경에서는 `list_sources`를 생략할 수 있다.
하나의 query는 하나의 source만 대상으로 하며 cross-database federation은 별도
기능으로 취급한다.

MCP를 붙이기 전 동일한 application service 동작을 HTTP로 먼저 검증한다.

```text
GET  /sources
POST /meta { source_id, question, max_objects? }
POST /query { source_id, sql, metadata_revision, sql_policy_revision }
```

`/meta`는 reader가 실제로 조회할 수 있는 allowed schema/relation kind만 catalog에서
읽는다. DB comment는 길이와 제어 문자를 제한한 비신뢰 description data이며, join은
comment 문장을 파싱하지 않고 manifest에 승인된 edge만 반환한다. Schema, view
definition, comment, overlay, source execution budget 또는 revision-scoped source policy가
바뀌면 `metadata_revision`도 바뀐다. 이 revision에 묶인 L2 verified SQL은 현재 실행
경계에서 다시 통과해야 한다. Application 전역 SQL parser/function/operator/type 정책은
별도 `sql_policy_revision`으로 digest하며 query는 context에서 받은 두 revision이 모두 현재
값과 일치할 때만 실행한다. 이로써 서로 다른 release의 replica 사이 policy drift를
metadata snapshot을 다시 쓰지 않고 fail-closed한다.

## Onboarding Levels

- L0: source 등록과 자동 catalog. 단순 질의를 best-effort로 지원한다.
- L1: description, grain, 시간 column과 비표준 join을 보강한다.
- L2: L1 조건과 현재 metadata revision의 verified-query baseline을 충족한다. Measure와
  curated view는 source 의미에 필요할 때만 추가한다.

권장 onboarding 흐름은 다음과 같다.

```text
register source profile
-> introspect
-> merge and validate semantic overlay
-> build retrieval index
-> publish immutable metadata revision
```

Schema drift로 overlay가 깨지면 신규 revision 발행을 중단하고 마지막 정상 revision을
유지한다.

## Success Criteria

아래 항목은 장기 system 목표다. ADR 0025 first launch의 완료 조건은 accepted 두 source, 단일
replica와 해당 decision의 repository/protected gate로 더 좁다.

- 신규 PostgreSQL source 추가에 애플리케이션 코드 변경이 없다.
- Production source 추가가 repository source file이나 application deploy를 만들지 않는다.
- `source_id`에 따른 runtime 분기문이 추가되지 않는다.
- 보안과 비용 정책은 Skill이나 prompt가 아니라 gateway가 강제한다.
- 복잡한 DB만 필요한 만큼 semantic overlay 또는 curated view를 추가한다.
- 실제 질문과 SQL로 source별 품질을 회귀 검증할 수 있다.
- 운영자는 한 management surface에서 source의 owner, active/applied state, history, 규모와 비용
  freshness를 조회할 수 있다. 이 항목은 `CTRL-*` 완료 전까지 목표 상태다.
- 모든 query 사용자는 같은 active source 목록과 source별 `budget_profile` 정의를 사용하며
  query credential은 admin endpoint를 호출할 수 없다.

## Decisions

- PostgreSQL AST parser와 canonical query fingerprint는
  [ADR 0001](decisions/0001-postgresql-ast-validation.md)을 따른다.
- Guarded query 순서, revision, truncation과 오류 의미는
  [ADR 0002](decisions/0002-guarded-query-contract.md)를 따른다.
- Reader role, curated view와 PostgreSQL-resolved function/operator 정책은
  [ADR 0003](decisions/0003-reader-and-resolved-object-policy.md)를 따른다.
- Caller identity, tenant와 source authorization은
  [ADR 0004](decisions/0004-caller-source-authorization.md)를 따른다.
- 초기 hard limit, connection budget과 override 정책은
  [ADR 0005](decisions/0005-initial-query-budgets.md)를 따른다.
- MCP transport, 인증 경계와 Text-to-SQL workflow는
  [ADR 0006](decisions/0006-mcp-transport-and-workflow.md)을 따른다.
- Immutable metadata publish, active revision과 rollback pin은
  [ADR 0007](decisions/0007-immutable-metadata-publishing.md)을 따른다.
- Physical primary/foreign key와 index 공개 범위는
  [ADR 0008](decisions/0008-physical-key-and-index-disclosure.md)을 따른다.
- Wide relation의 question-scoped column disclosure는
  [ADR 0009](decisions/0009-question-scoped-column-disclosure.md)을 따른다.
- Revision-scoped token/IDF metadata retrieval index는
  [ADR 0010](decisions/0010-revision-scoped-retrieval-index.md)을 따른다.
- L0/L1/L2 metadata publish gate는
  [ADR 0011](decisions/0011-metadata-quality-level-publish-gate.md)을 따른다.
- Source revision, encrypted credential과 control-plane 상태 전이는
  [ADR 0012](decisions/0012-control-plane-source-revisions.md)를 따른다.
- No-deploy verified query publish는
  [ADR 0013](decisions/0013-control-plane-verified-query-publishing.md)을 따른다.
- RLS tenant session context와 pool reset은
  [ADR 0014](decisions/0014-trusted-rls-tenant-context.md)를 따른다.
- Local/CI container topology와 secret/readiness 경계는
  [ADR 0015](decisions/0015-containerized-local-runtime.md)를 따른다.
- Production source authority와 단일 admin management surface는
  [ADR 0016](decisions/0016-centralized-source-management-plane.md)을 따른다.
- Shared source access, admin 경계와 source별 공통 resource tier는
  [ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)을 따른다.
- Module owner, 집중 읽기 범위와 module interface/boundary 승인 절차는
  [ADR 0018](decisions/0018-module-ownership-and-contract-governance.md)을 따른다.
- Static non-RLS first-launch source, reader compatibility, result OID, artifact와 rollout 경계는
  [ADR 0025](decisions/0025-static-non-rls-first-launch.md)를 따른다.

## Completion Tracking

Production acceptance까지의 구현 순서와 완료 증거는
[implementation-roadmap.md](implementation-roadmap.md)와
[completion audit](verification/2026-08-23-completion-audit.md)에서 baseline으로 관리한다.
해당 시점의 refactoring baseline과 의도적인 운영 경계는
[refactoring assurance audit](verification/2026-08-23-refactoring-assurance.md)에 기록한다.
Managed source authority startup과 bootstrap cutover 증거는
[managed source startup audit](verification/2026-08-23-managed-source-startup.md)에 기록한다.
Version 2 shared visibility와 query/admin capability 증거는
[shared access audit](verification/2026-08-23-shared-access.md)에 기록한다.
Strict manifest provenance와 admin source catalog 증거는
[source management catalog audit](verification/2026-08-23-source-management-catalog.md)에 기록한다.
Idempotent source mutation과 immutable receipt 증거는
[source mutation receipt audit](verification/2026-08-23-source-mutation-receipts.md)에 기록한다.
이후 범위 변경도 완료된 ID를 재사용하지 않고 새 decision과 roadmap ID로 추가한다.
네 번째 source 확장 감사와 남은 경계는
[source extension assurance](verification/2026-08-23-source-extension.md)에 기록한다.
현재 열린 작업의 우선순위와 checklist는
[active development TODO](development-todo.md)에서 관리한다.
