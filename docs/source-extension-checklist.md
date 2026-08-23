# Source Extension Checklist

## 목적

신규 PostgreSQL source를 추가할 때 애플리케이션 runtime 분기와 재배포를 늘리지 않고,
보안·비용·MCP 결과 정합성을 유지하기 위한 운영 checklist다. Source별 차이는 curated
view, versioned manifest, 기존 `budget_profile` resource tier와 verified query로 표현한다.
Production onboarding은 `QUERY_MAN_SOURCE_MODE=managed`의 Control DB authority에서 수행한다.
Repository source/verified file은 bootstrap/acceptance fixture이며 production publish 산출물이 아니다.
Managed runtime은 source scope가 없는 version 2 access policy와 explicit query/admin identity를
요구한다. Version 1과 legacy scope field는 자동 확대 없이 startup에서 거부한다.

## 항상 필요한 작업

- [ ] 질문에 필요한 column만 포함하고 grain이 하나인 curated `ai` relation을 만든다.
- [ ] 전용 LOGIN reader를 만들고 `CONNECT`, 공개 schema `USAGE`, curated relation
  `SELECT`만 부여한다. PUBLIC/TEMP/CREATE/base relation write와 불필요한 교차 DB 접근을
  회수한다.
- [ ] Reader를 `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`,
  `NOREPLICATION`, `NOBYPASSRLS`,
  `replicas × (query pool + metadata pool) + staging`에 맞는 connection limit,
  default read-only, statement/transaction/lock timeout과 temp/work memory 상한으로 고정한다.
- [ ] 기존 workload에 맞는 `budget_profile` 하나를 resource tier로 선택하고 실제 대표
  query의 plan cost, rows, node 수, 실행 시간과 결과 byte를 측정한다. 같은 source의 모든
  query 사용자가 이 profile을 공유한다.
- [ ] Source-scoped secret 이름과 L0 manifest를 준비해 admin API에서 stage/publish하고
  generation과 metadata revision을 기록한다. 같은 `source_id`는 host, port, database,
  user 또는 TLS mode가 다른 endpoint로 다시 묶지 않는다.
- [ ] 활성화 시 모든 인증된 query 사용자가 source를 본다는 영향을 확인한다. 서로 다른 두
  query identity가 같은 source 목록을 보고, caller override 없이 같은 source-resolved budget
  정의가 적용되며, admin API는 모두 거부되는지 검증한다. Admin 기록에는 선택한
  `budget_profile`과 metadata revision을 남긴다.
- [ ] HTTP와 MCP 양쪽에서 `list_sources`, question-scoped context, exact revision query,
  ordered columns/rows와 canonical result hash를 smoke test한다.
- [ ] Source health, reject/timeout/queue/truncation metric, owner, credential rotation과 rollback
  절차를 운영 기록에 연결한다.

## 조건에 따라 필요한 작업

| 조건 | 추가 작업 |
|---|---|
| Production 품질을 L1/L2로 관리 | 모든 공개 relation의 description/grain/time 역할, approved join·business term과 현재 revision의 reviewed verified query를 등록한다. |
| Join 또는 여러 grain 사용 | Cardinality/fanout guidance를 선언하고 각 grain 선집계 후 결합하는 query와 zero-child 데이터를 검증한다. |
| RLS source | `FORCE ROW LEVEL SECURITY`, `security_invoker` view, trusted tenant context와 pool 재사용 격리를 검증한다. |
| 기존 profile로 자원 상한을 만족하지 못함 | 별도 platform review 후 `budget_profile`을 변경한다. Source별 임의 숫자나 `cost_tier`를 추가하지 않는다. Budget 파일은 startup 설정이므로 현재는 restart가 필요하고, 새 metadata revision에서 L2 verified query를 다시 승인한다. |
| Wide relation 또는 큰 결과 | 질문별 column disclosure, context/result byte, row limit과 truncation UX를 측정한다. |
| Quoted PostgreSQL identifier | Manifest에는 `Identifier`/`schema.relation` 규칙을 만족하는 canonical 이름을 쓰고 metadata의 `sql_name`을 SQL에 그대로 사용한다. 공백·Unicode identifier는 curated view에서 안전한 이름으로 바꾼다. |
| Production network | TLS certificate 검증, 방화벽/allowlist와 replica별 target DB connectivity를 확인한다. |

## 보통 필요하지 않은 작업

- [ ] Python의 source ID 분기, 새 endpoint, registry factory 또는 dependency 추가
- [ ] 기존 workload와 같은 경우 새 budget profile 추가
- [ ] Table 전체를 application model로 복제하거나 database comment를 instruction으로 해석
- [ ] L0 탐색만 필요한 source에 불필요한 semantic overlay 추가

Repository의 fixture DB/role/seed와 `scripts/apply-db.sh` 변경은 재현 가능한 acceptance 환경을
만들기 위한 작업이다. 실제 운영 DB가 이미 준비되어 있다면 이 fixture 변경은 production
onboarding 절차에 포함되지 않는다.

## MCP 정합성 경계

Gateway가 강제하는 것은 authenticated shared source access, metadata revision, SQL AST와 resolved
object policy, plan/시간/concurrency/result 상한, 중복 결과 column 거부와 bounded JSON
응답이다. MCP도 같은 service와 오류 code를 사용하고 추가 입력 및 예상 밖 내부 오류를
fail-closed한다.

Canonical result에서 `numeric`은 scale을 보존한 문자열, `bytea`는 `base64:` 문자열이다.
Fixture expected row와 hash도 HTTP framework의 임의 coercion이 아니라 이 계약을 사용한다.

질문에서 SQL을 생성하는 과정, `unsupported`/`needs_clarification`에서 멈추는 판단과 반환된
context의 의미를 SQL이 올바르게 반영했는지는 client/Skill 책임이다. Production 회귀에서는
reviewed verified query와 canonical result hash로 이 의미 경계를 별도로 증명한다.
