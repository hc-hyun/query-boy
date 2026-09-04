# Source Catalog Module

Status: Active

## 30초 요약

Source Catalog는 `config/sources/`의 두 파일 package, DB profile과 budget YAML을 strict validation해
immutable `SourceProfile`을 만듭니다. DB에 연결하거나 `views.sql`을 실행하지 않습니다.

## 책임과 interface

- `SourceRegistry.load(...)`: DB profile을 resolve하고 모든 immediate child source package를 load
- `source_ids()`, `get(source_id)`: immutable startup inventory 조회
- Manifest version 6, exact two-file layout, DB profile/reader와 view-only allowlist 검증
- Database profile version 1, client-certificate `verify-full`과 disposable-test-only password validation
- Budget schema, 범위와 `max_concurrent_queries <= max_pool_size` 같은 cross-field 검증
- PostgreSQL reader connection/session policy descriptor 제공

Production client certificate path는 runtime credential root와 DB profile ID에서 결정하며 private
material을 읽거나 public projection에 노출하지 않습니다. Unknown field/file, YAML duplicate, unresolved
test secret, RLS/tenant 설정과 retired alternate authority는 fail-closed합니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `source_catalog/models.py` | Source, connection, provenance와 budget data class |
| `source_catalog/registry.py` | Package/YAML loading과 strict validation |
| `source_catalog/reader_policy.py` | Reader connection/session policy |
| `config/sources/` | Source별 `source.yaml`, `views.sql` |
| `config/database-profiles.yaml` | 물리 DB endpoint와 DB별 client-certificate policy |
| `config/budget-profiles.yaml` | Versioned resource budget |

Metadata는 resolved profile을 소비해 live catalog를 admission합니다. Delivery와 Runtime이 YAML을 다시
해석하거나 별도 source 목록을 유지하지 않습니다.

## 불변조건과 승인

- Startup inventory는 discovered valid package 전체이며 partial fallback이 없습니다.
- Runtime은 desired SQL을 실행하지 않습니다.
- Source ID, database profile, reader, relation allowlist, view contract, provenance, budget schema/값은 persisted 또는
  policy 의미이므로 변경 전 사용자 승인이 필요합니다.
- Secret resolution과 reader privilege를 약화하는 변경도 별도 safety 승인이 필요합니다.

두 파일 계약과 protected apply는 [ADR 0034](../../decisions/0034-source-view-package-and-direct-admission.md),
[ADR 0036](../../decisions/0036-database-profile-client-certificate.md),
[Source extension checklist](../../source-extension-checklist.md)를 따릅니다.

## 검증

```bash
uv run pytest tests/test_registry.py tests/test_reader_policy.py \
  tests/test_source_view_artifacts.py tests/test_revision.py
```

## 집중해서 읽을 범위

Package/schema 변경은 `registry.py`와 `test_registry.py`, reader 변경은 `reader_policy.py`와
`test_reader_policy.py`, budget 변경은 models/registry/query consumer와 bounded load test까지 읽습니다.
