# Source Onboarding And Extension Checklist

기존 physical database에 새 source를 추가하는 repository 변경은 directory 하나에 두 파일을 추가하는
것으로 끝납니다.

```text
config/sources/<source-id>/
├── source.yaml
└── views.sql
```

별도 registry, source ID 목록, test database, business-question corpus, 문서, Compose credential이나 source별
Python 분기를 추가하지 않습니다. 기존 `config/database-profiles.yaml` entry와 DB별 client certificate를
재사용합니다. DB apply와 reader/DN mapping은 repository 변경과 별도의 protected 작업입니다.

현재 repository처럼 production database profile이 없는 상태에서 첫 physical DB를 추가할 때는
`config/database-profiles.yaml`의 version 1 profile도 함께 생성합니다. 이후 같은 DB의 source 추가는 다시
두 파일만 필요합니다. [Query Cave](../query-cave/README.md)는 DB bootstrap, view/reader, 인증서와 direct
admission을 disposable 환경에서 연결한 최초 온보딩 참고 구현입니다.

## `source.yaml`

Manifest version 6의 현재 항목만 사용합니다.

- `source_id`, public `name`과 `description`
- 양의 `view_contract_version`
- `provenance`: owner, environment, `database_migration_ref`
- 기존 `database_profile`과 source별 `reader_user`
- `allowed_schemas`
- exact `allowed_relation_kinds: [view]`
- 기존 `budget_profile`

Password, token, DSN, certificate path와 private key를 넣지 않습니다. Database endpoint, `verify-full`과
client certificate type은 DB profile이 소유하고 credential path는 profile ID에서 결정됩니다. Unknown
field와 database profile reference는 fail-closed합니다.

같은 물리 DB의 source는 한 profile을 공유하되 reader와 curated schema/grant는 source별로 유지합니다.
새 물리 DB를 추가하는 경우에만 database profile과 certificate/HBA lifecycle을
[Database client certificate guide](database-certificate-authentication.md)에 따라 준비합니다.

## `views.sql`

Desired SQL은 bounded standalone transaction이어야 합니다.

- `BEGIN`/`COMMIT`, transaction-local `search_path=pg_catalog`, 짧은 `lock_timeout`
- `CREATE OR REPLACE VIEW <allowed-schema>.<name> (<explicit columns...>)`
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
| Source owner | 두 파일 작성, 기존 DB profile, reader, public 설명·budget과 secret-free manifest 확인 |
| DB/data owner | Exact output, no-PII, row 의미와 base dependency review |
| DBA | Protected target, view/owner/grant, certificate DN-reader mapping과 rollback |
| Runtime | DB profile/source strict load, certificate 연결과 live catalog/reader admission |
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
않습니다. CLI는 certificate file을 읽거나 DB에 연결하지 않으며 output에 credential path나 값을
노출하지 않습니다.

## Protected apply와 rollback

1. Approved commit, database profile, exact target DB/reader, certificate identity, 실행자, backup, stop
   condition과 change-record 위치를 승인받습니다.
2. DB/data owner가 no-PII와 exact view output을 sign off합니다.
3. DBA가 traffic 밖에서 reviewed `views.sql`을 적용합니다.
4. Certificate DN-reader mapping, owner/reader grant, RLS 0개, marker/source/version과 Runtime direct
   admission을 확인합니다.
5. 잘못된 certificate/DN, negative privilege, bounded query, timeout/cancel/rollback과 credential
   redaction을 검증합니다.
6. Application을 재배포·재시작한 뒤에만 source가 startup inventory에 들어옵니다.

Target, dependency, privilege나 output이 예상과 다르면 transaction을 rollback하고 적용을 중단합니다.
Commit 뒤 문제가 발견되면 신규 admission을 막고 직전 view definition과 grant를 DBA가 복구한 뒤 직전
application revision으로 돌아갑니다. Partial apply나 repository PASS를 protected activation 완료로
기록하지 않습니다.
