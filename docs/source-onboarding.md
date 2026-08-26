# Source Onboarding

## Boundary

신규 PostgreSQL source 추가는 runtime 코드 변경 없이 끝나야 한다. 다음 두 종류의
metadata를 분리한다.

자동 수집:

- reader가 `SELECT`할 수 있는 relation과 column
- data type, relation kind와 database comment
- 권한 범위 안의 primary key, foreign key와 단순 non-partial index key
- view definition의 revision hash 재료

사람이 선언:

- source 설명과 credential 환경 변수 이름
- 허용 schema와 relation kind, budget profile
- relation의 role, grain과 대표 시간 column
- 물리 이름만으로 알 수 없는 업무 alias
- 검증된 join key, cardinality와 fanout guidance
- source별 business predicate, value hint와 measure aggregation
- 지원 불가능하거나 정의 확인이 필요한 질문 규칙
- 여러 grain을 각각 집계한 뒤 결합해야 하는 검증된 composition

Table 전체를 Python 객체로 다시 모델링하거나 `COMMENT` 문장에서 join 규칙을
추출하지 않는다.

Source manifest는 임의 host로 연결할 수 있는 운영 설정이므로 application code와 같은
trust boundary에서 review·publish한다. 낮은 권한의 self-service 입력으로 직접 받지 않는다.
Codex가 이 절차를 반복 가능하게 안내할 때는 repository의
[`query-man-source-onboarding`](../skills/query-man-source-onboarding/SKILL.md) Skill을 사용한다.
[source onboarding Skill plan](source-onboarding-skill-plan.md)과
[acceptance evidence](verification/2026-08-25-source-onboarding-skill.md)가 책임·권한·검증 gate를
기록한다. Skill은 plan-only이며 이 문서의 절차와 server-side validation이 계속 현재 기준이다.

## Source Authority And Artifacts

Production hot-added source의 canonical manifest generation, active/deactivated state, metadata
snapshot과 verified-query record는 Control DB가 authority다. Publish가 `config/sources` YAML, Git
commit이나 PR을 만들지 않으며 Control DB와 repository를 양방향 동기화하지 않는다. 한곳에서
ownership, state, history, size와 cost를 조회하는 management 목표와 범위는
[source management plane](source-management-plane.md)을 따른다.

| Artifact | Authority and location |
|---|---|
| Canonical source generation and active state | Control DB |
| Metadata snapshot and hot-added verified-query records | Control DB |
| Encrypted reader credential | Control DB; plaintext/master key 제외 |
| Plaintext credential and master key | External secret system/runtime secret |
| Curated view, reader role and grants | Source DB and DB-owner migration system |
| Budget hard-limit template/resource-tier catalog and current bootstrap/access policy | Deployment configuration |
| Owner, environment and DB migration reference | Control DB immutable manifest generation |
| Mutation audit and authoritative receipt | Control DB management plane; implemented |
| Replica desired/applied drift and freshness | Control DB latest observation; implemented |
| Size/growth observation | Control DB current/previous와 latest-attempt; operator projection implemented |
| Gateway usage/cost projection | Operator usage/lower-bound status implemented; provider monetary cost remains not configured |
| Bootstrap and acceptance input | Repository YAML seed/fixture only |

`config/sources/*.yaml`은 local/CI bootstrap seed이고 `config/onboarding/*.yaml`은 integration
fixture다. Production managed mode의 desired-state backup이 아니다. Managed runtime은 source와
verified file을 열지 않으며 Control DB lifecycle이 없는 file source를 등록된 source로 간주하지
않는다.

## Runtime Mode And Existing-Source Import

`QUERY_MAN_SOURCE_MODE`는 process 전체에서 다음 두 값만 사용한다.

| Mode | Required/forbidden settings | Purpose |
|---|---|---|
| `bootstrap` (default) | Control DSN/key 금지; anonymous/query-only token 또는 v2 policy | Local/CI repository fixture |
| `managed` | Control DSN/key, stable replica ID와 v2 access policy 필수; API token/anonymous 금지 | Production Control DB authority와 hot-add |

Managed mode에는 source별 file fallback이나 filesystem verified-query dataset과 Control DB record의 merge가 없다. Budget profile과 access
policy는 계속 deployment configuration에서 읽는다. Source/verified directory는 없어도 되지만
budget file과 runtime secret은 있어야 한다.

이미 bootstrap으로 사용하던 source를 production Control DB로 이관할 때는 다음 순서를 한 번만
수행한다.

1. Control schema, 전용 writer, encryption key, 각 deployment slot의 stable replica ID와 version 2
   query/admin access policy를 준비한다. 동시에 실행되는 slot은 ID를 공유하지 않는다.
2. Serving traffic 밖에서 managed runtime을 시작한다. 아직 active source가 없으면 `/ready` 503이
   정상이며 admin endpoint는 별도 운영 경로로 호출한다.
3. Existing manifest의 `minimum_quality_level`을 L0/L1로 두고 기존 `PUT /admin/sources/{source_id}`로
   stage/publish한다. 새 UUID, change reference와 expected generation/state를 header에 넣는다. 첫
   Control publish는 `0/0`, 이후 publish는 직전에 조회한 현재값을 쓴다. 응답 receipt의 resulting
   state와 metadata revision을 확인한다.
4. File의 reviewed verified query를 현재 revision에 맞춰
   `POST /admin/sources/{source_id}/verified-queries`로 실행·저장한다. Revision이나 result invariant가
   다르면 자동 보정하지 않고 이관을 중단한다.
5. Semantic/budget 내용은 유지하고 `minimum_quality_level: L2`로 publish한다. Quality minimum은
   revision hash 재료가 아니므로 같은 revision의 verified-query baseline을 사용할 수 있다.
6. 필요한 source와 verified-query coverage, intended active/deactivated state를 Control DB에서 확인하고 serving
   replica를 `QUERY_MAN_SOURCE_MODE=managed`와 slot별 `QUERY_MAN_REPLICA_ID`로 재시작한다. File을
   제거하거나 같은 ID의 seed를 남겨도 inventory, rollback과 deactivate 결과가 바뀌지 않는지
   확인한다.
7. 각 source의 `GET /admin/sources/{source_id}/replicas`에서 시작한 모든 slot이 `available`이고
   desired/applied `drift`가 비었는지 확인한 뒤 traffic을 전환한다.

이 절차는 기존 staged admin API만 사용한다. Startup importer, bulk import endpoint, source별
mode/origin, seed digest/marker와 repository write-back은 없다. Plaintext credential은 관리자가
external secret system에서 직접 가져와 trusted manual-admin request에만 전달한다. 각 mutation은
`Idempotency-Key`, `X-Query-Man-Reason`, `X-Expected-Generation`,
`X-Expected-State-Version`을 요구하고 metadata resume만 현재 pinned revision을
`X-Expected-Metadata-Revision`으로 추가한다. Timeout 뒤에는 key로 receipt를 조회하고 source
generation/state를 대조한 뒤에만 같은 key로 재시도한다. 새 key로 blind retry하지 않는다. 상세
절차는 [source management plane](source-management-plane.md#timeout-reconciliation)을 따른다.

## Steps

1. 조회 목적에 맞는 `ai` view를 만든다. 한 view의 grain은 하나로 고정한다.
2. 별도 LOGIN reader에 해당 view의 `USAGE`와 `SELECT`만 부여한다.
3. Reader에 read-only, timeout, temp resource와 replica/pool capacity에 맞춘 connection
   limit을 설정한다.
4. 기존 `budget_profile` 중 workload에 맞는 resource tier 하나를 선택한다.
5. Owner, environment, DB migration reference와 resource tier를 명시한 L0 manifest 및 reader
   credential을 admin 전용 source management 경계에 전달한다.
6. 격리 staging에서 connection, reader identity, catalog, overlay, budget과 quality gate 결과를 확인한다.
7. Canonical manifest, generation, active pointer와 metadata snapshot을 Control DB에 publish하고
   응답의 generation과 metadata revision을 변경 기록에 남긴다. Runtime 재시작은 필요 없다.
8. 서로 다른 query identity로 같은 `/sources`를 보는지 검증한다. Integration 검증은 caller
   override 없이 같은 source-resolved budget 정의가 적용되는지 확인하고, admin 기록에는
   선택한 `budget_profile`과 관련 metadata revision을 남긴다.

[ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)에 따라 admin이
source를 활성화하면 모든 인증된 query principal에게 동시에 공개된다. 별도 caller grant나
재시작은 없다. 한 source의 모든 사용자는 manifest가 선택한 같은 `budget_profile` 정의를
쓰며 query 사용자가 이를 바꾸지 못한다. Stable caller/tenant identity는 audit와 source-native
RLS에만 남는다.

Access-policy version 2에는 source scope가 없다. Caller는 `caller_id`, `tenant_id`, token 환경 변수와
`operator`만 선언하며 version 1, `allowed_sources`와 `all_sources`가 남은 policy는 startup에서
fail-closed한다. Managed mode는 최소 한 개의 non-admin query identity와 explicit operator admin
identity를 모두 요구한다. 이 cutover는 Control DB migration이나 dependency를 추가하지 않는다.

최소 manifest는 semantic overlay 없이도 동작한다. 아래 YAML은 API manifest document를 읽기
쉽게 표현한 예이며 production repository file을 요구한다는 의미가 아니다.

```yaml
version: 2
source_id: example-source
name: 예제 Source
description: 예제 분석 데이터
provenance:
  owner: data-platform
  environment: production
  database_migration_ref: flyway:2026.08.23.1
connection:
  host: 127.0.0.1
  host_env: QUERY_MAN_POSTGRES_HOST # Optional deployment-time override.
  port: 5432
  database: example_database
  user: example_reader
  password_env: EXAMPLE_SOURCE_READER_PASSWORD
  ssl: false
allowed_schemas: [ai]
allowed_relation_kinds: [view]
budget_profile: interactive
```

이 상태는 L0 best-effort 검색이다. 한국어 질문, grain과 비표준 join이 필요할 때만
`semantic_overlay`를 추가한다.

Mutation endpoint는 explicit Query Man admin의 `operator` capability만 사용할 수 있다. 일반 query
credential은 모든 admin endpoint와 cancel에서 거부되며 managed mode는 anonymous local caller를
허용하지 않는다. Operator는 별도 role hierarchy가 아니라 query 권한에 관리 권한을 추가하는
capability superset이다.

| Operation | Endpoint | 안전 조건 |
|---|---|---|
| Inventory | `GET /admin/sources` | Exact filter와 bounded keyset page; 비활성 source도 기본 포함 |
| Effective detail | `GET /admin/sources/{source_id}` | 현재 generation과 active metadata pointer를 구분한 secret-free projection |
| Generation history | `GET /admin/sources/{source_id}/history` | 불변 generation을 내림차순으로 조회; lifecycle event audit은 별도 |
| Mutation history | `GET /admin/sources/{source_id}/mutations` | Actor/change reference와 expected/resulting state의 secret-free chronology |
| Replica convergence | `GET /admin/sources/{source_id}/replicas` | Ever-registered slot별 desired/applied drift와 DB-clock freshness; stale도 200 |
| Resource and usage | `GET /admin/sources/{source_id}/usage` | Query 없이 latest resource attempt/last-success와 inclusive 31일 gateway lower-bound 조회; missing은 0이 아님 |
| Receipt lookup | `GET /admin/mutations/{idempotency_key}` | Timeout 뒤 terminal success/rejection을 authoritative하게 조회 |
| Stage + publish | `PUT /admin/sources/{source_id}` | Body의 `manifest`, `credential`을 분리하고 path ID 일치 검증 |
| Credential rotation | `POST /admin/sources/{source_id}/credential` | Enabled/unpinned source에서 새 credential staging 후 generation 교체 |
| Verified-query record publish | `POST /admin/sources/{source_id}/verified-queries` | 현재 revision에서 guarded SQL 결과와 expected invariant 일치 |
| Rollback | `POST /admin/sources/{source_id}/rollback/{generation}` | 대상 profile, secret, metadata와 quality를 먼저 재검증 |
| Resume metadata publish | `POST /admin/sources/{source_id}/metadata/resume` | Rollback 점검 완료 후 metadata pin만 명시적으로 해제 |
| Deactivate | `DELETE /admin/sources/{source_id}` | Active pointer만 disable하고 immutable history 유지 |

Public `/sources`는 모든 인증 query caller가 공유하는 active source 목록일 뿐 관리 catalog가
아니다. Admin read API는 raw manifest 대신 명시적으로 허용한 필드만 반환하며 credential,
secret locator, semantic 자유형 text, verified question/SQL과 metadata snapshot을 포함하지 않는다.
Generation history는 immutable manifest 생성 순서이고 mutation history는 publish, credential
rotation, verified-query publish, rollback, metadata resume와 deactivate의 append-only chronology다.
Request hash는 keyed HMAC이라 payload를 복원할 수 없고 response/audit에는 credential, manifest,
question과 SQL이 없다.

현재 direct publish의 body credential은 trusted manual-admin 경계다. Plan-only onboarding
Skill은 이 endpoint를 호출하거나 credential을 읽지 않는다. AI production executor가 실제
요구가 되면 target-bound credential broker와 plan-ID apply를 별도 threat model/ADR 뒤에
설계한다.

Admin API는 TLS 뒤에서만 노출하고 request body를 access log에 기록하지 않는다. Credential은
응답, manifest JSON과 metadata에 포함되지 않으며 `QUERY_MAN_SOURCE_ENCRYPTION_KEY`로
AES-256-GCM 암호화되어 control DB에 저장된다. 이 key는 padding을 포함한 URL-safe Base64로
표현한 32 decoded bytes(일반적으로 44 characters)여야 하고 `QUERY_MAN_CONTROL_DSN`과 함께
모든 runtime replica에 secret으로 배포한다. 현재는
단일 direct key 형식이므로 환경 변수만 바꾸는 online master-key rotation을 지원하지 않는다.
Backup과 변경 경계는 [disaster recovery](disaster-recovery.md)를 따른다.

```yaml
semantic_overlay:
  default_relation: ai.case_overview
  relations:
    - relation: ai.case_overview
      role: event
      aliases: [고객 접수, 시장 불량]
      grain:
        name: customer_case
        description: 고객 접수 1건
        key_columns: [case_id]
      default_time_column: received_at
      use_for: [월별 접수 건수, 미해결 접수]
  joins: []
```

## Publish Checks

Registry와 metadata refresh는 다음 조건을 만족하지 않으면 source를 publish하지 않는다.

- 중복되지 않은 opaque `source_id`
- 존재하는 budget profile과 secret 환경 변수
- overlay relation이 allowed schema 안에 있음
- Reader 세션의 database, session user와 read-only 상태가 profile과 일치함
- Reader transaction의 첫 settings statement가 local `TimeZone=UTC`를 설정하고 공통 probe가
  catalog/query 전에 이를 확인함; role/database default는 변경하지 않음
- Reader 세션의 work memory, temporary file, parallel worker와 JIT가 budget과 정확히 일치함
- Grain key, 대표 시간 column과 column alias 대상이 실제 catalog에 존재함
- 승인된 join 양쪽 column이 존재하고 data type이 같음
- Relation/column 수와 metadata response가 budget 상한 안에 있음
- `observability`를 구성했다면 representative relation과 1~16개 storage relation이 같은 DB의
  non-system ordinary table/materialized view이고 representative relation이 목록에 포함됨

Manifest의 `minimum_quality_level`은 publish 가능한 최소 수준을 선언한다. `L0`는 물리
catalog 기반 best-effort, `L1`은 모든 공개 relation의 설명·grain과 시간 역할이 완성된
상태, `L2`는 현재 metadata revision과 일치하는 verified-query record 검증까지 통과한 상태다.
요구 수준에 미달한 refresh나 rollback은 거부되며 기존 active revision은 유지된다.

Manifest v2의 provenance에는 운영 팀 slug, source DB environment와 외부 DB migration/change
reference를 모두 명시한다. Query Man은 migration reference가 가리키는 외부 artifact를 대신
검증하지 않는다. Owner나 migration reference 변경은 새 immutable generation을 만들지만 query
metadata revision은 바꾸지 않는다. 누락 값을 `unknown`으로 자동 채우거나 이전 manifest version을
runtime에서 변환하지 않는다.

Optional `observability`는 대표 record grain과 physical relation, 그리고 그 relation을 포함한
1~16개 storage relation을 DB owner가 명시할 때만 사용한다. 이는 query allowlist나 public relation을
넓히지 않으며 ordinary table/materialized view만 허용한다. Definition 변경은 새 source generation을
만들지만 metadata revision은 바꾸지 않는다. 값이 없으면 resource observation 미구성으로 유지하고
임의 `COUNT(*)`, predicate나 SQL expression으로 대신하지 않는다.

Bootstrap manifest의 선택적 `host_env`와 `port_env`는 host/Compose처럼 deployment network가
다를 때만 사용한다. 환경변수가 없으면 manifest의 host와 port를 사용한다. Control-plane
publish는 publisher 환경에서 실제 host와 port를 한 번 resolve해 저장하므로 다른 replica가
자신의 환경 변수로 published endpoint를 바꾸지 않는다. 같은 `source_id`의 host, port,
database, user, TLS 설정과 environment는 이후 generation에서도 고정한다. 다른 endpoint나
environment는 새 source ID로 등록하고 현재 데이터에 대한 verified-query baseline을 다시 검토한다.
Credential 값만 바꾸는 rotation은 metadata revision을 바꾸지 않는다.

Reader staging은 login/non-superuser, 생성·복제·상속·RLS 우회 금지, 유한한 양수 connection
limit, default read-only, database TEMP 금지와 공개 schema CREATE 금지를 검사한다. 숨긴 base
schema의 전체 권한과 network/TLS 설정은 source owner도 별도 검토해야 하며, 반복 절차는
[`source-extension-checklist.md`](source-extension-checklist.md)에 있다.

Connection capacity는 `replica 수 × (query pool + metadata pool) + 동시 staging`으로
계산한다. 현재 fixture는 `2 × (2 + 1) + 1 = 7`이다. Replica나 pool을 늘릴 때 role
connection limit와 database 전체 connection 여유를 함께 다시 산정한다.

Catalog refresh가 일시 실패하면 제한된 stale 기간 동안 마지막 정상 revision을
`stale` 상태와 함께 반환하고 backoff 후 다시 시도한다. 정상 snapshot이 한 번도
없거나 stale 상한을 넘었거나 overlay가 현재 schema와 맞지 않으면 `/meta`는
`503 METADATA_UNAVAILABLE`을 반환한다. 권한 회수나 schema drift는 stale fallback을
사용하지 않는다.

Stale age는 process cache를 채운 시점이 아니라 control DB active metadata의 마지막 정상
activation 시점에서 계산한다. 재시작은 TTL/stale window를 초기화하지 않는다. 같은 revision을
정상 재검증하거나 현재 정책으로 rollback하면 age가 갱신된다. Resume만으로는 갱신되지 않고
다음 metadata 접근이 즉시 refresh하도록 cache 만료를 예약한다. Pin과 다른 candidate가
반복되면 기존 active age가 계속 증가해 상한 뒤 unavailable이 된다.

40개를 넘는 wide relation은 질문과 직접 관련된 column, grain/time key, measure,
business predicate와 approved join key를 우선 반환한다. `columns_truncated=true`는 전체
catalog가 사라졌다는 뜻이 아니라 question context에서 일부 column을 생략했다는 뜻이다.
이는 SQL column authorization 경계가 아니므로 민감 column은 curated view 자체에서
제거해야 한다.

Production managed mode에서는 target database/schema/control object owner 권한과
`query_man_control_writer`를 create·alter하고 남은 membership을 회수할 cluster role 관리 권한을
모두 가진 migration identity가 표준 libpq `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
`PGPASSFILE`/managed-auth 환경을 설정하고 다음을 실행한다. Database owner 권한만으로는 충분하지
않다. Password가 든 DSN을 command argument에 넣지 않는다.

```bash
./scripts/apply-control-schema.sh
```

이 script는 현재 `PGDATABASE`에 checksum이 일치하는 pending numbered migration만 적용하고,
`query_man_control_writer` NOLOGIN group과 최소 ACL을 매번 reconcile한다. 이미 적용된 migration
file은 수정하지 않고 schema 변경마다 새 번호를 추가한다. 네 fixture database·role·seed를
만드는 `scripts/apply-db.sh`는 production migration에 사용하지 않는다.
전용 LOGIN은 database/IAM 운영 절차로 별도 생성하고 `query_man_control_writer` membership만
부여한다. LOGIN은 `INHERIT`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`,
`NOBYPASSRLS`와 유한 connection limit를 사용한다. Metadata/source store pool이 각각 replica당
최대 2개이므로 limit는 최소 `runtime replica 수 × 4`의 의도된 capacity와 운영 여유를
명시적으로 산정한다. Runtime에는 `QUERY_MAN_SOURCE_MODE=managed`, 이 LOGIN의 TLS DSN, source
encryption key와 `QUERY_MAN_REPLICA_ID`를 함께 설정한다. Replica ID는 1~80자의 lowercase stable
slug이며 같은 deployment slot이 재시작할 때 재사용하고 동시에 실행되는 slot 사이에는 고유해야
한다. DSN/key/ID 중 하나가 빠지거나 bootstrap mode와 Control 설정을 함께 사용하면 startup이
실패한다. Bootstrap은 replica ID가 있어도 검증하지 않는다.

정상 refresh는 immutable snapshot과 active pointer를 원자적으로 publish한다. Rollback한
source는 pin되므로 이후 refresh가 active revision을 덮지 않으며, 검증 후 automatic publish를
명시적으로 resume해야 한다. Control-plane DSN이나 snapshot payload는 metadata/MCP 응답에
포함되지 않는다.

Control-plane source 변경은 `QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS` 주기로 다른 replica에도
반영된다. 각 replica는 encrypted credential, manifest, stored metadata revision과 quality를
다시 검증한 후 registry를 교체한다. 검증 실패 시 해당 replica의 현재 정상 generation을
유지한다. Generation은 immutable profile 번호이고 rollback으로 낮아질 수 있으므로 poller는
모든 pointer 전이에서 증가하는 `state_version`으로 순서를 판정한다. Older state와 같은
version의 충돌 payload는 적용하지 않는다.

Managed runtime은 실제 report cadence인
`max(QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS, 5000)`ms로 latest observation을 best-effort 보고한다.
Operator는 replica endpoint에서 source별 desired/applied generation, state version, metadata
revision과 freshness를 확인한다. 한 번 등록된 slot은 자동 삭제·retirement되지 않고 scale-down
뒤 stale로 남으며, 아직 한 번도 시작하지 않은 planned slot은 deployment inventory에서 확인한다.
Observation 실패는 registry, query, readiness나 mutation receipt를 바꾸지 않는다.

## L0 to L2 Promotion Runbook

1. Semantic overlay가 없는 manifest를 `minimum_quality_level: L0`로 publish한다. `/meta`에서
   reader-visible relation과 column이 의도한 범위인지 확인한다.
2. 모든 공개 relation에 description, grain과 event/comment/population의 default time을
   작성한다. Timestamp가 business calendar 경계에 쓰이면 timezone도 명시한다. Verified SQL은
   timestamp 범위를 `>= start AND < next_boundary`로 만든다. 현재 별도 timezone 필드는 없으므로
   `timestamptz`의 business timezone은 선택되는 relation 또는 column description에 검토된 IANA
   이름으로 명시하고, 없으면 query 생성기가 추측하지 않게 한다. Calendar bucket에도 같은
   timezone을 적용한다. Minimum을 L1으로 설정해 publish하고 새 `metadata_revision`을 기록한다.
3. 실제 사용자 질문과 deterministic SQL을 준비한다. SQL의 expected relation, ordered
   columns, row count와 canonical result hash를 별도 review한다. Aware datetime은 UTC `+00:00`,
   naive datetime/date/time/timetz는 저장된 ISO 표현을 사용한다.
4. 현재 L1 revision을 포함한 verified-query input을 verified-query admin endpoint에 제출한다. Gateway
   budget, AST/object policy, 결과 invariant 중 하나라도 실패하면 record는 저장되지 않는다.
5. Semantic overlay는 그대로 두고 `minimum_quality_level`만 L2로 바꿔 다시 publish한다.
   Quality minimum은 revision hash 재료가 아니므로 2단계와 같은 revision이 L2 gate를 통과한다.
   반면 source profile의 execution budget과 revision-scoped policy 변경은 revision 재료이므로
   새 revision에서 verified query를 다시 실행·승인해야 한다.
   Canonical-time/SQL policy처럼 모든 metadata revision이 바뀌는 전환은 current와 rollback-preserved
   verified-query baseline 전체를 새 revision에서 재실행·재발행하며 이전 immutable row를 삭제하지 않는다.
6. `/meta`와 MCP `get_context`의 `quality_level=L2`, 실제 query 결과, `/sources` visibility를
   확인한다. Replica endpoint에서 다른 replica도 report cadence 뒤 같은 generation/state/metadata
   revision으로 `available`, `drift=[]`인지 확인한다.
7. 문제가 있으면 마지막 정상 source generation으로 rollback한다. Rollback 대상의 encrypted
   Rollback 대상의 encrypted credential, manifest, metadata와 그 대상 metadata revision에 대응하는
   verified-query record가 먼저 재검증되므로 실패한 복구가
   active pointer를 바꾸지 않는다. 원인 점검과 현재 source 재검증을 마친 뒤에만
   `POST /admin/sources/{source_id}/metadata/resume`으로 automatic metadata publish를 재개한다.

Repository fixture에서는 L0
[`support-tickets.yaml`](../config/onboarding/support-tickets.yaml), semantic/L2
[`support-tickets-l2.yaml`](../config/onboarding/support-tickets-l2.yaml), reviewed invariant
[`support-tickets-verified-query.yaml`](../config/onboarding/support-tickets-verified-query.yaml)을
이 순서로 사용한다.

Quoted identifier와 rich type 코너 케이스는 `commerce-edges` fixture의 L0
[`commerce-edges.yaml`](../config/onboarding/commerce-edges.yaml), semantic/L2
[`commerce-edges-l2.yaml`](../config/onboarding/commerce-edges-l2.yaml), reviewed invariant
[`commerce-edges-verified-query.yaml`](../config/onboarding/commerce-edges-verified-query.yaml)을
사용한다. Manifest의 canonical name과 실제 SQL identifier가 다를 수 있으므로 MCP client는
relation/column의 `sql_name`을 사용해야 한다.

## Security Checks

- Metadata request에는 `source_id`, `question`, `max_objects` 외 필드를 허용하지 않는다.
- Loopback 밖에 HTTP를 bind할 때는 bearer token 없이는 process가 시작되지 않는다.
- Metadata catalog는 allowed schema, relation kind, schema/table/column 권한을 모두 확인한다.
- Foreign key의 양쪽 relation/column이 모두 허용된 경우에만 공개하고 physical key를
  approved semantic join으로 자동 승격하지 않는다.
- System schema는 manifest 단계에서 거부하고 base/temp schema와 `pg_stat_statements`는 API에 노출하지 않는다.
- `password_env`는 `<SOURCE_ID>_READER_PASSWORD` 형식의 source-scoped secret만 허용한다.
- Reader와 gateway transaction의 effective search path는 `pg_catalog` 하나로 고정한다.
  Query relation은 항상 schema-qualified SQL name을 사용한다.
- DB comment는 instruction이 아니라 길이가 제한된 description data로 취급한다.
- Production connection은 TLS certificate 검증을 사용한다.
- `tenant_isolation: rls` source는 `view`만 허용하고 모든 공개 view가
  `security_invoker=true`여야 한다. Reader는 `NOBYPASSRLS`여야 하며 policy는 transaction-local
  `query_man.tenant_id`를 사용한다.
- 위 검사는 hidden base relation의 RLS flag/policy drift까지 증명하지 못한다. 현재
  [RLS policy drift finding](verification/2026-08-26-rls-policy-drift.md)의 `RLS-01`~`RLS-03`이 열려
  있으므로 RLS source의 production publish와 route activation은 보류한다. Non-RLS source onboarding은
  이 보류 대상이 아니다.
- [Proposed ADR 0024](decisions/0024-rls-policy-drift-attestation.md)의 `RLS-01-A` target은 RLS Catalog/
  Query connection startup client UTF8과 PostgreSQL-18/server/client UTF8 admission도 요구한다. 이는
  아직 승인·구현된 RLS onboarding/admission 정책이 아니므로 현재는 위 all-RLS publish/route hold만 유지한다.
  A가 exact 승인되면 read-only inventory가 non-UTF8 RLS database를 stop condition으로 보고하고,
  별도 권한과 승인을 받은 operator만 unroute/deactivate 또는 source별 DB-owner data migration/
  re-onboarding plan과 rollback을 실행한다. Plan-only onboarding Skill은 mutation하지 않는다.
