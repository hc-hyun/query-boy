# Source Onboarding And Extension Checklist

새 PostgreSQL source의 repository 변경은 directory 하나에 두 파일을 추가하는 것으로 끝납니다.

```text
config/sources/<source-id>/
├── source.yaml
└── views.sql
```

별도 registry, source ID 목록, fixture, business-question corpus, 문서나 source별 Python 분기를 추가하지
않습니다. DB apply와 secret 설치는 repository 변경과 별도의 protected 작업입니다.

## `source.yaml`

Manifest version 5의 현재 항목만 사용합니다.

- `source_id`, public `name`과 `description`
- 양의 `view_contract_version`
- `provenance`: owner, environment, `database_migration_ref`
- `connection`: host/port와 optional environment override key, database, reader user,
  `password_env`, explicit `sslmode`
- `allowed_schemas`
- exact `allowed_relation_kinds: [view]`
- 기존 `budget_profile`

Password, token, DSN 값과 private key를 넣지 않습니다. `password_env` 이름만 versioned manifest에 두고
실제 secret은 배포 환경이 제공합니다. Unknown field와 environment substitution은 fail-closed합니다.

`sslmode`는 `disable`, `require`, `verify-full`만 허용합니다. `require`는 hostname을 검증하지 않으므로
승인된 compatibility risk와 CA/SAN 개선 조건이 있어야 합니다.

## `views.sql`

Desired SQL은 bounded standalone transaction이어야 합니다.

- `BEGIN`/`COMMIT`, transaction-local `search_path=pg_catalog`, 짧은 `lock_timeout`
- `CREATE OR REPLACE VIEW ai.<name> (<explicit columns...>)`
- Schema-qualified base relation과 wildcard 없는 explicit projection
- Dedicated `<source>_view_owner` ownership
- `PUBLIC`과 reader 권한 revoke 뒤 필요한 schema `USAGE`, reader view `SELECT`만 grant
- 각 view의 첫 comment line에
  `query-man:source=<source-id>;view-contract=<positive integer>` marker
- 각 view와 column의 bounded description comment

Role/database 생성, password, base table DDL·seed, extension/function/index 생성, broad/default grant,
psql meta-command와 destructive cleanup은 넣지 않습니다. Runtime은 `views.sql`을 열거나 실행하지 않습니다.

View comment는 row grain과 사용상의 제약을 사람이 이해할 만큼 설명하되 application 전용 schema를
중복시키지 않습니다. AI가 안전하게 join·집계할 수 없는 복잡한 grain은 DB owner가 별도 curated view로
해결합니다.

## Version 판단

`view_contract_version`은 다음처럼 공개 view 구조나 query 의미가 달라질 때 올립니다.

- View 추가·삭제·rename
- Output column 추가·삭제·rename, type/nullability 또는 derivation 변화
- Row filter, join, aggregation, grouping이나 security option 변화

설명만 명확히 고치고 output과 query 의미가 그대로면 version을 올리지 않아도 됩니다. 같은 version으로
privileged DDL이나 function/operator/collation/semantic setting이 바뀌는 것은 revision이 모두 포착하지
못하므로 serving freeze와 별도 review 대상입니다.

## 책임

| 주체 | 책임 |
|---|---|
| Source owner | 두 파일 작성, public 설명·budget과 secret-free manifest 확인 |
| DB/data owner | Exact output, no-PII, row 의미와 base dependency review |
| DBA | Protected target 확인, view/owner/grant apply와 rollback |
| Runtime | Package strict load, live catalog/marker/reader direct admission |
| Delivery | Caller 인증 뒤 source authorization |

Query Man은 개인정보를 탐지·masking하는 boundary가 아닙니다. DB owner는 개인정보와 민감정보를 제거한
curated view만 승인해야 합니다. Public description에도 실제 개인정보를 넣지 않습니다.

## Repository 검증

```bash
uv run qm source validate
uv run pytest tests/test_registry.py tests/test_source_view_artifacts.py \
  tests/test_catalog.py tests/test_revision.py tests/test_documentation.py
uv run ruff check .
uv run mypy src
uv run pytest
```

검사는 package를 발견해 공통 규칙을 적용합니다. 새 source 이름·개수를 다른 test나 문서에 등록하지
않습니다. Password environment는 검증용 dummy 값만 사용하며 output에 노출하지 않습니다.

## Protected apply와 rollback

1. Approved commit, exact target DB/role, 실행자, backup, stop condition과 change-record 위치를 승인받습니다.
2. DB/data owner가 no-PII와 exact view output을 sign off합니다.
3. DBA가 traffic 밖에서 reviewed `views.sql`을 적용합니다.
4. Owner/reader grant, RLS 0개, marker/source/version과 Runtime direct admission을 확인합니다.
5. Negative privilege, bounded query, timeout/cancel/rollback과 secret redaction을 검증합니다.
6. Application을 재배포·재시작한 뒤에만 source가 startup inventory에 들어옵니다.

Target, dependency, privilege나 output이 예상과 다르면 transaction을 rollback하고 적용을 중단합니다.
Commit 뒤 문제가 발견되면 신규 admission을 막고 직전 view definition과 grant를 DBA가 복구한 뒤 직전
application revision으로 돌아갑니다. Partial apply나 repository PASS를 protected activation 완료로
기록하지 않습니다.
