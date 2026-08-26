# Guarded Query Module

Status: Physical package boundary active

> **현재 launch 기준은 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A`다.** SQL policy v3와 PostgreSQL 18의 일곱 result OID만 지원하고 모든 RLS source는 DB 전에 격리한다.

## 목적

### 30초 요약

Guarded Query는 **SQL 안전문**이다. 이미 인가된 source와 발행된 metadata를 받아 SQL이 허용 범위
안인지 검사하고, PostgreSQL 자원 상한 안에서 실행·취소·rollback한다.

| 질문 | 답 |
|---|---|
| 언제 이 module을 고르는가? | SQL 허용 범위, 실행 제한, 결과 encoding, cancel/rollback을 바꿀 때 |
| 입력은 어디서 오는가? | Delivery가 인가한 source, 외부 입력인 SQL, published metadata/policy revision |
| 성공하려면? | AST·revision·resolved object·plan·session·result OID 검사를 모두 통과해야 함 |
| 하지 않는 일은? | SQL 생성, caller 인증·인가, source/metadata 관리, HTTP/MCP rendering |

핵심 구분은 다음과 같다.

- **공식 Python interface**: 다른 module이 호출하는 type, Protocol, function, use case와 domain error
- **Application result/error**: `QueryService`가 caller module에 돌려주는 dictionary와 오류 의미
- **Policy identity**: 어떤 SQL·reader·result type이 허용되는지를 식별하는 revision/material
- **Safety/lifecycle invariant**: 검사, transaction, cancel, rollback과 cleanup의 필수 순서·결과

## 소유 책임

- PostgreSQL AST 기반 단일 read-only statement와 relation/function/operator/type 검증
- Published metadata revision과 SQL policy revision 일치 확인
- Source별 queue/concurrency, active query ID와 cancel lifecycle
- Read-only repeatable-read transaction, timeout/resource setting과 reader-session 검증
- Resolved function/operator와 `EXPLAIN` plan admission
- Result column/OID 검사, bounded fetch, row/byte accounting과 canonical encoding
- Client/operator/shutdown cancellation, rollback, pool invalidation과 drain
- SQL literal, credential과 내부 DB 오류를 숨기는 query-domain 오류 의미

## 소유하지 않는 책임

- Caller 인증, active-source visibility와 operator 권한
- Source manifest, budget/access policy와 registry mutation
- Physical catalog 수집, metadata context/revision publish
- HTTP status, MCP `isError`와 transport serialization
- Verified-query/quality case 작성과 승인
- 자연어 질문에서 SQL을 생성하는 기능

## 현재 코드 위치

| 위치 | 역할 | 함께 볼 test |
|---|---|---|
| [`guarded_query/sql_validation.py`](../../../src/query_man/guarded_query/sql_validation.py) | AST policy, `ValidatedSql`, `SQL_POLICY_REVISION` | [`test_sql_validation.py`](../../../tests/test_sql_validation.py), [`test_security_evaluation.py`](../../../tests/test_security_evaluation.py) |
| [`guarded_query/query.py`](../../../src/query_man/guarded_query/query.py) | `QueryService`, executor Protocol과 PostgreSQL 실행/lifecycle | [`test_query.py`](../../../tests/test_query.py) |
| [`guarded_query/result_encoding.py`](../../../src/query_man/guarded_query/result_encoding.py) | Canonical scalar encoding과 launch OID policy material | [`test_result_encoding.py`](../../../tests/test_result_encoding.py) |
| [`errors.py`](../../../src/query_man/errors.py) | Query-domain error 발생 의미 | Delivery가 external envelope를 소유 |
| [`source_catalog/reader_policy.py`](../../../src/query_man/source_catalog/reader_policy.py) | Source Catalog가 제공하는 connection/session 검사 | [`test_reader_policy.py`](../../../tests/test_reader_policy.py) |
| [`security-evaluation.yaml`](../../../config/security-evaluation.yaml) | Assurance 소유 parser/execution corpus | [`test_security_evaluation.py`](../../../tests/test_security_evaluation.py) |
| [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py) | 실제 PostgreSQL type/OID/rollback corner | Integration 전용 |

Guarded Query 구현은 `src/query_man/guarded_query` physical package에 있다. Package marker는 re-export하지
않고 consumer는 위 leaf path를 직접 import한다. Root `errors.py`와 cross-module test는 shared
transition artifact다.

## 제공 인터페이스와 소유 경계

### 공식 Python interface

아래 code block은 body만 `...`로 생략한 **현재 exact signature/shape**이며 미래 예시가 아니다.

```python
@dataclass(frozen=True)
class ValidatedSql:
    fingerprint: str
    relations: tuple[str, ...]
    functions: tuple[str, ...]
    operators: tuple[str, ...]

def validate_sql(
    sql: str,
    *,
    allowed_relations: Iterable[str],
    allowed_functions: Iterable[str] = DEFAULT_ALLOWED_FUNCTIONS,
    allowed_operators: Iterable[str] = DEFAULT_ALLOWED_OPERATORS,
    allowed_types: Iterable[str] = DEFAULT_ALLOWED_TYPES,
    max_sql_bytes: int = 100_000,
) -> ValidatedSql: ...
```

`SQL_POLICY_REVISION`은 현재 policy token이다. Immutable `CANONICAL_TIME_POLICY_MATERIAL`과
`RESULT_OID_POLICY_MATERIAL`은 revision/hash 공유 입력이며 그 내용·digest는 별도 policy identity다.

`QueryService`의 공개 호출 signature는 다음과 같다.

```python
async def query(
    self,
    source_id: str,
    sql: str,
    metadata_revision: str,
    sql_policy_revision: str,
    *,
    query_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, object]: ...

async def cancel(self, query_id: str) -> bool: ...
```

Service는 다음 exact execution port를 소비한다.

```python
class QueryExecutor(Protocol):
    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]: ...
    async def cancel(self, query_id: str) -> bool: ...
    async def close(self) -> None: ...
```

### RuntimeQueryExecutor lifecycle interface

Runtime만 운영 lifecycle이 추가된 Protocol을 소비한다.

```python
class RuntimeQueryExecutor(QueryExecutor, Protocol):
    def stop_accepting(self) -> None: ...
    async def drain(self, grace_ms: int) -> None: ...
    async def invalidate(self, source_id: str) -> None: ...
```

`stop_accepting`은 새 query를 막고, `drain`은 grace 뒤 남은 query를 취소한다. `invalidate`는 해당
source의 pool/admission state를 버리고, inherited `close`는 active task와 pool을 정리한다.
Application-only fake는 Runtime lifecycle method를 구현할 필요가 없다.

공식 domain error는 `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`,
`QueryTimeoutError`, `QueryUnavailableError`다. Delivery가 이를 HTTP/MCP envelope로 표현하는 방식은
별도 external wire format이다.

### Application result와 error 의미

성공 dictionary는 다음 field를 반환한다.

| 묶음 | Field |
|---|---|
| Identity | `status`, `query_id`, `metadata_revision`, `sql_policy_revision`, `fingerprint` |
| Result | `columns`, `rows`, `row_count`, `result_bytes`, `truncated` |
| Usage | `queue_ms`, `elapsed_ms` |
| Plan | `plan_summary {total_cost, max_rows, node_count}` |

Delivery는 UUID query ID와 trusted tenant를 생성한다. Public caller는 query ID를 고르지 않으며,
Delivery가 operator authorization을 끝낸 뒤 `cancel`을 호출한다. Guarded Query는 active query ID가
있었는지만 `bool`로 반환한다.

Application error category는 `SOURCE_NOT_FOUND`, `METADATA_REVISION_MISMATCH`, `QUERY_REJECTED`,
`QUERY_INVALID`, `QUERY_OVERLOADED`, `QUERY_TIMEOUT`, `QUERY_UNAVAILABLE`다. Allowlisted 사용자 SQL
오류만 bounded server-authored reason/action을 공개한다. Session, connection, policy, result OID와
내부 DB/driver 실패는 details 없는 `QUERY_UNAVAILABLE`로 fail-closed한다.

### 현재 launch policy identity

SQL policy는 **v3**다. Digest는 기존 SQL/시간 policy에 PostgreSQL 18 server/client UTF-8·driver
`utf-8` compatibility와 다음 exact result OID material을 포함한다.

| Type | OID | Type | OID |
|---|---:|---|---:|
| `int8` | 20 | `int2` | 21 |
| `int4` | 23 | `text` | 25 |
| `date` | 1082 | `timestamptz` | 1184 |
| `numeric` | 1700 |  |  |

Boolean은 predicate/intermediate expression에는 쓸 수 있지만 final OID 16은 허용하지 않는다.
Float, bytea, JSON/JSONB, UUID, interval/time, array, network, record/domain 등도 final result가 될 수
없다. Encoder가 그 Python value를 처리할 수 있다는 사실은 launch 지원 범위를 넓히지 않는다.

검사 순서는 user cursor execute → column capture → duplicate-column 검사 → description/OID 검사 →
첫 fetch다. Duplicate는 기존 `400 QUERY_REJECTED`가 우선한다. Empty/malformed description이나
unsupported OID는 fetch·partial row·commit·success usage 없이 close/rollback 뒤 details 없는
`503 QUERY_UNAVAILABLE`로 끝난다.
Scalar domain은 RowDescription에서 base OID로 평탄화되므로 static Runtime/Assurance Catalog가 exposed
domain column을 snapshot 전에 거부하고 custom domain cast는 기존 SQL type allowlist가 거부한다.
보존된 managed Catalog 동작과 SQL policy material은 바꾸지 않는다.

`result_bytes`는 compact UTF-8 JSON rows array의 괄호와 comma까지 센다. 다음 행이 row/byte 상한을
넘으면 그 행은 넣지 않고 `truncated=true`로 반환한다. 상세 근거와 변경되지 않은 canonical
time/revision/hash는 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md), 실제 제한은
[Query 제한과 자원](../../query-cost-control.md), result 회귀는
[Verified Query](../../verified-queries.md)를 따른다.

이전 token은 executor 전에 기존 `409 METADATA_REVISION_MISMATCH`로 거부하며 v2/v3를 함께 serving하지 않는다.

### 실행 순서와 safety invariant

1. `SourceReader`에서 source 존재를 확인한다.
2. RLS source를 revision, validation, queue와 DB보다 먼저 quarantine한다.
3. Published metadata revision과 SQL policy revision을 확인한다.
4. SQL AST와 relation/function/operator/type을 검증한다.
5. Source별 concurrency slot을 획득한다.
6. Pool checkout 직후 no-SQL PostgreSQL 18/UTF-8 preflight를 수행한다.
7. Active query를 등록하고 read-only transaction, UTC/resource setting과 session을 검증한다.
8. Resolved function/operator와 `EXPLAIN` plan budget을 검증한다.
9. User cursor, duplicate column, exact result OID와 bounded fetch를 순서대로 수행한다.
10. 성공만 commit하고 실패·취소·disconnect·shutdown은 cancel/rollback과 slot 반환으로 끝낸다.

RLS quarantine는 direct executor에서도 pool/DB 전에 적용하며 tenant 유무와 관계없이 details 없는
`503 QUERY_UNAVAILABLE`다. 기존 RLS type/code/history는 보존하되 current serving에는 참여시키지 않는다.
Connection preflight mismatch는 connection을 close/discard하고 role/database default는 바꾸지 않는다.
Connection-info transport/driver failure의 기존 transient 분류는 유지한다.

`QueryService`는 optional `GatewayUsageRecorder`를 받는다. Static composition은 recorder를 주입하지
않아 query terminal event를 누적하지 않는다. Managed composition만
`ManagedGatewayUsageRecorder`를 주입해 server-resolved source ID, budget profile, active metadata
revision과 terminal outcome을 기록한다. 성공만 queue/elapsed/rows/bytes/truncation 합계에 기여한다.
Recorder 실패는 query 결과를 바꾸지 않고 SQL, literal, tenant, credential, query ID와 raw DB error를
payload에 넣지 않는다.

## 소비 인터페이스와 전제

| Provider/consumer | 사용하는 경계 | Guarded Query 또는 상대 module의 의무 |
|---|---|---|
| [Source Catalog](../source-catalog/README.md) | `SourceReader`, immutable profile/budget, connection/session verifier | Profile을 in-place 변경하지 않고 verifier 순서를 지킴 |
| [Metadata](../metadata/README.md) | Immutable published snapshot/revision, relation ceiling | Stale revision은 executor 전에 거부 |
| [Runtime](../runtime/README.md) | Query lifecycle caller와 managed-only usage recorder 조립 | Static에는 recorder를 주입하지 않고 shutdown/reload별 정해진 drain·invalidate·close 순서를 지킴 |
| Delivery | `QueryService`, result/domain error | Source 인가 뒤 generated ID/trusted tenant를 주고 disconnect를 cancellation으로 전파 |
| Assurance | Public service/executor | Offline CLI에서만 concrete adapter를 조립하고 verified SQL도 같은 query 경로로 실행 |

Guarded Query는 Control DB table, HTTP/MCP model이나 다른 module의 private implementation을 직접 알지
않는다.

## 불변조건

- SQL AST, relation/function/operator/type, revision과 resolved object를 fail-closed로 검증한다.
- Queue/concurrency/row/byte는 executor가, read-only/timeout/work/temp/session은 PostgreSQL이 강제한다.
- Reader는 최소 권한이며 PostgreSQL 18 server/client UTF-8이어야 한다.
- RLS source는 metadata, queue와 DB 전에 격리한다.
- Final result는 exact seven OID이며 OID 검사는 첫 fetch 전에 끝난다.
- Cancel, timeout, encoding 실패와 disconnect는 rollback과 slot 반환으로 끝난다.
- SQL, bind literal, credential과 원본 DB 오류를 response나 일반 log에 노출하지 않는다.
- Source 차이는 `source_id` Python 분기가 아니라 profile, budget과 curated view로 표현한다.

## 모듈 내부 변경

공식 interface, result/error, policy와 safety/lifecycle 결과가 같다면 다음은 독립적으로 바꿀 수 있다.

- 같은 AST acceptance를 만드는 validator 내부 정리
- 같은 admission 결과를 만드는 plan helper 개선
- Row/byte/OID 결과가 같은 cursor/encoding 성능 개선
- Public reason code가 같은 private exception mapping 정리
- Cancel/rollback/drain 결과가 같은 lock·task bookkeeping 개선

Shared transition file/test는 coordinating agent가 single-writer와 consumer 검토 순서를 정한다.

## 사용자 승인이 필요한 경계 변경

| 변경 범주 | 멈추고 승인받을 예 |
|---|---|
| Module interface | `validate_sql`, `ValidatedSql`, `QueryService`, executor Protocol, domain error의 shape/signature/호출 의미 |
| Policy/compatibility identity | SQL construct/allowlist, policy revision/material, reader compatibility, result OID·canonical encoding/hash |
| Application/external format | Query input/result field, bytes/truncation, error/reason, cancel-not-found 의미와 Delivery projection |
| Safety/lifecycle invariant | Authorize→validate→admit→preflight→transaction→OID→fetch→cancel/rollback 순서와 모든 limit/fail-closed 결과 |
| Ownership/composition | Concrete executor 조립 위치, provider private 구현이나 Control DB dependency |

Result type 확대나 RLS serving 재개는 parked research의 단순 실행이 아니다. Current v3/quarantine를
기준으로 compatibility, migration, rollback과 검증을 새로 제안해 정확히 승인받는다. Protected 실행은
repository 변경과 별도로 target, access, stop condition과 change-record 책임 승인이 필요하다.

## 검증

기본 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_reader_policy.py \
  tests/test_sql_validation.py tests/test_security_evaluation.py \
  tests/test_query.py tests/test_result_encoding.py
```

- Result/error projection 변경: HTTP/MCP test 추가
- Concurrency/cancel 변경: load/server test 추가
- PostgreSQL transaction/reader/OID/pool 변경: `uv run pytest -m integration tests/test_source_database_corners.py`
- DB boundary 변경: 전체 `uv run pytest -m integration` 추가

Focused gate는 root `ruff`, `mypy`, full pytest를 대신하지 않는다.

## 집중해서 읽을 범위

| 작업 | 먼저 읽을 것 | 직접 경계가 바뀔 때만 추가 |
|---|---|---|
| SQL validation | 이 문서, `guarded_query/sql_validation.py`, parser/security tests, [ADR 0001](../../decisions/0001-postgresql-ast-validation.md) | Metadata relation ceiling과 Delivery error projection |
| Query 실행·limit | `guarded_query/query.py`, `test_query.py`, [query-limit 문서](../../query-cost-control.md), [ADR 0005](../../decisions/0005-initial-query-budgets.md) | Source reader, Runtime usage/lifecycle |
| Result/OID/encoding | `guarded_query/result_encoding.py`, result/query corner tests, [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md) | Metadata revision과 [verified result](../../verified-queries.md) hash consumer |
| Cancel/drain/invalidate | Executor Protocol, query/load/server tests | Runtime composition·shutdown consumer |
| Application result/error | `QueryService`, errors와 direct consumer tests, [ADR 0002](../../decisions/0002-guarded-query-contract.md) | HTTP/MCP rendering은 Delivery 문서/test |
| Reader/resolved object | Source Catalog interface, `source_catalog/reader_policy.py`, [ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md) | Actual transaction/pool integration path |
| RLS 또는 result 확대 | Current ADR과 future-work research | 정확한 요구·승인 전에는 구현 범위로 읽지 않음 |

Control DB persistence, metadata relevance algorithm과 parked proposal body는 현재 interface나 승인된 launch
경계를 바꾸지 않는 한 읽을 필요가 없다.
