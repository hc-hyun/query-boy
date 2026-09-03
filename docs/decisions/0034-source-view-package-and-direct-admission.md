# ADR 0034: Source Package, Reviewed Views And Direct Admission

Status: Accepted

Decision ID: `SOURCE-VIEW-01`

## Context

Source definition, desired view SQL, 별도 품질 corpus와 문서 목록이 각각 registration authority가 되면 새
source마다 여러 곳을 동기화해야 합니다. Runtime이 DDL을 적용하거나 고급 업무 의미를 재해석하는 것도
DB owner와 gateway 책임을 섞습니다.

## Decision

### 두 파일 package

Git-reviewed source authority는 exact directory입니다.

```text
config/sources/<source-id>/
├── source.yaml
└── views.sql
```

Manifest version 5는 source ID/name/description, 양의 `view_contract_version`, provenance,
connection environment key, allowed schema, exact view-only relation kind와 기존 budget profile만
정의합니다. Secret 값, DSN, 업무별 semantic overlay나 실행 SQL은 넣지 않습니다. Unknown file/field,
symlink, 중복 key와 environment substitution failure는 startup을 fail-closed합니다.

### Desired view artifact

`views.sql`은 DB/data owner와 DBA가 별도 승인으로 적용하는 desired artifact입니다. Runtime은 이를 열거나
실행하지 않습니다. SQL은 bounded transaction, schema-qualified explicit projection, dedicated owner,
exact revoke/grant와 view/column comment를 포함하고 password, role creation, base DDL·seed와 broad grant를
포함하지 않습니다.

각 live view comment의 첫 줄은 다음 marker입니다.

```text
query-man:source=<source-id>;view-contract=<positive integer>
```

`view_contract_version`은 view 존재, output column, type/nullability, filter/join/aggregation/derivation 또는
security option처럼 공개 query 의미가 달라질 때 올립니다. Description만 명확히 하는 변경은 version을
올리지 않아도 됩니다.

### Direct admission

Metadata는 최소 권한 reader로 live PostgreSQL catalog를 읽고 다음을 query 전에 검사합니다.

- Exact server/database/session identity와 PostgreSQL 18/UTF-8
- Manifest의 allowed schema와 view-only relation kind
- RLS 0개와 reader/base-relation privilege 경계
- 모든 발견 view의 marker source/version 일치
- Relation·column 수와 response byte hard limit

Context는 admission한 전체 relation·column·type·description을 bounded deterministic order로 반환합니다.
Client 질문에 따른 server-side ranking이나 업무 규칙 해석은 하지 않습니다. 복잡한 grain이나 join
안전성은 DB owner가 curated view에 반영합니다.

`metadata_revision`은 source/budget/view contract와 admitted catalog의 구조·description을 digest합니다.
`sql_policy_revision`은 전역 SQL allowlist policy를 별도로 식별합니다. Query는 두 revision이 현재 값과
일치할 때만 실행됩니다.

### Ownership

- Source owner: 두 파일과 public manifest
- DB/data owner: Exact view output, row 의미와 no-PII
- DBA: Protected apply, owner/grant와 rollback
- Metadata: Live catalog/marker/reader admission과 revision
- Guarded Query: SQL validation/execution/cancel·rollback
- Delivery: Caller/source authorization와 HTTP projection

Source별 fixture, question/result registry, 추가 registration manifest와 documentation inventory는 만들지
않습니다. Tests는 discovered package에 공통 동작을 적용합니다.

## Protected apply와 rollback

Repository merge는 DB 변경 권한이 아닙니다. Approved commit, exact target/role, owner sign-off, backup,
traffic-off window, stop condition과 change-record 책임을 별도로 승인합니다. DBA가 reviewed `views.sql`을
적용한 뒤 marker, output, owner/grant, negative privilege와 Runtime direct admission을 확인합니다.

Target/dependency/output/privilege 불일치나 partial failure에서는 transaction을 rollback하고 전환하지
않습니다. Commit 뒤 문제는 신규 admission을 차단하고 DBA가 직전 view/grant를 복구한 뒤 직전
application revision으로 돌아갑니다. Protected activation은 [ADR 0035](0035-reviewed-source-package-inventory.md)의
inventory review와 [Operations](../operations.md)를 함께 따릅니다.
