# ADR 0001: PostgreSQL AST Validation Boundary

Status: Accepted

Date: 2026-08-22

[ADR 0032](0032-reader-temp-admission-relaxation.md)는 아래 unqualified cast 안전 근거 중 reader의
database `TEMP` privilege 부재 전제만 대체한다. 현재 admission은 `TEMP` 보유만으로 source를 거부하지
않으며, 같은 gateway session에 temporary object를 만드는 실행 경로가 없다는 전제를 함께 사용한다.

## Context

Query Man은 모델이 생성한 SQL을 신뢰할 수 없는 입력으로 취급한다. 문자열 prefix나
정규식으로 `SELECT` 여부를 검사하면 comment, CTE, nested statement와 PostgreSQL 고유
문법을 안전하게 판별할 수 없다. Parser는 현재 source database인 PostgreSQL 18 문법과
맞아야 하며 다음 정보를 제공해야 한다.

- 정확한 statement 개수와 최상위 statement 종류
- Nested CTE를 포함한 모든 relation 참조
- Function, operator, cast와 위험한 SELECT option
- Literal과 formatting 차이를 제거한 관측용 fingerprint

## Decision

PostgreSQL 18 parser tree를 제공하는 `pglast` v8을 사용한다. `pglast`는 PostgreSQL
source에서 추출한 parser를 사용하는 `libpg_query`의 Python interface다.

- `pglast`는 `>=8.4,<9`로 고정하고 PostgreSQL major version과 함께 올린다.
- 정확히 하나의 `SelectStmt`만 허용한다.
- CTE 내부를 포함해 `SelectStmt` 외 statement node가 하나라도 있으면 거부한다.
- Physical relation은 schema-qualified name만 허용하고 현재 published catalog와 대조한다.
- CTE 이름만 unqualified relation으로 허용한다.
- Function, operator와 cast type은 보수적인 built-in allowlist를 사용한다.
- 일반 `BETWEEN`은 catalog operator 이름이 아닌 grammar construct로 판별하고 실제 비교
  operator `>=`, `<=`로 정규화해 syntax와 resolved-object 경계에서 같은 allowlist를 적용한다.
  `NOT BETWEEN`과 `SYMMETRIC` 변형은 별도 사용 사례가 검증될 때까지 거부한다.
- Unqualified cast type은 흔한 `date`와 `text`만 허용한다. 나머지 승인 type은
  `pg_catalog.<type>`으로 명시해야 한다. 실행 전에 `search_path=pg_catalog`을 재검증하고, 단일
  `SelectStmt`·DDL/`SELECT INTO` 차단으로 같은 gateway session에서 temporary type을 만들 수 없게
  하므로 두 unqualified type이 user-defined type으로 해석되지 않는다.
- `rank`, `lag`, `lead`, `extract`에 더해 `dense_rank`, `percentile_cont`, `regexp_replace`,
  `position`, `jsonb_build_object`, `to_jsonb`를 일반 분석·문자열·JSON 사용 사례로 승인한다.
  이 함수들도 AST 이름 allowlist와 실행 시점의 `pg_proc` namespace·volatility·privilege
  검증을 모두 통과해야 한다.
- `SELECT INTO`, row locking, recursive CTE, table function, `TABLESAMPLE`, parameter와
  identity를 노출하는 SQL value function을 거부한다.
- 알 수 없거나 아직 정책이 없는 문법은 fail-closed로 거부한다.
- `libpg_query` fingerprint는 metric 집계에만 사용한다. Authorization, result cache key,
  metadata revision 검증이나 보안 결정에는 사용하지 않는다.

초기 validator는 syntax tree 단계의 admission이다. Function/operator overload의 실제 OID,
view security 속성, 권한과 query plan은 PostgreSQL parse analysis 이후에만 확정할 수 있다.
따라서 실행 단계에서 다음 경계를 추가로 강제한다.

- 최소 권한 reader와 `BEGIN READ ONLY`
- Published metadata revision과 relation allowlist 재검증
- Function/operator OID와 volatility 검증
- Transaction-local timeout과 source별 concurrency
- Optional `EXPLAIN (FORMAT JSON)` admission
- Row/byte 상한, cancel과 rollback

## Consequences

- PostgreSQL 문법을 자체 구현하지 않고 실제 parser 계열과 맞출 수 있다.
- Native wheel 의존성이 추가되므로 지원 platform과 PostgreSQL major upgrade 때 호환성
  테스트가 필요하다.
- 보수적인 allowlist 때문에 읽기 전용이지만 아직 승인하지 않은 query도 거부될 수 있다.
  사용 사례와 공격 corpus를 함께 추가한 뒤 allowlist를 확장한다.
- Grammar construct를 허용할 때는 raw AST label을 `pg_operator` 이름으로 추가하지 않고
  PostgreSQL parse analysis가 사용하는 effective operator를 검증해야 한다.
- Fingerprint는 literal을 의도적으로 구분하지 않으므로 원본 SQL을 복원하거나 서로 다른
  query의 실행 권한을 공유하는 용도로 사용할 수 없다.

## References

- [pglast v8 documentation](https://pglast.readthedocs.io/en/v8/)
- [libpg_query](https://github.com/pganalyze/libpg_query)
- [PostgreSQL function volatility categories](https://www.postgresql.org/docs/current/xfunc-volatility.html)
- [PostgreSQL EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html)
