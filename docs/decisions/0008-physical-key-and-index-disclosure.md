# ADR 0008: Physical Key And Index Disclosure

Status: Accepted

Date: 2026-08-23

## Context

Table source의 grain과 join 후보를 이해하려면 primary key와 foreign key가 유용하고,
index key는 예상 실행 특성을 검토하는 보조 정보가 된다. 하지만 `pg_catalog`에 존재하는
모든 constraint, expression과 predicate를 그대로 공개하면 reader가 조회할 수 없는
relation이나 내부 업무 조건이 metadata 응답으로 노출될 수 있다. Physical foreign key를
업무적으로 승인된 join으로 자동 해석하면 fanout 정책도 우회된다.

## Decision

Catalog transaction 안에서 relation/column 수집과 같은 권한 predicate로 key와 index를
수집한다.

- Local relation은 allowed schema/kind이며 reader에게 schema `USAGE`, table `SELECT`와
  모든 key column `SELECT`가 있어야 한다.
- Foreign key는 referenced relation과 column도 같은 eligible set 안에 있어야 한다.
- Index는 valid/ready, non-partial이며 모든 key가 단순 column인 경우만 수집한다.
- INCLUDE column, expression, partial predicate, constraint name, index name과 definition은
  응답하지 않는다.

질문 context의 relation에는 `primary_key`, `foreign_keys`, `indexes`를 반환하고 해당
column의 `semantic_roles`에 `primary_key` 또는 `foreign_key`를 추가한다. Non-empty 구조는
metadata revision과 immutable snapshot에 포함한다. Empty 구조는 기존 view-only revision을
불필요하게 변경하지 않도록 revision hash document에서 생략한다.

Physical foreign key는 `joins`에 자동 추가하지 않는다. `joins`는 계속 reviewed semantic
overlay만 제공하며, index 존재 여부도 query 허용이나 비용 안전을 보장하지 않는다.
Planner admission과 timeout/concurrency/result hard limit은 그대로 적용한다.

## Consequences

- L0 table source가 key 구조를 자동 제공할 수 있다.
- 현재 두 MVP source는 curated view만 공개하므로 physical key/index 배열이 비어 있다.
- Partial/expression index가 중요한 source는 안전한 curated view, 설명 또는 별도 reviewed
  semantic metadata로 의미를 제공해야 한다.
- Primary index는 primary key와 index 배열 양쪽에 나타날 수 있다. 이름 대신 column과
  uniqueness만 제공하므로 client는 이를 실행 계획 보장으로 해석하지 않는다.
