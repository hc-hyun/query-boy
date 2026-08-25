# Query Man Implementation Roadmap

Status: Production ready

이 문서는 Query Man의 최종 목적을 구현한 production baseline과 완료 이력을 보존한다.
세부 설계 원칙은 [architecture.md](architecture.md), 현재 검증용 데이터와 계약은
[mvp.md](mvp.md), source 등록 규칙은 [source-onboarding.md](source-onboarding.md)를
따른다. 전체 항목의 구현·검증 연결은
[completion audit](verification/2026-08-23-completion-audit.md)에 production baseline으로,
[refactoring assurance](verification/2026-08-23-refactoring-assurance.md)에 그 시점의 refactoring baseline과
의도적인 운영 경계로, [container runtime audit](verification/2026-08-23-container-runtime.md)에
Docker HTTP/MCP 실행 증거로,
[MCP server assurance](verification/2026-08-23-mcp-server-assurance.md)에 실제 server의
대량·병렬·코너케이스·사용성 검증으로 기록한다. 완료 baseline 이후의 우선순위와 열린
checklist는 [active development TODO](development-todo.md)에서 별도로 관리한다.
각 verification 문서는 자신의 scope와 실행 시점에 대한 증거이며 후속 변경까지 자동으로
포괄하는 단일 최종 audit로 해석하지 않는다.
완료 이력의 caller별 source-scope 문구는 당시 acceptance를 보존하며, 현재 version 2 shared-access
계약은 [shared access audit](verification/2026-08-23-shared-access.md)이 우선한다.

## Final Outcome

여러 PostgreSQL 데이터베이스를 하나의 Text-to-SQL gateway와 하나의 MCP endpoint로
안전하게 제공한다. 신규 source는 애플리케이션 코드 변경이나 서비스 배포 없이
등록할 수 있어야 하며, 질문 이해부터 SQL 실행까지 동일한 runtime 안전 정책과
회귀 검증을 적용해야 한다.

최종 완료 조건은 다음과 같다.

- 신규 source 등록에 source별 runtime 분기나 애플리케이션 재배포가 없다.
- Client와 모델은 DSN, credential, 임의 schema 또는 role을 선택할 수 없다.
- SQL 검증, 권한, timeout, concurrency, row/byte 제한을 gateway가 강제한다.
- Metadata revision과 SQL 실행 결과 사이의 schema 일관성을 보장한다.
- HTTP와 MCP가 동일한 application service와 정책을 사용한다.
- Source별 golden question과 verified SQL을 자동 회귀 검증한다.
- 운영자가 query의 허용·거부·취소 원인을 credential 노출 없이 추적할 수 있다.

## Checklist Rules

- `[x]`는 구현, 자동 테스트, 관련 문서가 모두 반영된 경우에만 표시한다.
- 각 항목 ID는 이슈, PR과 커밋에서 유지한다. 범위가 바뀌어도 ID를 재사용하지 않는다.
- 새 작업은 가능한 한 하나의 검증 가능한 결과만 포함하도록 쪼갠다.
- 설계 결정이 필요한 항목은 decision record를 먼저 작성하고 구현한다.
- 아래 순서는 권장 구현 순서다. 선행 항목이 있는 작업은 해당 ID를 명시한다.

## 0. Implemented Baseline

- [x] `BASE-01` 서로 독립된 PostgreSQL source database 두 개와 결정적 seed를 제공한다.
- [x] `BASE-02` Source별 최소 권한 reader와 `ai` curated view를 제공한다.
- [x] `BASE-03` 최소 Python 3.12 애플리케이션, Python 3.14 container와 고정된 lockfile을 제공한다.
- [x] `BASE-04` YAML source registry와 budget profile을 검증하고 credential 값을 manifest와 응답에서 분리한다.
- [x] `BASE-05` Reader 권한으로 relation, column, type, comment와 view definition hash를 수집한다.
- [x] `BASE-06` Semantic overlay의 grain, alias, measure, business predicate, join과 composition을 검증한다.
- [x] `BASE-07` 질문 범위형 `GET /sources`, `POST /meta` 계약과 relation relevance 선택을 제공한다.
- [x] `BASE-08` Metadata revision, TTL cache, 제한된 stale fallback과 schema drift fail-closed를 제공한다.
- [x] `BASE-09` 외부 bind 시 bearer token을 강제하고 public error에서 내부 연결 정보를 숨긴다.
- [x] `BASE-10` Ruff, mypy, 단위 테스트와 실제 PostgreSQL 통합 테스트 기반을 제공한다.

## 1. Contract And Safety Decisions

안전 경계를 먼저 고정해야 SQL parser나 실행기를 교체해도 외부 계약과 정책이 흔들리지
않는다.

- [x] `DEC-01` 지원할 PostgreSQL SQL 문법 범위와 AST parser 선정 기준을 decision record로 확정한다.
- [x] `DEC-02` 허용·거부할 relation kind, operator, function, system object 정책을 명세한다.
- [x] `DEC-03` Canonical SQL과 query fingerprint 규칙을 명세하고 literal 처리 원칙을 확정한다.
- [x] `DEC-04` `query` 요청·응답, reason code, truncation, plan summary와 error 계약을 확정한다.
- [x] `DEC-05` Metadata revision 불일치 시 거부와 context 재조회 흐름을 확정한다.
- [x] `DEC-06` 초기 budget hard limit과 source별 override 허용 범위를 부하 테스트 근거로 확정한다.
- [x] `DEC-07` Reader role, RLS, SECURITY DEFINER/INVOKER view와 함수 allowlist 정책을 확정한다.
- [x] `DEC-08` MCP transport, 인증 경계와 HTTP application service 재사용 방식을 확정한다.
- [x] `DEC-09` Caller identity, tenant, source authorization 정책 모델을 확정한다.

## 2. SQL Validation

Dependencies: `SQL-01`~`SQL-05`, `SQL-07`~`SQL-10`은 `DEC-01`~`DEC-03`을 따른다.
`SQL-06`은 `DEC-05`, 실행 시점의 OID 검증은 `DEC-07`과 `EXEC-13`을 따른다.

- [x] `SQL-01` PostgreSQL SQL을 AST로 parse하고 정확히 한 문장만 허용한다.
- [x] `SQL-02` `SELECT`와 허용된 read-only `WITH` 외 DDL, DML, transaction, session statement를 거부한다.
- [x] `SQL-03` AST에서 참조 relation과 schema를 추출하고 현재 source의 published catalog allowlist와 대조한다.
- [x] `SQL-04` 함수와 operator를 추출하고 `BETWEEN` 같은 grammar construct를 effective
  operator로 정규화하며 승인한 cast type과 분석 함수를 제한한다.
- [x] `SQL-05` system schema, temp object, cross-database 접근과 client 지정 search path를 거부한다.
- [x] `SQL-06` 요청의 `metadata_revision`과 `sql_policy_revision`이 실행 직전 published
  metadata와 validator policy digest에 각각 같은지 검증한다.
- [x] `SQL-07` SQL을 canonicalize하고 민감 literal을 노출하지 않는 query fingerprint를 생성한다.
- [x] `SQL-08` 정책 거부와 수정 가능한 database 의미 오류를 안정적인 reason code와 bounded
  detail로 반환하고 parser/database 내부 오류를 공개하지 않는다.
- [x] `SQL-09` `DATE BETWEEN`, cast와 분석 함수를 포함한 허용·거부 corpus, 우회 문법,
  nested query, CTE와 Unicode identifier 회귀 테스트를 추가한다.
- [x] `SQL-10` Property/fuzz test로 parser failure가 실행으로 이어지지 않는 fail-closed 성질을 검증한다.

## 3. Guarded Query Execution

Dependencies: `SQL-01`~`SQL-09`, `DEC-06`

- [x] `EXEC-01` HTTP application service에
  `query(source_id, sql, metadata_revision, sql_policy_revision)` 계약을 구현한다.
- [x] `EXEC-02` Caller의 source 접근 권한을 확인한 뒤 source별 concurrency slot을 획득한다.
- [x] `EXEC-03` `BEGIN READ ONLY` transaction과 transaction-local statement, lock, idle timeout을 강제한다.
- [x] `EXEC-04` Source profile의 reader identity, database와 read-only session 상태를 실행 직전에 검증한다.
- [x] `EXEC-05` 결과를 전부 메모리에 올리지 않고 stream하며 row와 UTF-8 byte 상한에서 중단한다.
- [x] `EXEC-06` Client disconnect, deadline과 운영자 요청 시 PostgreSQL query를 cancel하고 rollback한다.
- [x] `EXEC-07` Queue timeout과 pool 고갈을 안정적인 overload reason code로 반환한다.
- [x] `EXEC-08` Optional `EXPLAIN` admission을 구현하되 planner cost만으로 안전을 보장하지 않도록 한다.
- [x] `EXEC-09` `query_id`, fingerprint, elapsed time, row/byte 수, truncation과 plan summary를 반환한다.
- [x] `EXEC-10` 수정 가능한 고정 SQLSTATE만 bounded `QUERY_INVALID`로 분리하고 나머지 DB 오류,
  timeout, cancel과 serialization failure를 비공개 또는 전용 오류 계약으로 매핑한다.
- [x] `EXEC-11` 동시성, timeout, invalid query, large result, disconnect, cancel과 rollback 통합
  테스트를 추가한다.
- [x] `EXEC-12` Reader가 base schema, write statement와 비승인 함수를 실행할 수 없는지 end-to-end로 검증한다.
- [x] `EXEC-13` PostgreSQL이 해석한 function/operator OID, namespace와 volatility를 검증해 AST name allowlist를 보강한다.

## 4. Metadata Quality And Revision Publishing

Physical catalog, immutable publish와 품질 gate를 하나의 revision 계약으로 제공한다.

- [x] `META-01` Primary key, foreign key와 index metadata를 `pg_catalog`에서 권한 범위 내 수집한다.
- [x] `META-02` 수집 metadata와 전역 SQL capability가 revision/API 응답에 포함될 범위를 정하고
  정보 노출 테스트를 추가한다.
- [x] `META-03` Wide view에서 질문 관련 column만 단계적으로 반환하는 column-scoped disclosure를 구현한다.
- [x] `META-04` Exact phrase 중심 relevance를 대체·보완할 retrieval index와 ranking 평가 harness를 구현한다.
- [x] `META-05` Immutable metadata snapshot과 active revision을 control plane에 저장한다.
- [x] `META-06` Refresh 결과를 원자적으로 publish하고 이전 정상 revision으로 rollback할 수 있게 한다.
- [x] `META-07` Verified question/SQL, 기대 relation, 기대 결과 invariant 저장 모델을 구현한다.
- [x] `META-08` Verified query가 참조하는 revision과 현재 schema drift를 검증한다.
- [x] `META-09` L0/L1/L2 source 품질 수준과 publish gate를 자동 판정한다.
- [x] `META-10` Golden question precision, unsupported/clarification recall과 context byte 크기를 CI 지표로 관리한다.

## 5. MCP And Text-to-SQL Workflow

Dependencies: `EXEC-01`~`EXEC-10`, `META-05`~`META-08`, `DEC-08`

- [x] `MCP-01` HTTP와 동일한 service를 호출하는 단일 MCP server를 구현한다.
- [x] `MCP-02` 고정 schema의 `list_sources`, `get_context`, `query` tool, bounded argument
  description과 SQL capability를 제공한다.
- [x] `MCP-03` MCP 요청에도 동일한 caller authorization, budget와 오류 reason code를 적용한다.
- [x] `MCP-04` `answerability`가 `needs_clarification` 또는 `unsupported`이면 query 단계로 진행하지 않는 workflow를 검증한다.
- [x] `MCP-05` Metadata revision mismatch 시 context를 다시 조회하고 SQL을 재생성하는 workflow를 검증한다.
- [x] `MCP-06` Grain, fanout, composition, business predicate와 SQL capability를 준수하고 bounded
  invalid-query correction을 한 번만 수행하는 공통 Text-to-SQL Skill을 작성한다.
- [x] `MCP-07` 두 MVP source의 전체 golden question을 MCP tool 호출부터 실제 결과까지 end-to-end 검증한다.
- [x] `MCP-08` Tool schema 호환성, 응답 크기와 `/mcp` request arrival부터 final ASGI body까지의
  bounded lifecycle timing 회귀 테스트를 추가한다.

## 6. No-Deploy Source Onboarding

Dependencies: `META-05`, `META-06`

- [x] `ONB-01` Source manifest와 secret을 검증된 control plane 입력으로 등록하는 관리자 계약을 구현한다.
- [x] `ONB-02` 신규 source 연결, 권한, catalog, overlay와 budget을 격리된 staging 단계에서 검증한다.
- [x] `ONB-03` 검증 성공한 source profile과 metadata revision을 원자적으로 publish한다.
- [x] `ONB-04` Runtime이 재시작 없이 source 추가·변경·비활성화를 반영한다.
- [x] `ONB-05` 잘못된 update가 현재 정상 source와 revision에 영향을 주지 않도록 rollback한다.
- [x] `ONB-06` Credential rotation을 연결 중단과 secret 노출 없이 반영한다.
- [x] `ONB-07` Manifest schema version migration과 하위 호환 정책을 구현한다.
- [x] `ONB-08` 세 번째 fixture source를 애플리케이션 코드 변경 없이 등록하는 acceptance test를 추가한다.
- [x] `ONB-09` L0 등록부터 L2 verified query publish까지 운영 runbook을 완성한다.

## 7. Authorization And Tenant Isolation

Dependencies: `DEC-09`, `ONB-01`

- [x] `AUTH-01` 인증된 caller identity를 HTTP와 MCP application context에 전달한다.
- [x] `AUTH-02` Caller/tenant별 허용 source 목록을 서버 정책으로 관리한다.
- [x] `AUTH-03` `/sources`, `/meta`, `query`가 동일한 source authorization 결과를 사용한다.
- [x] `AUTH-04` Tenant identity를 SQL text나 client-controlled session setting으로 주입하지 않는다.
- [x] `AUTH-05` RLS가 필요한 source의 trusted session context 설정과 reset을 검증한다.
- [x] `AUTH-06` Connection pool 재사용 시 tenant context가 누출되지 않는 통합 테스트를 추가한다.
- [x] `AUTH-07` 인증·인가 실패 응답과 audit event가 source 존재 여부나 credential을 노출하지 않게 한다.

## 8. Observability And Operations

- [x] `OPS-01` Credential, bearer token, SQL literal과 DB error detail을 제거하는 구조화 logging 정책을 구현한다.
- [x] `OPS-02` Metadata refresh, validation reject, queue, execution, timeout, cancel과 truncation metric을 제공한다.
- [x] `OPS-03` Source별 health/readiness를 정의하되 public health가 source inventory를 노출하지 않게 한다.
- [x] `OPS-04` `query_id`로 application log와 PostgreSQL activity를 연계한다.
- [x] `OPS-05` Revision publish 실패, stale 상한 초과, reject 급증과 pool 고갈 alert 기준을 정의한다.
- [x] `OPS-06` Graceful shutdown 중 신규 query를 거부하고 실행 중 query를 제한 시간 안에 종료한다.
- [x] `OPS-07` Migration, backup, restore와 disaster recovery runbook을 작성하고 복구 훈련을 검증한다.
- [x] `OPS-08` Dependency, container, secret scanning과 정기 보안 업데이트 절차를 CI에 추가한다.

## 9. Release Acceptance

Dependencies: all required items above

- [x] `REL-01` 세 개 이상의 서로 다른 PostgreSQL source가 동일한 runtime 코드 경로를 사용하는지 확인한다.
- [x] `REL-02` 신규 source를 서비스 재배포 없이 등록하고 MCP에서 조회·실행하는 시나리오를 통과한다.
- [x] `REL-03` 모든 golden/verified question의 relation 선택, SQL 안전성과 결과 invariant를 통과한다.
- [x] `REL-04` 공격·오용 corpus에서 write, privilege escalation, system object와 resource limit 우회를 차단한다.
- [x] `REL-05` 부하 테스트에서 source별 concurrency와 queue 격리, cancel과 hard limit을 확인한다.
- [x] `REL-06` Schema drift, source 장애, stale 만료와 control plane rollback 복구 시나리오를 통과한다.
- [x] `REL-07` 운영 dashboard, alert, audit와 runbook 검토를 완료한다.
- [x] `REL-08` Architecture의 Success Criteria를 전부 충족하고 문서 상태를 `Production ready`로 갱신한다.

## 10. Source Extension Assurance

Dependencies: `ONB-*`, `AUTH-*`, `MCP-*`, `REL-*`

- [x] `EXT-01` 네 번째 독립 PostgreSQL fixture에서 quoted identifier, rich/nullable type,
  composite grain, one-to-many fanout과 zero-child row를 검증한다.
- [x] `EXT-02` 신규 source의 항상 필수·조건부·불필요 작업을 운영 checklist로 분리한다.
- [x] `EXT-03` Production 전권 caller가 명시적 opt-in으로 미래 control-plane source를
  재시작 없이 사용하고 제한 caller는 계속 숨김 처리되는지 검증한다.
- [x] `EXT-04` Publish staging에서 reader role 안전 속성을 검사하고 같은 source ID의
  connection endpoint 재지정을 fail-closed한다.
- [x] `EXT-05` 다른 runtime replica가 control-plane verified revision을 poll해 L2 generation을
  재시작 없이 적용한다.
- [x] `EXT-06` MCP가 추가 입력과 내부 예외를 비공개 거부하고 duplicate result column을
  안정적인 reason code로 거부한다.
- [x] `EXT-07` 두 runtime replica와 실제 bearer caller를 사용해 L0→L2 publish, revision
  refresh, exact MCP result, authorization isolation과 deactivate를 end-to-end 검증한다.
- [x] `EXT-08` 전체 unit/integration/quality/verified/security 회귀와 runtime source-specific
  branch 부재를 확인하고 기존 Production-ready 목표의 유지 여부를 기록한다.

상세 반복 절차와 현재 한계는
[`source-extension-checklist.md`](source-extension-checklist.md)에 유지한다.

## 11. Refactoring Assurance

Dependencies: completed production baseline and extension assurance

완료 표시에는 재현 테스트, 최소 수정, 관련 운영 계약 정비와 전체 회귀 검증이 모두
필요하다. 과거 verification 문서는 당시 실행 증거로 보존하고, 이번 보강의 새 증거는
[refactoring assurance audit](verification/2026-08-23-refactoring-assurance.md)에 기록한다.

- [x] `REF-01` Composite key·foreign-key pairing·index column 순서가 metadata revision에서
  보존되는지 검증하고 순서 변경을 서로 다른 revision으로 판정한다.
- [x] `REF-02` MCP caller context provider 실패가 세 tool 모두에서 내부 정보 없는
  `INTERNAL_ERROR`로 수렴하는지 실제 MCP client로 검증한다.
- [x] `REF-03` Runtime이 실제 logging backend에서 지원하지 않는 log level을 설정 단계에서
  거부한다.
- [x] `REF-04` 이전 source generation의 지연된 metadata refresh가 새 generation의 active
  revision을 덮지 못하도록 process epoch와 control-plane CAS를 함께 강제한다.
- [x] `REF-05` Catalog와 query 직전에 동일한 reader role/session 정책을 검사하고 privilege
  drift에는 stale metadata를 제공하지 않는다.
- [x] `REF-06` Graceful drain이 semaphore queue·pool wait·DB 실행을 포함해 이미 수락한 모든
  query를 추적하고 grace 이후 남은 작업을 취소·rollback한다.
- [x] `REF-07` Disabled source의 credential rotation, rollback pin resume, stale generation
  apply와 source identity 변경을 명시적인 상태 전이로 fail-closed한다.
- [x] `REF-08` Work memory, temporary file, parallel worker와 JIT 상한을 versioned budget으로
  transaction-local 강제하고 replica 수를 포함한 reader connection capacity를 문서화한다.
- [x] `REF-09` Startup·control-plane polling·dynamic deactivate 상태가 readiness와 operator
  health에 누락되지 않게 한다.
- [x] `REF-10` Process restart가 stored metadata의 stale age를 초기화하지 않도록 publish
  provenance에 기반한 상한을 적용한다.
- [x] `REF-11` L2 verified contract가 현재 metadata뿐 아니라 source 실행 budget/policy와도
  호환되는지 publish 시점에 재검증한다.
- [x] `REF-12` PostgreSQL `numeric`, binary와 시간 값을 손실·인코딩 오류 없이 전달하는 JSON
  scalar 계약을 고정하고 byte accounting과 API serialization을 일치시킨다.
- [x] `REF-13` 실제 server SIGTERM 순서에서 readiness 전환과 application drain grace가
  실행되는지 검증하고 process manager timeout을 일관되게 설정한다.
- [x] `REF-14` Metric·audit·dashboard·restore 문구를 실제 수집 가능한 신호와 검증 범위에
  맞추고 비용 통제 운영 절차를 실행 가능한 runbook으로 정비한다.
- [x] `REF-15` Ruff, mypy, unit/integration/load/evaluation/verified/security 회귀와 문서 링크
  검사를 통과한 최종 completion audit을 남긴다.

## 12. Containerized HTTP And MCP Runtime

Dependencies: production baseline and `ADR-0015`

완료 표시에는 host 개발 경계를 유지하면서 실제 Compose network, image, 인증, HTTP와 MCP
호출을 재현 가능하게 검증해야 한다. 실행 증거는
[container runtime audit](verification/2026-08-23-container-runtime.md)에 기록한다.

- [x] `DEP-01` 단일 `query-man` container가 HTTP API와 stateless Streamable HTTP `/mcp`
  endpoint를 함께 제공하는 network·인증·secret 경계를 decision record로 확정한다.
- [x] `DEP-02` Locked production dependency만 포함한 non-editable multi-stage image를
  non-root direct `query-man` entrypoint와 read-only filesystem으로 실행한다.
- [x] `DEP-03` 동일 source manifest가 host loopback과 Compose service DNS를 선택적
  `host_env`로 resolve하고 control-plane 문서에는 resolved endpoint만 저장한다.
- [x] `DEP-04` PostgreSQL TCP health dependency, loopback host publish, `/ready` healthcheck와
  application drain보다 긴 container stop grace를 구성한다.
- [x] `DEP-05` PostgreSQL administrator secret을 application에서 분리하고 source-limited
  bearer caller 및 명시적 MCP Host/Origin allowlist를 강제한다.
- [x] `DEP-06` Published port의 exact readiness와 무인증 401을 검증하고 공식 MCP client로
  세 tool discovery, source authorization과 실제 guarded query를 통과한다.
- [x] `DEP-07` 기존 host 기반 unit/integration job과 container smoke를 분리하고 Query Man
  application image의 Critical vulnerability scan을 CI gate에 추가한다.
- [x] `DEP-08` README, architecture, operations, MVP 실행 절차와 재사용 가능한 container
  verification script를 정비하고 전체 회귀 증거를 남긴다.

## 13. MCP Server Assurance

Dependencies: `MCP-*`, `DEP-*`, query budget and observability baseline

완료 표시는 in-process adapter test가 아니라 실행 중인 Compose `/mcp` endpoint, 공식 MCP
client와 실제 PostgreSQL fixture를 사용해야 한다. 실행 결과와 남은 선택지는
[MCP server assurance](verification/2026-08-23-mcp-server-assurance.md)에 기록한다.

- [x] `MCPX-01` Docker MCP 전용 marker와 loopback/token-safe client fixture를 만들고 기본
  unit/integration selection 및 CI container job과 분리한다.
- [x] `MCPX-02` Versioned quality case 전체를 MCP `get_context`로 실행해 relation,
  answerability와 context byte gate를 자동 확장형으로 검증한다.
- [x] `MCPX-03` Verified query contract 전체를 MCP context→query로 실행해 revision, relation,
  typed result hash, truncation과 unique query ID를 검증한다.
- [x] `MCPX-04` Host/Origin, 인증, 단일 exact media type, body limit, malformed JSON과 strict
  tool argument 및 단일 current protocol version의 bounded 비노출 거부를 실제 transport에서
  검증한다.
- [x] `MCPX-05` Application 오류를 safe structured payload와 MCP `isError=true`로 함께
  반환하고 string normalization 및 integer coercion 거부를 고정한다.
- [x] `MCPX-06` Tool마다 server-generated correlation ID, caller, 허가 source, duration,
  outcome과 public error를 기록하되 SQL/question/token은 기록하지 않는 debug log와 metric을
  추가한다.
- [x] `MCPX-07` 동일 client 24개 병렬 query, 독립 session 8개와 source concurrency 포화를
  실행해 exact result, overload, timeout, source 격리 및 즉시 복구를 검증한다.
- [x] `MCPX-08` Current MCP POST disconnect를 query cancel/rollback으로 전파하고 같은 client
  재사용을 실제 socket에서 검증하며 이전 handshake/version을 fail-closed한다.

## 14. Post-Baseline Completion Ledger And Active Development

완료된 131개 baseline checkbox의 설명과 ID는 당시 acceptance를 보존한다. 이후 기능 보강은
기존 설명을 소급 확장하지 않고 아래 ledger의 새 ID로 기록한다. 아직 끝나지 않은 항목은
[active development TODO](development-todo.md)에만 두며, 완료 시 같은 변경에서 이 ledger로
옮긴다.

| ID | 완료 결과 | 실행 증거 |
|---|---|---|
| `SOAK-01` | MCP protocol version의 누락·이전·미지원·중복을 bounded error로 거부했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-02` | 기본 1 replica와 soak 전용 2 replica Compose 구성을 분리했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-03` | 두 replica의 tool schema, metadata revision, exact result와 query ID uniqueness를 검증했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-04` | Replica별 source concurrency 포화·격리·timeout 후 복구를 검증했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-05` | 공식 client의 1,000 stateless session을 두 replica에서 균등하게 통과시켰다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-06` | Session churn 전후 process, FD와 RSS growth gate를 고정했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `SOAK-07` | 장시간 soak를 주간·수동 CI와 재현 절차로 분리했다. | [multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md) |
| `CTRL-01` | Numbered Control DB migration과 disposable integration store를 도입했다. | [control schema migration audit](verification/2026-08-23-control-schema-migrations.md) |
| `CTRL-02` | Managed authority mode, zero-bootstrap와 Control DB lifecycle precedence를 고정했다. | [managed source startup audit](verification/2026-08-23-managed-source-startup.md) |
| `CTRL-03` | Access policy v2, shared visibility/resource tier와 query/admin capability 분리를 고정했다. | [shared access audit](verification/2026-08-23-shared-access.md) |
| `CTRL-04` | Strict manifest v2 provenance와 secret-free admin catalog/history를 구현했다. | [source management catalog audit](verification/2026-08-23-source-management-catalog.md) |
| `CTRL-05` | Expected-state mutation, authoritative receipt와 append-only lifecycle history를 구현했다. | [source mutation receipt audit](verification/2026-08-23-source-mutation-receipts.md) |
| `CTRL-06` | Stable managed replica가 desired/applied generation·state·metadata drift와 DB-clock freshness를 latest-only로 보고하고 operator가 source별로 조회하게 했다. | [runtime replica observation audit](verification/2026-08-25-runtime-replica-observations.md) |
| `CTRL-07` | Optional manifest observability, bounded catalog resource sample과 privacy-safe gateway hourly lower-bound rollup을 migration 4에 저장했다. 31일은 logical visibility/input window이고 source당 최신 1,000행만 physical cap으로 유지한다. | [resource and gateway observation audit](verification/2026-08-25-resource-and-gateway-observations.md) |
| `CTRL-08` | Migration 5의 latest resource attempt/last-success와 operator-only usage endpoint를 추가해 five-state resource availability, global gateway reporter health, inclusive 31일 lower-bound를 missing-to-zero 없이 제공했다. Provider monetary cost는 근거가 없어 not configured로 유지했다. | [usage projection audit](verification/2026-08-25-usage-projection.md) |
| `CTRL-09` | PostgreSQL 18.4의 격리 Control DB를 18.6 fresh DB로 복원하고 13-table fingerprint, 원래 key의 모든 generation decrypt, logical retention, zero-bootstrap, receipt replay와 두 managed replica의 query/convergence를 하나의 격리 recovery fixture acceptance로 재현했다. | [control recovery acceptance](verification/2026-08-25-control-recovery-acceptance.md) |
| `SQLX-01` | 기존 SQL validation baseline 뒤 window·ordered-set·문자열·JSON 함수 정책과 corpus를 보강했다. | Commit `de2b364`; [ADR 0001](decisions/0001-postgresql-ast-validation.md), [`test_sql_validation.py`](../tests/test_sql_validation.py) |
| `QCORR-01` | 수정 가능한 query/argument 오류에 bounded reason별 correction action을 추가하고 한 번의 retry workflow를 고정했다. | Commit `de2b364`; [ADR 0002](decisions/0002-guarded-query-contract.md), [ADR 0006](decisions/0006-mcp-transport-and-workflow.md), [`test_query.py`](../tests/test_query.py), [`test_mcp.py`](../tests/test_mcp.py) |
| `MOD-01` | 논리 module owner, 허용 dependency, 계약 승인과 module-scoped agent 절차를 문서·테스트로 고정했다. | Commit `de2b364`; [ADR 0018](decisions/0018-module-ownership-and-contract-governance.md), [module index](modules/README.md), [`test_documentation.py`](../tests/test_documentation.py) |
| `MOD-02` | Active-only TODO, module별 작업 gate, non-Python artifact primary owner/single-writer와 immutable baseline description 검사를 추가했다. | [active TODO](development-todo.md), [module index](modules/README.md), [`test_documentation.py`](../tests/test_documentation.py) |
| `MOD-03` | Startup cleanup과 다섯 contract debt의 용어, 객관식 선택지, 영향·불변조건·승인 형식을 이해 문서로 고정했다. | [module contract decision guide](module-contract-decision-guide.md), [`test_documentation.py`](../tests/test_documentation.py) |
| `RTSAFE-01` | MCP child lifespan 진입 실패 시 child exit를 호출하지 않고 parent 최상위 resource를 고정 순서로 정확히 한 번씩 정리 시도하며 최초 startup error를 보존한다. | [Runtime contract](modules/runtime/README.md#startup-contract), [Delivery child lifespan contract](modules/delivery/README.md#child-lifespan-ownership-contract), [`test_runtime_startup_cleanup.py`](../tests/test_runtime_startup_cleanup.py) |
| `MOD-04` | Delivery의 Control persistence/Assurance DTO hidden import를 제거하고 Control Plane public sequence/verified-publish input에서 Assurance DTO로 exact mapping하며 HTTP, storage, verified identity/hash 의미를 보존했다. | [Control Plane contract](modules/control-plane/README.md#source-administration-contract), [Delivery contract](modules/delivery/README.md#소비-계약), [`test_documentation.py`](../tests/test_documentation.py), [`test_http.py`](../tests/test_http.py), [`test_source_admin.py`](../tests/test_source_admin.py), [`test_control_startup.py`](../tests/test_control_startup.py) |
| `MOD-05` | Source Catalog의 read-only `SourceReader`와 이를 확장하는 `SourceProjectionWriter`를 분리해 ordinary consumer와 Control reloader의 type capability를 좁히고 registry/load/runtime output을 보존했다. | [Source Catalog contract](modules/source-catalog/README.md#source-read-contract), [module index](modules/README.md#현재-코드-전환-맵), [`test_registry.py`](../tests/test_registry.py), [`test_http.py`](../tests/test_http.py), [`test_source_admin.py`](../tests/test_source_admin.py) |
| `MOD-06` | 작은 Query/Catalog application Protocol을 유지하면서 Runtime 전용 lifecycle composite를 추가하고, 모든 required callable을 composition에서 검증해 누락 adapter를 ready 전에 거부하며 기존 drain/invalidation 순서를 보존했다. | [Guarded Query lifecycle contract](modules/guarded-query/README.md#executor-lifecycle-contract), [Metadata catalog capability](modules/metadata/README.md#catalog-provider-capability-contract), [Runtime composition](modules/runtime/README.md#composition-contract), [`test_query.py`](../tests/test_query.py), [`test_catalog.py`](../tests/test_catalog.py), [`test_managed_mode.py`](../tests/test_managed_mode.py), [`test_http.py`](../tests/test_http.py), [`test_runtime_startup_cleanup.py`](../tests/test_runtime_startup_cleanup.py) |
| `MOD-07` | `SourceProfile`/semantic과 published catalog/prepared metadata graph를 tuple·alias-copy read-only mapping·frozen dataclass로 재귀적으로 immutable하게 만들고 provider/decoder boundary에서 freeze했다. Persisted/wire JSON array/object, metadata revision golden, snapshot codec와 result-hash 계약은 유지해 DB migration 없이 rolling compatibility를 보존했다. | [Source immutability contract](modules/source-catalog/README.md#published-source-immutability-contract), [Metadata published contract](modules/metadata/README.md#published-metadata-contract), [`test_registry.py`](../tests/test_registry.py), [`test_catalog.py`](../tests/test_catalog.py), [`test_revision.py`](../tests/test_revision.py), [`test_metadata_store.py`](../tests/test_metadata_store.py), [`test_metadata.py`](../tests/test_metadata.py) |
| `MOD-08` | Assurance quality/verified core에서 concrete adapter 조립을 제거하고 두 bootstrap-only offline command를 전용 `assurance_cli.py` composition root로 격리했다. Command/`--root`/JSON/exit, Guarded Query safety·RLS fail-closed path와 cleanup 순서는 유지하고 production Runtime/Control staging wiring은 바꾸지 않았다. | [Assurance offline CLI contract](modules/assurance/README.md#offline-cli-composition-contract), [`assurance_cli.py`](../src/query_man/assurance_cli.py), [`test_assurance_cli.py`](../tests/test_assurance_cli.py), [`test_verified.py`](../tests/test_verified.py), [`test_registry.py`](../tests/test_registry.py) |
| `SKILL-01` | Source onboarding planning의 positive trigger와 manual admin/query workflow negative boundary를 확정했다. | [Skill plan](source-onboarding-skill-plan.md), [Skill acceptance](verification/2026-08-25-source-onboarding-skill.md) |
| `SKILL-02` | 비밀 아닌 입력, 8-section output, DB-owner/admin handoff, shared visibility와 secret/mutation threat 경계를 확정했다. | [Skill plan](source-onboarding-skill-plan.md), [Skill acceptance](verification/2026-08-25-source-onboarding-skill.md) |
| `SKILL-03` | Plan-only repository Skill과 progressive-disclosure reference를 구현했다. | [`query-man-source-onboarding`](../skills/query-man-source-onboarding/SKILL.md), [`test_onboarding_skill.py`](../tests/test_onboarding_skill.py) |
| `SKILL-04` | 정상·누락·negative-routing·DBA·prompt-injection·secret/immediate-publish 요청을 fresh-context forward evaluation으로 검증했다. | [Skill acceptance](verification/2026-08-25-source-onboarding-skill.md) |
| `SKILL-05` | `support-tickets` owner/admin handoff를 재현하고 repository, source DB, Control authority/roles와 spy admin endpoint의 mutation 0을 검증했다. | [Skill acceptance](verification/2026-08-25-source-onboarding-skill.md) |
| `SKILL-06` | Skill validator, 정적 회귀, 운영 문서와 기본 onboarding planning workflow 채택 기록을 완료했다. | [Skill acceptance](verification/2026-08-25-source-onboarding-skill.md), [source onboarding](source-onboarding.md) |
| `DBEDGE-01` | 세 UUID별 disposable PostgreSQL source에서 wide/untrusted metadata, temporal/rich scalar, partition/materialized/empty result와 leak-free cleanup을 검증하고 기존 ADR을 위반한 wide-match overflow를 수정했다. TimeZone canonicalization gap은 계약 승인 전 미구현으로 분리했다. | [source database corner acceptance](verification/2026-08-25-source-database-corners.md), [`test_source_database_corners.py`](../tests/test_source_database_corners.py), [`test_metadata.py`](../tests/test_metadata.py) |
| `DBEDGE-02` | 추가 disposable DB에서 live view-definition revision 전환, cold/warm relation·column·structure catalog 상한, multibyte row truncation과 unsupported infinity/range/nonempty-multirange의 비공개 실패·rollback·pool 복구를 고정했다. Month interval, fractional JSONB numeric, empty multirange의 hash collision과 reader-format default drift를 재현하고 계약 변경 전 구현을 중단했다. | [source database corner acceptance](verification/2026-08-25-source-database-corners.md), [proposed ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md), [`test_source_database_corners.py`](../tests/test_source_database_corners.py) |
| `DBEDGE-03` | 별도 disposable DB들에서 SQL semantic GUC drift, array lower-bound 소실, empty unsupported array 우회, anonymous record field/type 소실과 string-valued unknown OID의 accidental success를 재현했다. `bytea_output`은 current loader가 안정적으로 정규화함을 확인하고, 의미 수정은 확장한 proposed ADR 0020의 정확한 승인 전 중단했다. | [source database corner acceptance](verification/2026-08-25-source-database-corners.md), [proposed ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md), [`test_source_database_corners.py`](../tests/test_source_database_corners.py) |
| `TIME-01` | Reader session UTC, aware datetime UTC `+00:00`, business calendar `Asia/Seoul`, SQL-policy/metadata revision 재료, full verified reissue, coordinated cutover와 immutable rollback 보존을 하나의 정확한 계약으로 확정하고 사용자 승인을 받았다. R1에서 업무 날짜 SQL을 명시하고 dev/market 9개 계약의 기존 결과를 보존했다. | [ADR 0019](decisions/0019-canonical-time-stability.md), [canonical time verification](verification/2026-08-25-canonical-time-stability.md) |
| `TIME-02` | Catalog와 Query가 transaction 시작 직후 UTC를 local 설정·검사하고 aware datetime만 UTC `+00:00`으로 정규화한다. Canonical-time material을 metadata와 SQL-policy revision에 넣어 이전 token을 실행 전에 거부하면서 naive datetime/date/time/timetz 의미는 보존했다. | [ADR 0019](decisions/0019-canonical-time-stability.md), [canonical time verification](verification/2026-08-25-canonical-time-stability.md), [`test_catalog.py`](../tests/test_catalog.py), [`test_query.py`](../tests/test_query.py), [`test_result_encoding.py`](../tests/test_result_encoding.py) |

Ledger의 완료 결과 column은 찾기 쉬운 요약일 뿐 acceptance를 축소하지 않는다. 각 ID에 연결된
evidence가 해당 완료 작업의 상세 경계와 실행 증거를 보존한다. Audit가 연결된 row에서 각 audit는
자신의 실행 시점과 scope만 증명하며, post-baseline code 변경 전체를 하나의 과거 audit가
포괄한다고 해석하지 않는다.

## Recommended Milestones

| Milestone | Scope | Exit |
|---|---|---|
| M1 Safe Query MVP | `DEC-*`, `SQL-*`, `EXEC-*` | HTTP에서 검증된 read-only query를 hard limit 안에서 실행한다. |
| M2 MCP MVP | `MCP-01`~`MCP-08` | 두 fixture source의 golden question을 MCP end-to-end로 통과한다. |
| M3 Published Metadata | `META-*` | Immutable revision과 verified query를 저장·publish·rollback한다. |
| M4 No-Deploy Onboarding | `ONB-*` | 세 번째 source를 코드 변경과 재배포 없이 등록한다. |
| M5 Multi-Tenant Operations | `AUTH-*`, `OPS-*` | Tenant 격리, 관측성, 복구와 운영 안전 기준을 충족한다. |
| M6 Production Acceptance | `REL-*` | 최종 성공 기준과 공격·장애·부하 시나리오를 모두 통과한다. |
| M7 Extension Assurance | `EXT-*` | 네 번째 source와 production-authenticated multi-replica MCP 회귀를 통과한다. |
| M8 Refactoring Assurance | `REF-*` | 상태 경쟁, 권한 drift, 종료·비용 경계를 재검증하고 문서와 실제 보장을 일치시킨다. |
| M9 Container Runtime | `DEP-*` | Compose의 단일 HTTP/MCP image가 격리·인증·health·실제 query acceptance를 통과한다. |
| M10 MCP Server Assurance | `MCPX-*` | 실제 Docker MCP에서 전체 contract, 병렬·포화·취소·비노출 경계를 통과한다. |
| M11 Multi-Replica Soak | `SOAK-*` | 두 Docker replica의 exact result, 독립 포화·복구와 1,000-session resource gate를 통과한다. |
| M12 Centralized Source Management | `CTRL-*` | Admin 한곳에서 source authority, 공통 resource tier, 상태·규모·비용 freshness를 관리한다. |
| M13 Onboarding Planning Skill | `SKILL-*` | Credential·mutation 없이 반복 가능한 source plan과 admin handoff를 만든다. |
| M14 Canonical Time Stability | `TIME-*` | 같은 PostgreSQL instant의 public value와 verified hash를 reader timezone과 무관하게 고정한다. |
| M14.5 Lossless Scalar, Reader And Result Types | `ENC-*` | Calendar interval/nested JSON numeric, array/record type identity 손실과 unsupported OID 우회를 닫고 SQL 의미·decode를 role default와 무관하게 고정한다. |
| M15 Cost Attribution | `COST-*` | DB-native 사용량을 source/resource-tier time bucket으로 bounded 집계하고 운영 threshold를 고정한다. |
| M16 Workflow Trace | `TRACE-*` | 여러 tool call과 retry를 bounded trace ID로 안전하게 연결한다. |

M1부터 M13, `TIME-01`~`TIME-02`와 별도 assurance `DBEDGE-01`~`DBEDGE-03`은 완료됐다.
M14.5의 `ENC-*` 결정·구현과 M14 production 전환 `TIME-03`은 active이며 M15와 M16은 각각
정확한 계약을 다시 승인받아야 한다. M15/M16의
[proposed ADR 0021](decisions/0021-database-native-cost-attribution.md)과
[proposed ADR 0022](decisions/0022-w3c-workflow-trace-context.md)는 read-only prework이며 priority/start
gate나 contract 승인이 아니다. 새로운 기능은 기존 완료 ID나 설명을 소급 변경하지 않고 별도
roadmap 항목과 검증 가능한 exit condition을 추가한다.
