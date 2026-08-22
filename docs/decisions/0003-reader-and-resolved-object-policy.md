# ADR 0003: Reader And Resolved Object Policy

Status: Accepted

Date: 2026-08-22

## Context

PostgreSQL 함수와 operator는 이름만이 아니라 인자 type과 overload resolution으로 실제
OID가 결정된다. 따라서 AST의 이름 allowlist만 검사하면 같은 이름의 user-defined
overload가 선택될 가능성을 독립적으로 차단하지 못한다. Curated view가 base table을
대신 읽는 privilege 경계와 향후 RLS source의 실행 주체도 명시해야 한다.

## Decision

- Source login은 `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`,
  `NOREPLICATION`, `NOBYPASSRLS`이고 자기 database `CONNECT`와 curated `ai` view의
  `SELECT`만 가진다.
- Reader는 database `TEMP`, base schema `USAGE`, `ai` schema `CREATE`를 갖지 않는다.
- 일반 curated view는 별도 `NOLOGIN` owner가 소유한다. Owner는 source base table의
  `SELECT`만 가지고 write 또는 role 관리 privilege를 갖지 않는다.
- RLS가 필요한 source는 일반 owner-rights view를 사용하지 않는다. `security_invoker`
  view와 gateway가 설정한 trusted tenant context를 함께 검증하기 전에는 등록을
  허용하지 않는다.
- Query transaction은 `search_path=pg_catalog`를 transaction-local로 강제한다.
  Physical relation은 AST 정책상 항상 schema-qualified이므로 `ai`를 search path에 둘
  필요가 없다.
- AST에서 승인한 함수와 operator 이름마다 현재 session에서 visible한 candidate OID를
  `pg_proc`와 `pg_operator`에서 조회한다. 모든 candidate와 operator 구현 함수는
  `pg_catalog` namespace이고, `VOLATILE` 또는 `SECURITY DEFINER`가 아니며 reader가
  실행 가능한 경우에만 SQL planning을 진행한다.
- `EXPLAIN`이 PostgreSQL의 실제 type/overload resolution을 수행한다. 고정된 search
  path와 안전한 전체 candidate 집합 때문에 선택된 OID도 같은 정책 안에 있다.

이 정책은 PostgreSQL의
[function security](https://www.postgresql.org/docs/18/perm-functions.html),
[function/operator type resolution](https://www.postgresql.org/docs/18/typeconv.html),
[`pg_proc`](https://www.postgresql.org/docs/18/catalog-pg-proc.html)와
[`pg_operator`](https://www.postgresql.org/docs/18/catalog-pg-operator.html) 계약을
기준으로 한다.

## Consequences

- `random`, `nextval`, advisory lock, sleep처럼 volatile하거나 side effect가 있는 함수는
  이름 allowlist와 database candidate 검증 양쪽에서 거부된다.
- 신규 extension 함수는 이름만 profile에 추가할 수 없다. Trusted namespace, owner,
  volatility, privilege와 resource 특성을 별도 ADR로 승인해야 한다.
- RLS onboarding은 caller/tenant authorization과 pool reset 검증이 선행되어야 한다.
