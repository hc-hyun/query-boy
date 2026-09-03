# Metadata Module

Status: Active

## 30초 요약

Metadata는 최소 권한 reader로 PostgreSQL catalog를 읽고 source/view marker와 구조를 admission한 뒤,
bounded deterministic context와 revision을 제공합니다. 질문 관련도나 업무 규칙을 추론하지 않습니다.

## 책임과 interface

- `PostgresCatalog.collect(source)`: bounded catalog와 reader/admission evidence 수집
- Runtime startup probe의 reviewed inventory 전체 admission
- `MetadataService.get_context(source_id)`: 현재 full admitted catalog와 두 revision 반환
- `MetadataService.assert_revision(...)`: stale metadata/SQL policy fail-closed
- TTL 안의 snapshot 재사용과 bounded stale/retry behavior

Admission은 PostgreSQL 18/UTF-8, exact database/session user, allowed schema의 view-only catalog, RLS 0개,
marker source/version, reader privilege와 metadata hard limit을 확인합니다. Context는 relation과 column을
안정적으로 정렬하고 `max_context_columns_per_relation`, response byte 상한을 적용합니다.

`metadata_revision`은 source/budget/view contract와 admitted relation·column·type/nullability/comment를
포함합니다. `sql_policy_revision`은 Guarded Query가 소유하며 별도 identity입니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `metadata/models.py` | Catalog snapshot과 public context data model |
| `metadata/catalog.py` | PostgreSQL pool, bounded catalog SQL과 direct admission evidence |
| `metadata/revision.py` | Canonical metadata revision material |
| `metadata/service.py` | Startup, cache/refresh, context와 revision check |

Source Catalog는 validated profile을 제공하고 Guarded Query는 published snapshot과 revisions를 소비합니다.
Delivery는 response를 운반할 뿐 catalog를 재해석하지 않습니다.

## 불변조건과 승인

- Partial, over-limit, wrong marker, RLS 또는 reader-policy snapshot은 publish하지 않습니다.
- Cache refresh 실패가 max-stale을 넘으면 이전 snapshot으로 계속 제공하지 않습니다.
- Context/revision material과 stale availability 의미는 policy·wire 변경이므로 별도 승인이 필요합니다.
- Catalog SQL, timeout과 cleanup을 약화하는 변경은 safety 승인과 real-DB 검증이 필요합니다.

## 검증

```bash
uv run pytest tests/test_catalog.py tests/test_metadata.py tests/test_revision.py
uv run pytest -m integration tests/test_database_integration.py
```

## 집중해서 읽을 범위

Catalog/admission은 `catalog.py`와 catalog/integration tests, context/cache는 `service.py`와 metadata tests,
revision은 `revision.py`, Guarded Query mismatch consumer와 revision tests까지 읽습니다.
