# Physical Catalog Verification — 2026-08-23

## Scope

Reader 권한 범위 안에서 PostgreSQL primary key, foreign key와 단순 index key를 수집하고
revision, immutable snapshot과 question context에 제한된 형태로 제공하는지 검증했다.

## Evidence

| Boundary | Executed evidence | Result |
|---|---|---|
| Primary key | `development.issues(id)` | 수집 및 `primary_key` semantic role 확인 |
| Foreign key | assignee/reporter/test-unit references | Local/referenced column 순서와 relation 일치 |
| Simple indexes | PK, discovered date, status/date keys | 수집 성공 |
| Partial index | `(assignee_id, status) WHERE status <> 'RESOLVED'` | 공개 대상에서 제외 |
| Reader scope | MVP reader가 접근하는 curated views | Base-table key/index 미노출 |
| API scope | 이름/definition/predicate 없이 columns, uniqueness, primary flag만 반환 | PASS |
| Join boundary | Physical FK가 있는 synthetic context | `joins`는 빈 상태 유지 |
| Revision/store | Non-empty structure hash와 PostgreSQL JSONB round trip | PASS |

Indexes는 비용 보장이나 allowlist가 아니다. Query의 비용 통제는 plan admission,
statement/transaction timeout, source concurrency와 result hard limit이 계속 담당한다.
