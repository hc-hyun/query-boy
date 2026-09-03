# ADR 0003: Reader And Resolved Object Policy

Status: Accepted; database `TEMP` admission condition superseded by ADR 0032

## Context

AST의 function/operator 이름 검사만으로는 PostgreSQL overload resolution과 실제 object OID를
검증할 수 없습니다. Curated view가 base table을 읽는 owner 권한과 query reader 권한도 분리해야 합니다.

## Decision

- Source login은 `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`, `NOREPLICATION`,
  `NOBYPASSRLS`이며 exact database `CONNECT`, `ai` schema `USAGE`와 curated view `SELECT`만 가집니다.
- Reader는 base schema/relation, `ai` schema `CREATE`, write와 role switch 권한을 갖지 않습니다. Database
  `TEMP` 자체는 [ADR 0032](0032-reader-temp-admission-relaxation.md)에 따라 admission 조건이 아닙니다.
- Curated view는 별도 `NOLOGIN` owner가 소유하며 owner는 필요한 base relation `SELECT`만 가집니다.
- RLS source는 현재 [ADR 0025](0025-static-non-rls-first-launch.md)에 따라 전부 선행 거부합니다.
- Catalog/query transaction은 `BEGIN ... REPEATABLE READ READ ONLY` 직후 transaction-local
  `search_path=pg_catalog`과 `TimeZone=UTC`를 설정하고 다른 SQL 전에 검증합니다.
- Commit, rollback, cancel과 pool reuse 뒤 transaction-local setting이 남지 않아야 합니다.
- 허용한 function/operator 이름의 visible candidate OID와 operator implementation function을 catalog에서
  읽고, 모두 `pg_catalog`, non-volatile, non-`SECURITY DEFINER`이며 reader가 실행 가능할 때만 planning합니다.
- `EXPLAIN`이 고정된 search path에서 실제 type/overload resolution과 plan limit을 검증합니다.

Aware timestamp result는 UTC `+00:00` ISO로 canonicalize합니다. Database/role timezone default를 바꾸지
않으며 업무 calendar가 필요하면 curated view의 explicit output으로 versioning합니다.

## Consequences

`random`, `nextval`, advisory lock, sleep과 user-defined overload는 이름 allowlist 하나만으로 승인되지
않습니다. 신규 extension function/operator는 namespace, owner, volatility, privilege, SQL policy revision과
resource 특성을 별도 승인해야 합니다. Reader/session/overload 검증 실패는 내부 details 없는
`QUERY_UNAVAILABLE`로 fail-closed합니다.
