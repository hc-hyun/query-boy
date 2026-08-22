# ADR 0002: Guarded Query Contract

Status: Accepted

Date: 2026-08-22

## Context

Metadata API는 질문에 맞는 relation과 현재 `metadata_revision`을 제공하지만 SQL을
실행하지 않는다. Query 실행은 모델이 생성한 SQL, 오래된 metadata, 비싼 plan과 큰
결과를 동시에 다루므로 하나의 gateway 경계에서 검증과 제한을 강제해야 한다.

## Decision

HTTP에 다음 application contract를 추가한다.

```text
POST /query
{
  "source_id": "development-issues",
  "sql": "SELECT ... FROM ai.issue_overview",
  "metadata_revision": "sha256:..."
}
```

Operator caller는 실행 시작 audit log의 `query_id`로 활성 query를 취소할 수 있다.

```text
DELETE /queries/{query_id}
  -> { "status": "cancel_requested", "query_id": "..." }
```

성공 응답은 원본 SQL을 반복하지 않고 다음 정보를 반환한다.

```text
status, query_id, metadata_revision, fingerprint
columns, rows, row_count, result_bytes, truncated
queue_ms, elapsed_ms, plan_summary
```

실행 순서는 고정한다.

1. Source와 현재 published metadata를 확인한다.
2. 요청 revision이 현재 revision과 다르면 `409 METADATA_REVISION_MISMATCH`로 거부한다.
3. ADR 0001의 AST validation으로 현재 snapshot relation allowlist를 검사한다.
4. Source별 concurrency slot을 제한 시간 안에 획득한다.
5. Reader connection에서 `BEGIN READ ONLY`와 transaction-local timeout을 적용한다.
6. Database, session user와 read-only 상태를 재검증한다.
7. `EXPLAIN (FORMAT JSON)`의 total cost, 최대 plan rows와 node 수를 profile 상한과 비교한다.
8. 결과 column 이름이 중복되면 `QUERY_DUPLICATE_RESULT_COLUMN`으로 거부한다.
9. Named cursor로 bounded fetch하고 row/UTF-8 byte 상한에서 결과를 truncate한다.
10. Timeout, cancel과 오류는 rollback하고 connection을 pool에 안전하게 반환한다.

`result_bytes`는 `rows` 배열을 compact JSON으로 UTF-8 직렬화한 크기이며 배열 괄호와
행 사이 쉼표를 포함한다. 상한을 넘길 행은 응답에 넣지 않고 `truncated`를 설정한다.

Planner cost는 PostgreSQL의 상대적인 추정치이지 시간이나 금액이 아니다. 따라서 plan
admission은 명백히 비싼 query를 일찍 거부하는 보조 장치이며 statement/transaction
timeout, concurrency, result size와 cancel을 대체하지 않는다.

응답의 `rows`는 column 이름을 key로 쓰므로 같은 이름을 두 번 반환할 수 없다. 중복
alias를 허용하면 `columns`에는 두 이름이 남고 dictionary row에서는 앞선 값이 사라져
HTTP와 MCP 계약이 모순되므로 fetch 전에 fail-closed한다.

초기 reason code는 다음과 같다.

| HTTP | Code | Meaning |
|---:|---|---|
| 400 | `QUERY_REJECTED` | AST 또는 plan policy가 query를 허용하지 않음 |
| 403 | `OPERATOR_REQUIRED` | Query cancel에 operator 권한이 없음 |
| 404 | `SOURCE_NOT_FOUND` | Source를 공개하지 않는 기존 오류 |
| 404 | `QUERY_NOT_FOUND` | Operator의 허용 source 안에 활성 query가 없음 |
| 408 | `QUERY_TIMEOUT` | 실행 deadline 초과 또는 취소 |
| 409 | `METADATA_REVISION_MISMATCH` | SQL 생성에 사용한 revision이 현재 revision과 다름 |
| 429 | `QUERY_OVERLOADED` | Source concurrency/connection queue 상한 초과 |
| 503 | `QUERY_UNAVAILABLE` | 비공개 database 또는 infrastructure 오류 |

## Consequences

- Client는 revision mismatch 시 `/meta`를 다시 호출해 SQL을 재생성해야 한다.
- 응답은 bounded list로 반환한다. Database fetch는 streaming하지만 HTTP chunk streaming은
  현재 범위에 포함하지 않는다.
- 초기 plan threshold는 보수적인 운영 기본값이며 부하 테스트 전에는 최종 비용 정책으로
  간주하지 않는다.
- Process-local semaphore는 단일 replica 안에서만 concurrency를 제한한다.
  `ponytail:` replica가 source quota를 공유해야 할 때 distributed limiter로 교체한다.
- Function/operator의 resolved candidate와 volatility 검증은 ADR 0003을 따른다.
- Query ID는 실행 전에 생성되어 audit log와 PostgreSQL `application_name`에 동일하게
  기록된다. Cancel lookup과 connection의 pool 반환은 lock으로 직렬화한다.
