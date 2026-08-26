# ADR 0002: Guarded Query External Behavior And Safety

Status: Accepted

Date: 2026-08-22

## Context

Metadata API는 질문에 맞는 relation과 현재 `metadata_revision`을 제공하지만 SQL을
실행하지 않는다. Query 실행은 모델이 생성한 SQL, 오래된 metadata, 비싼 plan과 큰
결과를 동시에 다루므로 하나의 gateway 경계에서 검증과 제한을 강제해야 한다.

## Decision

HTTP에 다음 application endpoint를 추가한다.

```text
POST /query
{
  "source_id": "development-issues",
  "sql": "SELECT ... FROM ai.issue_overview",
  "metadata_revision": "sha256:...",
  "sql_policy_revision": "sha256:..."
}
```

Operator caller는 실행 시작 audit log의 `query_id`로 활성 query를 취소할 수 있다.

```text
DELETE /queries/{query_id}
  -> { "status": "cancel_requested", "query_id": "..." }
```

성공 응답은 원본 SQL을 반복하지 않고 다음 정보를 반환한다.

```text
status, query_id, metadata_revision, sql_policy_revision, fingerprint
columns, rows, row_count, result_bytes, truncated
queue_ms, elapsed_ms, plan_summary
```

실행 순서는 고정한다.

1. Source와 현재 published metadata를 확인한다.
2. 요청 metadata 또는 SQL policy revision이 현재 revision과 다르면
   `409 METADATA_REVISION_MISMATCH`로 거부한다.
3. ADR 0001의 AST validation으로 현재 snapshot relation allowlist를 검사한다.
4. Source별 concurrency slot을 제한 시간 안에 획득한다.
5. Reader connection에서 명시적인 `REPEATABLE READ READ ONLY` transaction을 시작하고 첫
   settings statement로 transaction-local `TimeZone=UTC`를 적용한 뒤 timeout/resource setting을
   적용한다.
6. Database, session user, isolation, read-only와 `TimeZone=UTC` 상태를 재검증한다.
7. `EXPLAIN (FORMAT JSON)`의 total cost, 최대 plan rows와 node 수를 profile 상한과 비교한다.
8. 결과 column 이름이 중복되면 `QUERY_DUPLICATE_RESULT_COLUMN`으로 거부한다.
9. Named cursor로 작은 bounded batch를 fetch하고 row/UTF-8 byte 상한에서 결과를 truncate한다.
10. Timeout, cancel과 오류는 rollback하고 connection을 pool에 안전하게 반환한다.

`result_bytes`는 `rows` 배열을 compact JSON으로 UTF-8 직렬화한 크기이며 배열 괄호와
행 사이 쉼표를 포함한다. 상한을 넘길 행은 응답에 넣지 않고 `truncated`를 설정한다.

JSON scalar external format은 HTTP와 MCP에서 동일하다.

- PostgreSQL integer, boolean, text와 finite floating point는 JSON의 대응 scalar를 사용한다.
- `numeric`은 precision과 scale을 잃지 않도록 decimal 문자열로 반환한다.
- `bytea`는 `base64:<standard-base64>` 문자열로 반환한다.
- Aware datetime은 UTC로 정규화한 ISO 문자열(`+00:00`, `Z` 아님)로 반환한다. Naive datetime,
  date, time과 timetz는 기존 ISO 표현을 보존하고 interval, UUID와 network type은 안정적인
  text 문자열로 반환한다.
- 비유한 float는 JSON number로 내보내지 않고 `NaN`, `Infinity`, `-Infinity` 문자열로
  반환한다.
- 지원하지 않는 driver value와 string이 아닌 object key는 비공개 `QUERY_UNAVAILABLE`로
  fail-closed한다.

Byte accounting, verified result hash와 protocol serialization은 이 canonical value를 함께
사용한다. 따라서 정확한 numeric 표현을 바꾸는 것은 external result format 변경이다.

Planner cost는 PostgreSQL의 상대적인 추정치이지 시간이나 금액이 아니다. 따라서 plan
admission은 명백히 비싼 query를 일찍 거부하는 보조 장치이며 statement/transaction
timeout, concurrency, result size와 cancel을 대체하지 않는다.

응답의 `rows`는 column 이름을 key로 쓰므로 같은 이름을 두 번 반환할 수 없다. 중복
alias를 허용하면 `columns`에는 두 이름이 남고 dictionary row에서는 앞선 값이 사라져
HTTP와 MCP response shape가 모순되므로 fetch 전에 fail-closed한다.

초기 reason code는 다음과 같다.

| HTTP | Code | Meaning |
|---:|---|---|
| 400 | `QUERY_REJECTED` | AST 또는 plan policy가 query를 허용하지 않음 |
| 400 | `QUERY_INVALID` | PostgreSQL이 식별한 수정 가능한 query 의미 오류 |
| 403 | `OPERATOR_REQUIRED` | Query cancel에 operator 권한이 없음 |
| 404 | `SOURCE_NOT_FOUND` | Source를 공개하지 않는 기존 오류 |
| 404 | `QUERY_NOT_FOUND` | Operator의 허용 source 안에 활성 query가 없음 |
| 408 | `QUERY_TIMEOUT` | 실행 deadline 초과 또는 취소 |
| 409 | `METADATA_REVISION_MISMATCH` | SQL 생성에 사용한 metadata 또는 SQL policy revision이 현재 값과 다름 |
| 429 | `QUERY_OVERLOADED` | Source concurrency/connection queue 상한 초과 |
| 503 | `QUERY_UNAVAILABLE` | 비공개 database 또는 infrastructure 오류 |

AST가 승인하지 않은 operator construct를 식별할 수 있을 때 `QUERY_REJECTED`의 details에는
기존 `reason_code`와 함께 bounded `rejected_construct`를 선택적으로 반환한다. 값은
`BETWEEN`, `NOT BETWEEN`, `BETWEEN SYMMETRIC`, `NOT BETWEEN SYMMETRIC`, `OPERATOR`의 고정
집합이며 raw SQL token, snippet, literal과 parser location은 반환하지 않는다. Reason code와
로그 label은 construct별로 늘리지 않는다.

PostgreSQL이 반환한 오류 중 사용자가 SQL만 수정해 해결할 수 있고 안전하게 분류 가능한
SQLSTATE만 `QUERY_INVALID`로 반환한다. `details.reason_code`는 다음 고정 집합이며 database
message, identifier, SQL snippet, literal과 위치는 반환하지 않는다.
이 분류는 사용자 SQL을 parse analysis하는 `EXPLAIN`과 실제 cursor execute/fetch
단계에서만 적용한다. Session setting, reader policy, resolved-object 검증, transaction
commit과 같은 내부 단계의 같은 SQLSTATE는 수정 가능한 사용자 오류로 공개하지 않는다.

| Reason | Internal SQLSTATE category |
|---|---|
| `QUERY_UNDEFINED_COLUMN` | `42703` |
| `QUERY_INVALID_CAST` | `22P02`, `22007`, `22008`, `42846` |
| `QUERY_DIVISION_BY_ZERO` | `22012` |
| `QUERY_INVALID_LIMIT` | `2201W`, `2201X` |
| `QUERY_INVALID_REGULAR_EXPRESSION` | `2201B` |
| `QUERY_NUMERIC_VALUE_OUT_OF_RANGE` | `22003` |
| `QUERY_INVALID_FUNCTION_ARGUMENT` | `22023` |
| `QUERY_INVALID_FUNCTION_USAGE` | `42809` |
| `QUERY_FUNCTION_SIGNATURE_MISMATCH` | `42883` |

`QUERY_INVALID` detail은 `{reason_code, action: "CORRECT_SQL", retryable: true}`로 고정한다.
Public message는 reason별로 SQL을 어떻게 교정할지 짧게 안내하는 server-authored
message이며 PostgreSQL message를 재사용하지 않는다. `retryable=true`는 같은 SQL을
반복하라는 뜻이 아니라 `action`을 수행하고 사용자 의미를 보존하는 교정이 명확할
때만 한 번 재시도해도 된다는 뜻이다.

Privilege, connection, server shutdown, 알 수 없는 SQLSTATE와 driver/serialization 오류는
계속 details 없는 `QUERY_UNAVAILABLE`로 숨긴다. Timeout과 cancel은 기존 전용 분기를 먼저
적용한다.

## Consequences

- Client는 revision mismatch 시 `/meta`를 다시 호출해 SQL을 재생성하고 두 revision을 함께
  갱신해야 한다.
- 응답은 bounded list로 반환한다. Database fetch는 streaming하지만 HTTP chunk streaming은
  현재 범위에 포함하지 않는다.
- 초기 plan threshold는 보수적인 운영 기본값이며 부하 테스트 전에는 최종 비용 정책으로
  간주하지 않는다.
- Process-local semaphore는 단일 replica 안에서만 concurrency를 제한한다.
  `ponytail:` replica가 source quota를 공유해야 할 때 distributed limiter로 교체한다.
- Function/operator의 resolved candidate와 volatility 검증은 ADR 0003을 따른다.
- Reader UTC, canonical-time policy material, revision 전환과 business calendar 분리는
  [ADR 0019](0019-canonical-time-stability.md)를 따른다.
- Default psycopg loader가 interval/time/duplicate·fractional JSON을 무손실로 전달하지 못하고
  SQL_ASCII와 result OID identity를 혼동하며 empty multirange를 array로 오인한다. Reader semantic
  setting과 unversioned collation이 SQL 의미·hash를 흔드는 현재 gap과 승인 전 선택지는
  [proposed ADR 0020](0020-lossless-interval-and-json-numeric-encoding.md)에
  기록한다. 승인 전에는 현재 encoding을 lossless/deterministic 범위로 확대해 해석하지 않는다.
- Query ID는 실행 전에 생성되어 audit log와 PostgreSQL `application_name`에 동일하게
  기록된다. 완료 audit는 query ID, caller/tenant/source, fingerprint, queue/elapsed,
  row/byte/truncation과 plan cost를 남기고 실패 audit는 공개 가능한 application error code만
  남긴다. SQL text, literal과 database error detail은 기록하지 않는다. Cancel lookup과
  connection의 pool 반환은 lock으로 직렬화한다.
- 비용과 resource 운영 절차는
  [query cost runbook](../query-cost-control.md)을 따른다.
