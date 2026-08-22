# Metadata Publishing Verification — 2026-08-23

## Scope

PostgreSQL control plane에서 metadata snapshot의 immutable 저장, atomic active revision,
process restart 복구와 rollback pin을 검증했다.

## Commands And Result

```text
./scripts/apply-db.sh                             PASS (idempotent migration)
uv run pytest tests/test_metadata.py              PASS (7 tests)
uv run pytest -m integration tests/test_metadata_store.py
                                                   PASS (1 PostgreSQL test)
uv run ruff check .                               PASS
uv run mypy src                                   PASS (18 source files)
```

## Evidence

| Boundary | Executed evidence | Result |
|---|---|---|
| Immutable snapshot | Same revision is idempotent; direct SQL UPDATE triggers an exception | PASS |
| Atomic publish | Snapshot insert and active pointer update use one transaction | PASS |
| Restart recovery | New `MetadataService` loads the stored active revision without catalog access | PASS |
| Contract validation | Current manifest와 revision이 다른 stored payload | `METADATA_UNAVAILABLE` 경계에서 fail-closed |
| Rollback | Two revisions publish, previous revision activates, process cache follows it | PASS |
| Rollback pin | Refresh stores a newer snapshot but cannot replace pinned active revision | PASS |
| Resume | Explicit unpin 뒤 다음 refresh가 newer revision을 활성화 | PASS |
| Secret boundary | DSN validation error, snapshot JSON과 public response | Credential value 미노출 |

## Subsequent Completion

- 다중 replica 반영은 push channel 대신 generation 기반 bounded polling으로 구현했다.
- Source manifest와 encrypted secret의 control-plane 등록/hot reload는 `ONB-01`~`ONB-08`에서
  구현했다.
- Backup, restore와 복구 훈련은 `OPS-07`에서 완료했다.
