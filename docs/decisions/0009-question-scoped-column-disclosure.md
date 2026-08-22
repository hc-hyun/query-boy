# ADR 0009: Question-Scoped Column Disclosure

Status: Accepted

Date: 2026-08-23

## Context

Relation을 질문 단위로 선택해도 wide view의 모든 column을 반환하면 context byte와 모델
비용이 relation 폭에 비례한다. 단순 lexical match만 남기면 grain key, 시간 기준,
business predicate나 fanout-safe join key가 빠져 올바른 SQL을 만들 수 없다.

## Decision

Versioned budget profile에 `max_context_columns_per_relation`을 두고 initial interactive
값을 40으로 설정한다. 40개 이하 relation은 기존처럼 전부 반환한다. Wide relation은
다음 순서로 column을 선택한다.

1. Grain/physical primary key, local foreign key와 approved semantic join key
2. Default time column, measure source column과 질문에 매칭된 business predicate column
3. Relation selection reason에 연결된 column
4. 질문과 이름, comment, alias 또는 value hint가 직접 매칭된 column
5. 남은 budget을 ordinal 순서의 column으로 채움

필수 column 수가 profile target보다 많으면 필수 column을 숨기지 않고 target을 초과할 수
있다. 전체 metadata response byte hard limit은 계속 최종 상한으로 동작한다. Relation은
`column_count`, `returned_column_count`, `columns_truncated`를 반환하며 하나라도 잘리면
top-level `truncated`도 true다. 반환되지 않은 column만 사용하는 index metadata도 숨기고
`indexes_truncated`로 표시한다.

Column scoping은 context 크기와 모델 입력을 줄이는 disclosure 정책이지 SQL 보안
allowlist가 아니다. Immutable revision과 gateway relation allowlist는 전체 published
snapshot을 사용한다. Column-level 민감정보 정책이 필요한 source는 curated view에서
제거하거나 후속 classification/authorization 정책을 사용해야 한다.

## Consequences

- Wide source의 기본 context 크기는 relation당 40 column 수준으로 제한된다.
- Grain, time, measure, predicate와 join correctness에 필요한 column은 lexical score가 없어도
  유지된다.
- `truncated=true`인 client는 질문을 구체화하거나 허용된 `max_objects` 범위 안에서 context를
  다시 요청할 수 있지만 임의 schema dump를 요청할 수는 없다.
- 현재 MVP curated view는 모두 40 column 이하라 golden revision과 SQL 결과가 바뀌지 않는다.
