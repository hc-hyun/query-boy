# No-Deploy Source Onboarding Verification — 2026-08-23

## Scope

Operator source admin 계약, isolated staging, atomic publish, runtime reload, credential rotation,
rollback/deactivate와 세 번째 PostgreSQL fixture acceptance를 검증했다.

## Acceptance Evidence

| Scenario | Result |
|---|---|
| 잘못된 manifest/schema update | 기존 active generation과 runtime profile 유지 |
| 동시 update generation 충돌 | Lost update 없이 `SOURCE_GENERATION_CONFLICT` |
| 외부 process가 publish한 generation | Poller가 재시작 없이 registry/cache/pool 교체 |
| Credential rotation | 새 credential staging 후 generation 교체, 후속 query PASS |
| Credential/API response | 평문 secret 비노출 |
| Deactivate | 같은 process의 `/sources`에서 즉시 제거 |
| Rollback | 대상 profile/secret/metadata 품질 사전 검증 후 active pointer 복구 |
| 세 번째 source | 독립 `support_tickets` DB와 reader를 admin API로 L0 publish |
| End-to-end | `PUT admin → /sources → /meta → /query` PASS, 3 queue/120 tickets |

세 번째 fixture는 [`config/onboarding/support-tickets.yaml`](../../config/onboarding/support-tickets.yaml)
manifest를 사용한다. 이 파일은 bootstrap registry에 포함되지 않으며 application의
`source_id` 분기 코드도 추가하지 않았다.

## Regression Results

```text
uv run ruff check .                 PASS
uv run mypy src                     PASS (23 source files)
uv run pytest -q                    PASS (108 unit tests)
uv run pytest -m integration -q     PASS (12 integration tests)
uv run query-man-evaluate           PASS (16/16 cases, max 13,509 bytes)
uv run query-man-verify             PASS (9/9 verified SQL contracts)
```
