# ADR 0010: Revision-Scoped Metadata Retrieval Index

Status: Accepted

Date: 2026-08-23

## Context

기존 relevance는 매 요청마다 모든 relation/column/overlay 문자열을 순회하고 exact phrase
boost에 크게 의존했다. Word order가 달라진 paraphrase는 metadata에 같은 핵심 token이
있어도 낮은 점수를 받을 수 있으며 relation 수가 늘면 반복 비용도 증가한다.

## Decision

Metadata revision과 source ID를 key로 in-memory `RelationRetrievalIndex`를 한 번 생성한다.
Relation 이름, column 이름/comment, semantic description/use-for/alias, column alias와 value
hint를 token posting으로 저장한다. Query token은 document frequency 기반 IDF와 field별
weight로 점수화한다.

Exact `use_for`, relation alias와 column alias phrase boost는 deterministic business term을
위해 유지하지만 token score에 더해지는 보조 신호로 사용한다. Index는 metadata revision이
바뀌거나 source cache가 invalidate되면 폐기한다. 결과 선택 threshold와 default relation
fallback contract는 유지한다.

## Consequences

- 같은 revision의 요청은 metadata text를 매번 다시 tokenize하지 않는다.
- Exact word order가 다른 paraphrase도 shared business token으로 relation을 찾을 수 있다.
- In-memory index는 source 수와 published metadata 크기에 비례한다. Immutable control-plane
  snapshot 자체에는 파생 index를 저장하지 않는다.
- Ranking quality는 versioned `quality-evaluation.yaml`의 relation accuracy, answerability
  recall과 context byte gate로 판정하고 CI에서 실행한다.
