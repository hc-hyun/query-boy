# Column Disclosure Verification — 2026-08-23

## Scope

73-column synthetic wide relation에서 initial 20-column test budget으로 question-scoped
disclosure와 semantic 필수 column 보존을 검증했다. Production interactive target은 40이다.

## Evidence

| Boundary | Result |
|---|---|
| Total/returned column count | 73 / 20 |
| Grain key | `issue_id` 유지 |
| Default time | `discovered_at` 유지 |
| Matched business predicate | `cause` 유지 |
| Measure source | `comment_count` 유지 |
| Unrelated tail column | `extra_attribute_60` 제외 |
| Hidden-column index | 응답에서 제외, `indexes_truncated=true` |
| Truncation contract | Relation과 top-level 모두 true |
| Existing MVP | Unit 94+, integration 9, golden query 9/9 회귀 통과 |

이 검증은 context 비용 감소 동작을 다룬다. SQL 실행 비용과 권한은 기존 gateway hard
limit과 curated view 경계가 담당한다.
