# Query Man Architecture

Query Man은 하나의 process에서 reviewed PostgreSQL source를 설명하고, AI가 만든 SQL을 검증한 뒤
제한된 reader로 실행하는 modular monolith입니다.

## 실행 구조

```text
HTTP client
   |
   v
Delivery: authentication, authorization, request/response
   |
   v
Gateway service
   |--------------------|
   v                    v
Metadata             Guarded Query
catalog/revision     AST/plan/transaction/result
   |                    |
   +----------+---------+
              v
       Source Catalog
       source/database profile/budget
              |
              v
       PostgreSQL curated views
```

Runtime만 concrete provider와 lifecycle을 조립합니다. 다른 module은 source 설정을 다시 읽거나
PostgreSQL adapter를 우회하지 않습니다.

## 요청 흐름

1. Delivery가 bearer token을 검증하고 caller의 query 또는 operator capability를 확인합니다.
2. `GET /sources`는 caller가 사용할 수 있는 reviewed source의 public projection만 반환합니다.
3. `POST /meta`는 source authorization 뒤 live catalog를 읽어 source/version marker, reader와 허용
   view를 admission합니다.
4. Metadata는 hard limit 안의 전체 relation·column catalog와 `metadata_revision`,
   `sql_policy_revision`을 결정적으로 반환합니다.
5. `POST /query`는 두 revision 일치, SQL AST, relation·function·operator·cast allowlist를 확인합니다.
6. Guarded Query는 source별 admission과 plan limit을 통과한 SQL만 최소 권한 reader의
   `REPEATABLE READ READ ONLY` transaction에서 실행합니다.
7. Named cursor가 결과를 bounded batch로 읽고 row·byte·OID 제한을 적용합니다.
8. 성공은 commit하고 timeout, cancel, disconnect, shutdown과 오류는 cancel·rollback·cleanup합니다.

HTTP는 `/health`, `/ready`와 operator용 `/admin/health`, `/admin/metrics`도 제공합니다. 어느 endpoint도
DSN, password, token, SQL literal이나 내부 database 오류를 반환하지 않습니다.

## Source와 revision

Startup inventory는 `config/sources/`의 immediate child package 전체입니다.

```text
config/sources/<source-id>/
├── source.yaml
└── views.sql
```

`config/database-profiles.yaml`은 physical database endpoint와 DB별 client certificate authentication을
정합니다. `source.yaml`은 기존 database profile과 source별 reader user, allowed schema/relation kind,
budget과 provenance를 정합니다. 여러 source가 한 database profile을 공유해도 source inventory와
metadata/query pool은 source별로 유지됩니다.
`views.sql`은 DB owner가 별도 승인으로 적용하는 desired curated-view artifact이며 Runtime은 실행하지
않습니다. Live view comment의 `query-man:source=<id>;view-contract=<version>` marker가 package와 다르거나
reader가 허용 범위를 벗어나면 source는 fail-closed합니다.

Metadata revision은 source 설정, budget, admitted relation·column·type·comment와 view contract를
포함합니다. SQL policy revision은 전역 AST/function/operator/type 정책을 나타냅니다. Client는 같은
context의 두 값을 query에 전달해야 하며 mismatch에서는 context를 다시 받아야 합니다.

## 안전 경계

- 외부 입력은 strict schema와 크기 제한을 통과해야 합니다.
- SQL은 하나의 read-only `SELECT` 또는 `WITH`만 허용하며 PostgreSQL AST로 검사합니다.
- Resolved relation, function과 operator도 live catalog에서 다시 검증합니다.
- Reader role, database, session, read-only, isolation과 UTC 설정을 transaction 안에서 확인합니다.
- Timeout, concurrency, pool, plan cost/rows/nodes, memory/temp, result row/byte와 exact result OID를
  제한합니다.
- RLS source는 현재 지원하지 않으며 DB connection 전 거부합니다.
- Revision이 포착하지 못하는 privileged DDL/function/operator/collation/semantic setting drift는
  reviewed package inventory와 serving freeze로 완화합니다.

## Module 책임

| Module | 책임 |
|---|---|
| Source Catalog | 두 파일 package, database profile, reader와 budget strict validation |
| Metadata | Live catalog admission, bounded context와 revision |
| Guarded Query | SQL policy, execution limit, result encoding, cancel·rollback |
| Delivery | HTTP authentication, authorization와 public wire |
| Runtime | 설정, production composition, readiness와 shutdown |
| Assurance | Security corpus와 integration/container/load repository gate |

작업 위치와 허용 의존은 [Module index](modules/README.md)를 따릅니다.

## 범위 밖

- RLS/tenant serving과 cross-source federation
- Source runtime 등록·reload와 application의 DDL 실행
- 임의 SQL, write transaction과 raw database access
- AI model, prompt hosting과 source 선택 추론
- Multi-replica shared quota 또는 distributed query state

Current authority는 [ADR 0025](decisions/0025-static-non-rls-first-launch.md),
[ADR 0034](decisions/0034-source-view-package-and-direct-admission.md),
[ADR 0035](decisions/0035-reviewed-source-package-inventory.md),
[ADR 0036](decisions/0036-database-profile-client-certificate.md)입니다. Protected 환경 연결과 전환은
[Operations](operations.md)와 [Active TODO](development-todo.md)를 따릅니다.
