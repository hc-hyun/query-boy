# ADR 0003: Reader And Resolved Object Policy

Status: Accepted; connection compatibility extended and RLS launch branch superseded by ADR 0025;
database TEMP admission requirement superseded by ADR 0032

Date: 2026-08-22

[ADR 0025](0025-static-non-rls-first-launch.md)는 기존 transaction/session 검증 앞에 PostgreSQL 18과
server/client UTF-8을 확인하는 no-SQL connection admission을 추가한다. 이 문서의 reader privilege,
read-only transaction과 resolved-object 정책은 그대로다. 다만 현재 launch에서는 모든 RLS source가
bootstrap/injected/managed admission에서 선행 차단되므로 아래 `security_invoker`/trusted-context
등록 branch는 preserved historical capability이며 serving 가능한 현재 절차가 아니다.
[ADR 0032](0032-reader-temp-admission-relaxation.md)는 database `TEMP` privilege 부재 요건만 대체한다.
Reader의 `TEMP` 보유 여부는 admission 조건이 아니며 나머지 privilege·session 정책은 유지한다.

## Context

PostgreSQL 함수와 operator는 이름만이 아니라 인자 type과 overload resolution으로 실제
OID가 결정된다. 따라서 AST의 이름 allowlist만 검사하면 같은 이름의 user-defined
overload가 선택될 가능성을 독립적으로 차단하지 못한다. Curated view가 base table을
대신 읽는 privilege 경계와 향후 RLS source의 실행 주체도 명시해야 한다.

## Decision

- Source login은 `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`,
  `NOREPLICATION`, `NOBYPASSRLS`이고 자기 database `CONNECT`와 curated `ai` view의
  `SELECT`만 가진다.
- Reader는 base schema `USAGE`와 `ai` schema `CREATE`를 갖지 않는다. Database `TEMP` privilege 보유는
  admission 조건이 아니며 temporary relation을 사용자 기능으로 허용하지도 않는다.
- 일반 curated view는 별도 `NOLOGIN` owner가 소유한다. Owner는 source base table의
  `SELECT`만 가지고 write 또는 role 관리 privilege를 갖지 않는다.
- RLS가 필요한 source는 일반 owner-rights view를 사용하지 않는다. `security_invoker`
  view와 gateway가 설정한 trusted tenant context를 함께 검증하기 전에는 등록을
  허용하지 않는다.
- Query transaction은 `search_path=pg_catalog`를 transaction-local로 강제한다.
  Physical relation은 AST 정책상 항상 schema-qualified이므로 `ai`를 search path에 둘
  필요가 없다.
- Catalog와 Query transaction은 `BEGIN ... REPEATABLE READ READ ONLY` 직후 첫 settings
  statement로 transaction-local `TimeZone=UTC`를 설정한다. 공통 reader probe가 UTC를 확인한
  뒤에만 catalog, resolved-object, `EXPLAIN`과 사용자 SQL을 실행한다. Role/database default는
  바꾸지 않으며 commit, rollback과 cancel 뒤 pool에서 원래 default가 복원돼야 한다.
- AST에서 승인한 함수와 operator 이름마다 현재 session에서 visible한 candidate OID를
  `pg_proc`와 `pg_operator`에서 조회한다. 모든 candidate와 operator 구현 함수는
  `pg_catalog` namespace이고, `VOLATILE` 또는 `SECURITY DEFINER`가 아니며 reader가
  실행 가능한 경우에만 SQL planning을 진행한다.
- `EXPLAIN`이 PostgreSQL의 실제 type/overload resolution을 수행한다. 고정된 search
  path와 안전한 전체 candidate 집합 때문에 선택된 OID도 같은 정책 안에 있다.
- 함수 승인은 이름 단위이므로 해당 이름의 visible `pg_catalog` overload 전체가 위
  candidate 검증을 통과해야 한다. 따라서 `dense_rank`의 window·hypothetical ordered-set,
  `percentile_cont`의 scalar·array 및 `float8`·`interval`, JSON 함수의 polymorphic form도 같은
  경계에 포함되며 특정 문법만 승인한 것으로 간주하지 않는다.
- 승인한 window·ordered-set aggregate·문자열·JSON 함수도 예외 없이 같은 검증을
  거친다. Window/aggregate 실행 비용은 statement·transaction timeout, `work_mem`, temp file과
  plan admission으로 제한한다.

이 정책은 PostgreSQL의
[function security](https://www.postgresql.org/docs/18/perm-functions.html),
[function/operator type resolution](https://www.postgresql.org/docs/18/typeconv.html),
[`pg_proc`](https://www.postgresql.org/docs/18/catalog-pg-proc.html)와
[`pg_operator`](https://www.postgresql.org/docs/18/catalog-pg-operator.html) catalog 정의를
기준으로 한다.

## Consequences

- `random`, `nextval`, advisory lock, sleep처럼 volatile하거나 side effect가 있는 함수는
  이름 allowlist와 database candidate 검증 양쪽에서 거부된다.
- 신규 extension 함수는 이름만 profile에 추가할 수 없다. Trusted namespace, owner,
  volatility, privilege와 resource 특성을 별도 ADR로 승인해야 한다.
- RLS serving은 현재 전면 차단하며 재개 조건은
  [Active TODO](../development-todo.md#현재-일정에-없는-일)를 따른다.
- Canonical time과 business calendar의 current contract는
  [Guarded Query의 canonical-time identity](../modules/guarded-query/README.md#canonical-time-identity)를
  따른다.
