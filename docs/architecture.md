# Query Man Architecture

Status: Production ready

## Goal

여러 PostgreSQL 데이터베이스를 하나의 Text-to-SQL gateway와 하나의 MCP
endpoint로 제공한다. 신규 데이터베이스 추가 과정에서 애플리케이션 코드 변경과
배포가 발생하지 않는 것을 최종 성공 기준으로 삼는다.

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
  |-- Physical Catalog                                  |-- Source Generations
  |-- Semantic Overlay                                  |-- Metadata Revisions
  |-- Budget Profile                                    `-- Verified Contracts
  `-- guarded connection
        |
PostgreSQL Reader / Analytics Replica
```

Bootstrap은 `development_issues`와 `market_voc` 독립 source database를 사용한다. M6 release
acceptance는 `support_tickets` database를 control plane으로 실행 중 등록한다. 이후 M7
extension assurance는 quoted/rich-type `commerce_edges` database를 같은 runtime과 MCP
경로로 검증한다. Bootstrap source의 구체적인 grain, seed와 검증 범위는
[mvp.md](mvp.md)에 기록한다.

Local Compose는 PostgreSQL과 단일 `query-man` application container를 실행한다. 이 한
process가 HTTP와 `/mcp`를 함께 제공해 registry, metadata cache, authorization과 query
admission을 공유한다. Container network의 PostgreSQL service name은 manifest의 선택적
`host_env`로 resolve하되 control plane에는 resolved endpoint만 저장한다. Host publish는
loopback으로 제한하고 container 내부 non-loopback bind에는 source-limited bearer policy를
강제한다. Image, secret, readiness와 shutdown 경계는
[ADR 0015](decisions/0015-containerized-local-runtime.md)를 따르며 실제 container acceptance는
[container runtime audit](verification/2026-08-23-container-runtime.md)에 기록한다. 전체
MCP contract와 병렬·포화·취소·비노출 경계의 실제 server 검증은
[MCP server assurance](verification/2026-08-23-mcp-server-assurance.md)에, 두 replica session
내구성과 resource 경계는
[multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md)에 기록한다.

## Component Boundaries

### Physical Catalog

`pg_catalog`에서 relation, column, type, primary key, foreign key, index와 comment를 공통
형식으로 생성해 revision snapshot으로 발행한다. Planner 통계의 row estimate는 fresh catalog
응답에만 포함될 수 있는 best-effort hint이며 revision 재료나 persisted snapshot contract가
아니다. Restart 복원 뒤 없을 수 있으므로 안전·정확성 판단에 사용하지 않는다.

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
- plan admission 정책 profile

Bootstrap registry는 `config/sources/*.yaml`을 읽는다. Credential 값은 manifest에
저장하지 않고 환경 변수 이름만 참조한다. Control-plane source revision과 암호화된
credential 계약은 [ADR 0012](decisions/0012-control-plane-source-revisions.md)를 따르며,
runtime hot reload와 관리자 staging API가 bootstrap 이후 변경을 반영한다.

No-deploy source 등록과 caller별 접근 grant는 서로 다른 변경이다. 미래 source까지
명시적으로 신뢰한 `all_sources` caller는 hot-added source를 즉시 사용할 수 있다. 제한
caller의 개별 allowlist는 현재 startup 설정이므로 grant 변경에는 restart가 필요하다.

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

`EXPLAIN total_cost`는 시간이나 금액이 아니므로 단독 안전 기준으로 사용하지
않는다. Timeout, concurrency, result size, temporary resource 제한과 query cancel을
실제 안전 경계로 사용한다.

## MCP Contract

하나의 MCP endpoint에서 고정된 tool schema를 제공한다. 데이터베이스나 table마다
tool을 생성하지 않는다.

```text
list_sources()

get_context(source_id, question, max_objects?)
  -> source metadata, question, metadata_revision, snapshot_status, quality_level,
     answerability, relations[{columns, measures, grain, keys, indexes}], joins,
     business_terms, composition_hints, ambiguities, truncated

query(source_id, sql, metadata_revision)
  -> success: status, query_id, metadata_revision, fingerprint, columns, rows,
              row_count, result_bytes, truncated, queue_ms, elapsed_ms, plan_summary
  -> failure: error.code, error.message, error.details?.reason_code
```

Source가 사용자 session에 고정되는 환경에서는 `list_sources`를 생략할 수 있다.
하나의 query는 하나의 source만 대상으로 하며 cross-database federation은 별도
기능으로 취급한다.

MCP를 붙이기 전 동일한 application contract를 HTTP로 먼저 검증한다.

```text
GET  /sources
POST /meta { source_id, question, max_objects? }
POST /query { source_id, sql, metadata_revision }
```

`/meta`는 reader가 실제로 조회할 수 있는 allowed schema/relation kind만 catalog에서
읽는다. DB comment는 길이와 제어 문자를 제한한 비신뢰 description data이며, join은
comment 문장을 파싱하지 않고 manifest에 승인된 edge만 반환한다. Schema, view
definition, comment, overlay, source execution budget 또는 revision-scoped source policy가
바뀌면 `metadata_revision`도 바뀐다. 이 revision에 묶인 L2 verified SQL은 현재 실행
경계에서 다시 통과해야 한다. Application 전역 SQL parser/function policy의 code 변경은
별도 release 회귀 대상이며 자동으로 metadata revision을 바꾸지는 않는다.

## Onboarding Levels

- L0: source 등록과 자동 catalog. 단순 질의를 best-effort로 지원한다.
- L1: description, grain, 시간 column과 비표준 join을 보강한다.
- L2: L1 조건과 현재 metadata revision의 verified query contract를 충족한다. Measure와
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

- 신규 PostgreSQL source 추가에 애플리케이션 코드 변경이 없다.
- `source_id`에 따른 runtime 분기문이 추가되지 않는다.
- 보안과 비용 정책은 Skill이나 prompt가 아니라 gateway가 강제한다.
- 복잡한 DB만 필요한 만큼 semantic overlay 또는 curated view를 추가한다.
- 실제 질문과 SQL로 source별 품질을 회귀 검증할 수 있다.

## Decisions

- PostgreSQL AST parser와 canonical query fingerprint는
  [ADR 0001](decisions/0001-postgresql-ast-validation.md)을 따른다.
- Guarded query 순서, revision, truncation과 오류 계약은
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

## Completion Tracking

Production acceptance까지의 구현 순서와 완료 증거는
[implementation-roadmap.md](implementation-roadmap.md)와
[completion audit](verification/2026-08-23-completion-audit.md)에서 baseline으로 관리한다.
현재 코드의 refactoring assurance와 의도적인 운영 경계는
[refactoring assurance audit](verification/2026-08-23-refactoring-assurance.md)에 기록한다.
이후 범위 변경도 완료된 ID를 재사용하지 않고 새 decision과 roadmap ID로 추가한다.
네 번째 source 확장 감사와 남은 경계는
[source extension assurance](verification/2026-08-23-source-extension.md)에 기록한다.
현재 열린 작업의 우선순위와 checklist는
[active development TODO](development-todo.md)에서 관리한다.
