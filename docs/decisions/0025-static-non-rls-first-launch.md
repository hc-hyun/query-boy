# ADR 0025: Static Non-RLS First Launch

Status: Accepted — repository implementation in progress; protected execution separately gated

Date: 2026-08-26

Decision ID: `LAUNCH-01-A`

## Context

Query Man에는 managed source lifecycle, RLS tenant context와 넓은 PostgreSQL result encoding이
구현돼 있다. 그러나 현재 증거는 다음 범위까지만 첫 운영에 충분하다.

- repository가 관리하는 `development-issues`, `market-voc` 두 bootstrap source
- PostgreSQL 18.6, server/client UTF-8
- non-RLS curated view와 9개 verified query
- 단일 process replica의 기존 authentication, SQL validation, reader limit와 cancel/rollback

RLS base-policy drift는 같은 metadata revision 아래 cross-tenant 결과를 만들 수 있다. Default
driver는 여러 PostgreSQL type을 Python `str`, `int`, `list` 등으로 평탄화해 unsupported type이
허용된 scalar처럼 우연히 성공할 수 있다. 전체 RLS attestation, lossless encoding, managed hot
onboarding과 HA를 한 번에 완료하면 첫 오픈 범위와 일정이 불필요하게 커진다.

이 결정은 일반적인 production 완성을 주장하지 않는다. 현재 두 source를 단일 호스트에서
제한적으로 제공할 repository launch profile을 정하고, 실제 TLS·secret·backup·route·배포는
대상 환경과 change record를 갖춘 별도 실행 승인으로 남긴다.

## Decision

### 1. Source authority and composition

- `bootstrap`을 현재 두 reviewed source의 static first-launch authority로 허용한다.
- Process는 bootstrap과 managed authority를 섞거나 서로 fallback하지 않는다.
- Static launch에는 Control DB, admin mutation, hot onboarding과 runtime source reload가 참여하지
  않는다. 구현된 managed mode는 보존하며 동적 source 운영이 필요할 때 별도로 활성화한다.
- Source ID, manifest, budget/access policy 또는 database를 추가·교체하는 것은 이 결정 밖의 새
  inventory review와 배포다.
- 첫 launch topology는 단일 Query Man replica다. `soak` profile과 두 번째 replica는 acceptance
  fixture이며 serving topology가 아니다.

이 범위는 ADR 0016의 “bootstrap은 local/CI 전용이고 production은 managed” 결정에 대한 정확히
한정된 예외다. Managed authority의 persistence, mutation과 recovery 의미는 바꾸지 않는다.

### 2. RLS quarantine

`tenant_isolation=rls` shape, `TenantIsolation`, historical Control row와 기존 implementation은
삭제하지 않는다. 하지만 launch serving admission은 모든 RLS source를 차단한다.

| Path | Required outcome |
|---|---|
| Bootstrap manifest | `RegistryConfigurationError`; listener 생성 전 startup 실패 |
| Injected bootstrap registry | Runtime composition 실패 |
| Managed publish/rotate | 기존 `400 SOURCE_VALIDATION_FAILED`; generation/staging 없음 |
| Managed cold stored record | `RUNTIME_VALIDATION_REJECTED`; registry에 projection하지 않음 |
| QueryService 또는 executor 우회 | tenant 유무와 무관한 details 없는 `503 QUERY_UNAVAILABLE` |

Delivery authentication/authorization와 source existence는 기존 순서로 먼저 처리한다. 존재하는
RLS source의 quarantine는 tenant-required, revision, SQL validation, queue와 database access보다
먼저다. Full RLS serving은 별도 attestation 결정과 migration/cutover가 승인·검증된 뒤에만 다시
열 수 있다.

### 3. Reader connection compatibility

Source Catalog는 다음 additive official module interface를 제공한다.

```python
READER_CLIENT_ENCODING: Final = "UTF8"

def require_reader_connection_policy(
    connection: AsyncConnection[Any],
) -> None: ...
```

Metadata와 Guarded Query는 각 source pool checkout 직후, `BEGIN`과 application SQL 전에 이
interface를 호출한다. Verifier는 SQL을 실행하지 않고 libpq/driver connection info에서 다음을
요구한다.

- `180000 <= server_version < 190000`
- `server_encoding == "UTF8"`
- `client_encoding == "UTF8"`
- psycopg codec `encoding == "utf-8"`

Pool connection은 startup parameter로 `client_encoding=UTF8`을 요청한다. Deterministic mismatch는
기존 `ReaderSessionPolicyError`의 고정된 비공개 message로 끝내고 connection을 close해 pool에서
discard한다. 실제 관측값, DSN과 driver/database error는 외부나 structured log에 넣지 않는다.
Connection-info 자체의 transport/driver failure는 기존 transient error 분류를 유지한다.

Deterministic mismatch의 결과는 Metadata `METADATA_UNAVAILABLE`과 no-stale, Query
`QUERY_UNAVAILABLE`, managed staging `SOURCE_VALIDATION_FAILED`, periodic resource observation
`RESOURCE_READ_FAILED`다. 기존 `/ready` wire status는 바꾸지 않지만 Compose healthcheck는 body가
정확히 `{"status":"ready"}`일 때만 healthy다. 한 source라도 deterministic launch policy를
충족하지 못해 `degraded`면 container launch acceptance는 실패한다.

### 4. Result OID policy

User-result named cursor는 `execute` 뒤 RowDescription 전체를 검사하고 첫 `fetchmany` 전에 다음
PostgreSQL 18 `pg_catalog` base OID만 허용한다.

| Type | OID |
|---|---:|
| `int8` | 20 |
| `int2` | 21 |
| `int4` | 23 |
| `text` | 25 |
| `date` | 1082 |
| `timestamptz` | 1184 |
| `numeric` | 1700 |

이는 현재 여섯 curated view의 OID 합집합과 9개 verified result의 합집합이다. Boolean은 predicate,
filter와 intermediate expression에서 계속 허용하지만 final result OID 16은 허용하지 않는다.

검사 순서는 user cursor execute, column capture, 기존 duplicate-column 검사, description/OID 검사,
첫 fetch다. Duplicate는 기존 `400 QUERY_REJECTED` 우선순위를 유지한다. Empty/malformed description과
그 밖의 OID는 details 없는 `503 QUERY_UNAVAILABLE`이며 cursor close와 transaction rollback 뒤
끝난다. Fetch, partial row/hash, commit과 success usage/metric은 없어야 한다.

ADR 0002에서 성공 형식을 설명한 bool, float, bytea, naive temporal, interval, UUID, network와 현재
우연히 성공할 수 있는 JSON/JSONB, array, record, domain 및 그 밖의 type은 launch success domain에서
제외한다. SQL AST의 type/function allowlist를 줄이지 않으므로 predicate나 approved final scalar로
귀결되는 intermediate 사용은 유지한다. 추가 result type은 lossless/deterministic encoding과 새
policy revision을 정확히 승인한 뒤 연다.

PostgreSQL RowDescription은 scalar domain을 base OID로 평탄화하므로 OID 검사만으로 domain을
구분할 수 없다. Static bootstrap Runtime과 Assurance offline composition의 Catalog는 snapshot
publication 전에 eligible column의 `pg_type.typtype`을 확인하고 `d`를 fail-closed한다. 이는 launch
source-admission invariant이며 preserved managed staging/runtime의 기본 Catalog 동작은 바꾸지 않는다.
Transient `type_kind`는 snapshot, metadata revision, persisted codec와 SQL policy material에 들어가지
않으므로 아래 v3 digest와 현재 revision/hash는 그대로다.

### 5. Policy identity and preserved formats

- SQL policy version은 2에서 3으로 올린다.
- SQL policy digest는 reader connection compatibility와 exact seven-OID material을 포함한다.
- 이 구현의 SQL policy v3 digest는
  `sha256:2e94db36095f11f2e9cc4e804666598f79a2ee956002ffa60dbe26bc6ee81388`이다.
- 이전 SQL policy token은 기존 `409 METADATA_REVISION_MISMATCH`로 executor 전에 거부한다.
- `CANONICAL_TIME_POLICY_MATERIAL`, canonical result bytes, metadata revision algorithm과 현재 두
  metadata revision은 바꾸지 않는다.
- `config/verified-queries.yaml`의 schema, metadata revision, column/row count와 expected result hash는
  바꾸지 않는다. 9개 query를 새 SQL policy로 전부 재실행한다.
- Control DB schema, stored metadata codec와 historical row에는 migration, update 또는 delete가 없다.
- SQL policy v2/v3 process를 같은 serving fleet에 섞지 않는다.

Proposed ADR 0020이 선점했던 24-OID result policy v2/SQL policy v3는 이 결정의 v3와 다른 의미이므로
implementation-ready authority가 아니다. Future lossless encoding은 launch v3를 baseline으로 SQL
policy v4 이상에서 다시 제안한다. ADR 0024의 combined-v3 절차도 같은 이유로 재검토 전 실행하지
않는다.

### 6. Artifact and operational freeze

Repository release는 mutable upstream image tag만으로 식별하지 않는다. PostgreSQL, Python과 uv
upstream은 tag와 digest를 함께 고정하고 application image에는 approved Git revision label을 넣는다.
실제 built image digest는 environment change record에 기록한다.

Serving 중 다음은 동결한다.

- source manifest, budget/access policy와 verified dataset
- source schema/view/function/operator/type/collation/extension DDL
- reader role membership, grant와 database/role/server semantic setting
- application/PostgreSQL image와 compose configuration

정상 business DML은 허용한다. Runtime fingerprint로 privileged DBA drift를 모두 탐지하는 기능은
이번 범위가 아니다. Initial inventory 비교와 이후 freeze는 protected operator procedure이며,
설명되지 않은 drift가 발견되면 route하지 않는다.

## Rollout and rollback

Repository acceptance 뒤 실제 환경 실행에는 대상, 접근 권한, route, secret/backup, artifact digest,
stop condition과 change-record owner의 별도 승인이 필요하다.

1. Approved commit과 pinned images에서 application image를 만들고 revision/digest를 기록한다.
2. Source/DDL/role/settings inventory와 RLS 0건, PostgreSQL 18/UTF-8을 traffic 밖에서 확인한다.
3. 단일 replica를 시작하고 exact ready, Metadata, seven-OID corpus와 9/9 verified query를 실행한다.
4. Old/new SQL policy process와 source connection이 동시에 serving하지 않게 old traffic을 drain한다.
5. Route 뒤 health/error/usage를 확인한다.

Stop condition은 RLS source 존재, unsupported advertised result, PG/encoding mismatch, exact ready 실패,
9개 invariant 차이, artifact/inventory 불명확, old/new mixed serving 또는 rollback 미검증이다.

Rollback은 route를 차단하고 new process를 drain한 뒤 직전 image/config/SQL policy와 preserved source
inventory를 복구해 ready와 그 release의 verified baseline을 확인한 다음 route한다. New policy의
Git history와 실행 기록, stored RLS/control data는 삭제하지 않는다.

## Verification

Repository completion에는 다음 증거가 모두 필요하다.

- RLS bootstrap/managed/injected/query quarantine와 기존 external error envelope
- PG18 UTF-8 positive 및 PG17/19, SQL_ASCII, non-UTF8 client/codec negative corpus
- Catalog/resource/query pre-BEGIN order, mismatch connection discard와 no-SQL/no-stale behavior
- Exact seven OID의 nonempty/zero-row positive corpus, bool/JSON/bytea/float/array/record negative와
  scalar-domain OID-erasure/static Catalog rejection corpus
- Duplicate priority, no-fetch/partial result, rollback, pool recovery와 HTTP/MCP 503 parity
- SQL policy v3 digest/stale token, unchanged metadata revision/result hash와 9/9 verified execution
- Exact ready Compose health, pinned image references와 revision label
- `ruff`, `mypy`, full pytest, integration and container acceptance

## Consequences

- 첫 오픈 범위는 작고 fail-closed하며 현재 데이터와 검증 질문을 그대로 제공한다.
- 기존에 우연히 성공하던 결과 타입과 모든 RLS source는 명시적으로 unavailable이 된다.
- Static source 변경은 configuration review와 재배포가 필요하다.
- Managed onboarding, full RLS, broader lossless types, multi-replica/HA, cost attribution와 workflow trace는
  구현 삭제가 아니라 first-launch 이후의 별도 결정이다.
- 실제 protected deployment 증거가 없으면 repository가 launch profile을 구현했다는 사실만 말할 수
  있고 특정 production 환경이 전환됐다고 주장할 수 없다.
