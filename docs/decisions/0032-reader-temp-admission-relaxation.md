# ADR 0032: Reader TEMP Admission Relaxation

Status: Accepted

Decision ID: `QB-READER-TEMP-RELAX-20260830`

## Context

PostgreSQL database의 `TEMP` privilege는 보통 `PUBLIC`에 부여됩니다. 이를 source admission 조건으로
검사하면 Query Man이 사용하지 않는 capability 때문에 정상 reader를 거부하지만, privilege를 보유한
별도 session에서는 temporary object를 만들 수 있습니다.

## Decision

Reader가 database `TEMP` privilege를 보유하는지는 source admission 조건이 아닙니다. 이 결정은 다음
방어를 바꾸지 않습니다.

- [ADR 0001](0001-postgresql-ast-validation.md)의 단일 read-only SQL AST 검사
- `SELECT INTO`, write/DDL, unqualified 또는 `pg_temp` relation 거부
- [ADR 0003](0003-reader-and-resolved-object-policy.md)의 exact reader와 resolved-object policy
- `REPEATABLE READ READ ONLY` transaction, timeout, limit, cancel과 rollback

Source manifest schema, metadata/SQL policy revision과 external response는 바뀌지 않습니다. Query Man
밖에서 reader credential을 직접 사용하면 별도 session에서 temporary object가 가능하므로 credential
비공개와 최소 권한 운영은 계속 필요합니다.

## 변경과 rollback

Protected 환경에서 `TEMP`를 revoke할 수 있으면 별도 hardening으로 수행할 수 있지만 Query Man startup
전제는 아닙니다. AST 또는 transaction 방어가 약해지는 변경은 이 ADR의 범위가 아니며 별도 safety
승인과 negative real-DB 검증이 필요합니다.
