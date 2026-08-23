# Source Extension Assurance — 2026-08-23

> 이 문서의 caller별 source-scope 결과는 당시 구현의 역사적 증거다. 현재 access-policy
> version 2와 shared visibility 계약은
> [shared access audit](2026-08-23-shared-access.md)이 우선한다.

## 결론

네 번째 독립 database `commerce_edges`를 추가해도 source별 runtime Python 분기, 새 endpoint,
dependency 또는 budget profile 없이 기존 목표를 유지했다. Production-style bearer policy와
두 runtime replica에서 control-plane L0→L2 publish, MCP exact query와 deactivate가 service
restart 없이 반영됐다.

감사 과정에서 기존 단일-process/local acceptance가 가리던 정합성 문제를 발견해 함께
수정했다.

| 발견한 문제 | 조치 | 결과 |
|---|---|---|
| 중복 result alias가 `columns`와 dictionary `rows`를 불일치시킴 | Fetch 전에 `QUERY_DUPLICATE_RESULT_COLUMN`으로 거부 | PASS |
| Production allowlist caller가 미래 source를 사전 승인할 수 없음 | 명시적 `all_sources: true` opt-in 추가 | PASS |
| 다른 replica가 startup 뒤 publish된 verified revision을 모름 | Source reload poll마다 immutable revision map을 병합 | PASS |
| MCP 예상 밖 예외와 추가 입력 처리에서 HTTP와 차이 | `INTERNAL_ERROR` 비공개 처리, strict argument schema | PASS |
| Bootstrap 또는 control source ID를 동일 schema의 다른 endpoint에 재사용 가능 | Host/port/database/user/TLS mode 고정 | PASS |
| Publish staging의 reader role 검사가 좁음 | Role flags, default read-only, 유한한 양수 connection limit, TEMP/CREATE 금지 검사 | PASS |
| Control-plane `port_env`가 replica마다 다르게 해석될 수 있음 | Publisher에서 실제 port로 resolve한 document 저장 | PASS |

## Fixture Corner Cases

`commerce_edges`는 `ai."Order"`, `ai."OrderLine"` 두 quoted view를 공개한다.

- UUID, `numeric(12,2)`, `timestamptz`, date, JSONB와 SQL/JSON null
- Unicode JSON value와 ordered typed JSON serialization
- `[OrderID, LineNo]` composite grain
- One-to-many approved join, fanout guidance와 line이 0개인 order
- Relation/column canonical name과 quoted `sql_name` 분리
- Dedicated reader/view owner, base relation·TEMP·CREATE·cross-database 권한 차단
- UTC reader timezone으로 deterministic timestamp hash 고정

대표 verified query 결과는 4 rows, 973 bytes,
`sha256:b935f61fdd91ca7c1249b23c420bd599dac7d5729f143ba8b842cd0c9798ee27`다.
`EXPLAIN` summary는 total cost 3.66, 최대 추정 6 rows, 10 nodes로 `interactive` profile의
100,000 cost, 1,000,000 rows, 100 nodes 상한 안에 있다.

## Authenticated Two-Replica MCP Acceptance

| Scenario | Evidence |
|---|---|
| 미래 source 권한 | `all_sources` operator는 hot-added source를 보고 development-only caller는 목록/조회 모두 `SOURCE_NOT_FOUND` |
| Overprivileged reader | 일시적 `CREATEDB` reader publish가 `SOURCE_VALIDATION_FAILED`; role 복구 확인 |
| Revision consistency | L0 revision으로 생성한 SQL은 semantic generation 적용 뒤 `METADATA_REVISION_MISMATCH` |
| Cross-replica L2 | Replica A가 새 unique revision의 contract/L2를 publish하고 이미 실행 중인 replica B가 poll로 적용 |
| Metadata fidelity | Quoted SQL names, data types, nullability, composite grain, join/cardinality/fanout exact match |
| Result fidelity | MCP columns, ordered typed rows, revision, bytes, truncation과 canonical hash exact match |
| HTTP parity | HTTP와 MCP의 revision, fingerprint, columns, rows, count, bytes, truncation 동일 |
| Duplicate alias | MCP도 `QUERY_REJECTED / QUERY_DUPLICATE_RESULT_COLUMN` 반환 |
| Deactivate | 같은 열린 replica B MCP session에서 source 목록 제거와 `SOURCE_NOT_FOUND` 확인 |

## Executed Regression

```text
uv run ruff check .                 PASS
uv run mypy src                     PASS (24 source files)
uv run pytest                       PASS (158 unit tests)
uv run pytest -m integration        PASS (14 PostgreSQL/MCP/load tests)
uv run query-man-evaluate           PASS (16/16; max context 13,509 bytes)
uv run query-man-verify             PASS (9/9 bootstrap result contracts)
uv run pytest -m load -s            PASS (40 queries; max plan cost 215.55)
pip-audit                           PASS (0 known vulnerabilities)
gitleaks                            PASS (0 leaks)
Trivy repository                    PASS (0 HIGH/CRITICAL findings)
Trivy PostgreSQL image              PASS (CI-equivalent CRITICAL/fixed policy)
```

## Remaining Deliberate Boundaries

- 제한된 `allowed_sources` caller의 개별 hot-grant는 아직 file policy와 service restart가
  필요하다. 재시작 없는 source 등록 경로는 미래 source까지 명시적으로 신뢰한
  `all_sources` caller로 검증했다.
- MCP query는 선행 question/context token에 묶이지 않는다. 질문→SQL 의미 일치와
  `unsupported`/`needs_clarification` 중단은 Skill/client 책임이며 reviewed verified contract로
  회귀 검증한다.
- Control-plane verified contract는 L2 publish 시 실행된다. Bootstrap CLI처럼 모든 동적
  contract를 주기적으로 재실행하는 runner는 현재 없으므로 운영 smoke/monitoring에서
  canonical invariant를 반복한다.
- 새 workload가 기존 profile을 벗어나면 budget profile review와 현재는 service restart가
  필요하다. 단순 source 추가에는 필요하지 않다.

필수·조건부·불필요 확장 작업은
[`source-extension-checklist.md`](../source-extension-checklist.md)에 유지한다.
