# Query Man Implementation Roadmap

Status: Active

이 문서는 Query Man의 최종 목적을 구현하기 위한 TODO의 단일 관리 문서다.
세부 설계 원칙은 [architecture.md](architecture.md), 현재 검증용 데이터와 계약은
[mvp.md](mvp.md), source 등록 규칙은 [source-onboarding.md](source-onboarding.md)를
따른다.

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
- [x] `BASE-03` `uv` 기반 Python 3.12 애플리케이션과 고정된 lockfile을 제공한다.
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
- [ ] `DEC-06` 초기 budget hard limit과 source별 override 허용 범위를 부하 테스트 근거로 확정한다.
- [x] `DEC-07` Reader role, RLS, SECURITY DEFINER/INVOKER view와 함수 allowlist 정책을 확정한다.
- [ ] `DEC-08` MCP transport, 인증 경계와 HTTP application service 재사용 방식을 확정한다.
- [ ] `DEC-09` Caller identity, tenant, source authorization 정책 모델을 확정한다.

## 2. SQL Validation

Dependencies: `SQL-01`~`SQL-05`, `SQL-07`~`SQL-10`은 `DEC-01`~`DEC-03`을 따른다.
`SQL-06`은 `DEC-05`, 실행 시점의 OID 검증은 `DEC-07`과 `EXEC-13`을 따른다.

- [x] `SQL-01` PostgreSQL SQL을 AST로 parse하고 정확히 한 문장만 허용한다.
- [x] `SQL-02` `SELECT`와 허용된 read-only `WITH` 외 DDL, DML, transaction, session statement를 거부한다.
- [x] `SQL-03` AST에서 참조 relation과 schema를 추출하고 현재 source의 published catalog allowlist와 대조한다.
- [x] `SQL-04` 함수와 operator를 추출해 위험 함수와 비승인 확장을 거부한다.
- [x] `SQL-05` system schema, temp object, cross-database 접근과 client 지정 search path를 거부한다.
- [x] `SQL-06` 요청의 `metadata_revision`이 실행 직전 published revision과 같은지 검증한다.
- [x] `SQL-07` SQL을 canonicalize하고 민감 literal을 노출하지 않는 query fingerprint를 생성한다.
- [x] `SQL-08` 모든 거부 경로를 안정적인 reason code로 반환하고 parser 내부 오류를 공개하지 않는다.
- [x] `SQL-09` 허용·거부 corpus, 우회 문법, nested query, CTE와 Unicode identifier 회귀 테스트를 추가한다.
- [x] `SQL-10` Property/fuzz test로 parser failure가 실행으로 이어지지 않는 fail-closed 성질을 검증한다.

## 3. Guarded Query Execution

Dependencies: `SQL-01`~`SQL-09`, `DEC-06`

- [x] `EXEC-01` HTTP application service에 `query(source_id, sql, metadata_revision)` 계약을 구현한다.
- [ ] `EXEC-02` Caller의 source 접근 권한을 확인한 뒤 source별 concurrency slot을 획득한다.
- [x] `EXEC-03` `BEGIN READ ONLY` transaction과 transaction-local statement, lock, idle timeout을 강제한다.
- [x] `EXEC-04` Source profile의 reader identity, database와 read-only session 상태를 실행 직전에 검증한다.
- [x] `EXEC-05` 결과를 전부 메모리에 올리지 않고 stream하며 row와 UTF-8 byte 상한에서 중단한다.
- [ ] `EXEC-06` Client disconnect, deadline과 운영자 요청 시 PostgreSQL query를 cancel하고 rollback한다.
- [x] `EXEC-07` Queue timeout과 pool 고갈을 안정적인 overload reason code로 반환한다.
- [x] `EXEC-08` Optional `EXPLAIN` admission을 구현하되 planner cost만으로 안전을 보장하지 않도록 한다.
- [x] `EXEC-09` `query_id`, fingerprint, elapsed time, row/byte 수, truncation과 plan summary를 반환한다.
- [x] `EXEC-10` DB 오류, timeout, cancel과 serialization failure를 비공개 오류 계약으로 매핑한다.
- [ ] `EXEC-11` 동시성, timeout, large result, disconnect, cancel과 rollback 통합 테스트를 추가한다.
- [x] `EXEC-12` Reader가 base schema, write statement와 비승인 함수를 실행할 수 없는지 end-to-end로 검증한다.
- [x] `EXEC-13` PostgreSQL이 해석한 function/operator OID, namespace와 volatility를 검증해 AST name allowlist를 보강한다.

## 4. Metadata Quality And Revision Publishing

Metadata 응답은 구현되어 있지만 architecture의 전체 physical catalog와 immutable publish
모델은 아직 완성되지 않았다.

- [ ] `META-01` Primary key, foreign key와 index metadata를 `pg_catalog`에서 권한 범위 내 수집한다.
- [ ] `META-02` 수집 metadata가 revision과 API 응답에 포함될 범위를 정하고 정보 노출 테스트를 추가한다.
- [ ] `META-03` Wide view에서 질문 관련 column만 단계적으로 반환하는 column-scoped disclosure를 구현한다.
- [ ] `META-04` Exact phrase 중심 relevance를 대체·보완할 retrieval index와 ranking 평가 harness를 구현한다.
- [ ] `META-05` Immutable metadata snapshot과 active revision을 control plane에 저장한다.
- [ ] `META-06` Refresh 결과를 원자적으로 publish하고 이전 정상 revision으로 rollback할 수 있게 한다.
- [ ] `META-07` Verified question/SQL, 기대 relation, 기대 결과 invariant 저장 모델을 구현한다.
- [ ] `META-08` Verified query가 참조하는 revision과 현재 schema drift를 검증한다.
- [ ] `META-09` L0/L1/L2 source 품질 수준과 publish gate를 자동 판정한다.
- [ ] `META-10` Golden question precision, unsupported/clarification recall과 context byte 크기를 CI 지표로 관리한다.

## 5. MCP And Text-to-SQL Workflow

Dependencies: `EXEC-01`~`EXEC-10`, `META-05`~`META-08`, `DEC-08`

- [ ] `MCP-01` HTTP와 동일한 service를 호출하는 단일 MCP server를 구현한다.
- [ ] `MCP-02` 고정 schema의 `list_sources`, `get_context`, `query` tool을 제공한다.
- [ ] `MCP-03` MCP 요청에도 동일한 caller authorization, budget와 오류 reason code를 적용한다.
- [ ] `MCP-04` `answerability`가 `needs_clarification` 또는 `unsupported`이면 query 단계로 진행하지 않는 workflow를 검증한다.
- [ ] `MCP-05` Metadata revision mismatch 시 context를 다시 조회하고 SQL을 재생성하는 workflow를 검증한다.
- [ ] `MCP-06` Grain, fanout, composition과 business predicate를 준수하는 공통 Text-to-SQL Skill을 작성한다.
- [ ] `MCP-07` 두 MVP source의 전체 golden question을 MCP tool 호출부터 실제 결과까지 end-to-end 검증한다.
- [ ] `MCP-08` Tool schema 호환성과 응답 크기 회귀 테스트를 추가한다.

## 6. No-Deploy Source Onboarding

Dependencies: `META-05`, `META-06`

- [ ] `ONB-01` Source manifest와 secret을 검증된 control plane 입력으로 등록하는 관리자 계약을 구현한다.
- [ ] `ONB-02` 신규 source 연결, 권한, catalog, overlay와 budget을 격리된 staging 단계에서 검증한다.
- [ ] `ONB-03` 검증 성공한 source profile과 metadata revision을 원자적으로 publish한다.
- [ ] `ONB-04` Runtime이 재시작 없이 source 추가·변경·비활성화를 반영한다.
- [ ] `ONB-05` 잘못된 update가 현재 정상 source와 revision에 영향을 주지 않도록 rollback한다.
- [ ] `ONB-06` Credential rotation을 연결 중단과 secret 노출 없이 반영한다.
- [ ] `ONB-07` Manifest schema version migration과 하위 호환 정책을 구현한다.
- [ ] `ONB-08` 세 번째 fixture source를 애플리케이션 코드 변경 없이 등록하는 acceptance test를 추가한다.
- [ ] `ONB-09` L0 등록부터 L2 verified query publish까지 운영 runbook을 완성한다.

## 7. Authorization And Tenant Isolation

Dependencies: `DEC-09`, `ONB-01`

- [ ] `AUTH-01` 인증된 caller identity를 HTTP와 MCP application context에 전달한다.
- [ ] `AUTH-02` Caller/tenant별 허용 source 목록을 서버 정책으로 관리한다.
- [ ] `AUTH-03` `/sources`, `/meta`, `query`가 동일한 source authorization 결과를 사용한다.
- [ ] `AUTH-04` Tenant identity를 SQL text나 client-controlled session setting으로 주입하지 않는다.
- [ ] `AUTH-05` RLS가 필요한 source의 trusted session context 설정과 reset을 검증한다.
- [ ] `AUTH-06` Connection pool 재사용 시 tenant context가 누출되지 않는 통합 테스트를 추가한다.
- [ ] `AUTH-07` 인증·인가 실패 응답과 audit event가 source 존재 여부나 credential을 노출하지 않게 한다.

## 8. Observability And Operations

- [ ] `OPS-01` Credential, bearer token, SQL literal과 DB error detail을 제거하는 구조화 logging 정책을 구현한다.
- [ ] `OPS-02` Metadata refresh, validation reject, queue, execution, timeout, cancel과 truncation metric을 제공한다.
- [ ] `OPS-03` Source별 health/readiness를 정의하되 public health가 source inventory를 노출하지 않게 한다.
- [ ] `OPS-04` `query_id`로 application log와 PostgreSQL activity를 연계한다.
- [ ] `OPS-05` Revision publish 실패, stale 상한 초과, reject 급증과 pool 고갈 alert 기준을 정의한다.
- [ ] `OPS-06` Graceful shutdown 중 신규 query를 거부하고 실행 중 query를 제한 시간 안에 종료한다.
- [ ] `OPS-07` Migration, backup, restore와 disaster recovery runbook을 작성하고 복구 훈련을 검증한다.
- [ ] `OPS-08` Dependency, container, secret scanning과 정기 보안 업데이트 절차를 CI에 추가한다.

## 9. Release Acceptance

Dependencies: all required items above

- [ ] `REL-01` 세 개 이상의 서로 다른 PostgreSQL source가 동일한 runtime 코드 경로를 사용하는지 확인한다.
- [ ] `REL-02` 신규 source를 서비스 재배포 없이 등록하고 MCP에서 조회·실행하는 시나리오를 통과한다.
- [ ] `REL-03` 모든 golden/verified question의 relation 선택, SQL 안전성과 결과 invariant를 통과한다.
- [ ] `REL-04` 공격·오용 corpus에서 write, privilege escalation, system object와 resource limit 우회를 차단한다.
- [ ] `REL-05` 부하 테스트에서 source별 concurrency와 queue 격리, cancel과 hard limit을 확인한다.
- [ ] `REL-06` Schema drift, source 장애, stale 만료와 control plane rollback 복구 시나리오를 통과한다.
- [ ] `REL-07` 운영 dashboard, alert, audit와 runbook 검토를 완료한다.
- [ ] `REL-08` Architecture의 Success Criteria를 전부 충족하고 문서 상태를 `Production ready`로 갱신한다.

## Recommended Milestones

| Milestone | Scope | Exit |
|---|---|---|
| M1 Safe Query MVP | `DEC-*`, `SQL-*`, `EXEC-*` | HTTP에서 검증된 read-only query를 hard limit 안에서 실행한다. |
| M2 MCP MVP | `MCP-01`~`MCP-08` | 두 fixture source의 golden question을 MCP end-to-end로 통과한다. |
| M3 Published Metadata | `META-*` | Immutable revision과 verified query를 저장·publish·rollback한다. |
| M4 No-Deploy Onboarding | `ONB-*` | 세 번째 source를 코드 변경과 재배포 없이 등록한다. |
| M5 Multi-Tenant Operations | `AUTH-*`, `OPS-*` | Tenant 격리, 관측성, 복구와 운영 안전 기준을 충족한다. |
| M6 Production Acceptance | `REL-*` | 최종 성공 기준과 공격·장애·부하 시나리오를 모두 통과한다. |

M1과 M2는 현재 MVP의 직접적인 다음 단계다. Control plane 기반인 M3과 M4는
설계는 함께 진행할 수 있지만, query 실행 안전 경계를 우회하지 않도록 M1 계약을 먼저
고정한다.
