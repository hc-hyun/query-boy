# ADR 0013: Control-Plane Verified Query Publishing

Status: Accepted

Date: 2026-08-23

## Context

No-deploy source가 L2가 되려면 현재 metadata revision과 일치하는 verified query가 필요하다.
Filesystem contract만 startup에 읽으면 새 source는 결국 배포나 재시작 없이는 L2로 승격할
수 없다. 운영자가 기대 결과를 확인하지 않고 현재 결과를 자동 승인하는 방식도 회귀 계약의
의미를 약화한다.

## Decision

Operator는 question, SQL, expected relations, columns, row count와 canonical result hash를
`POST /admin/sources/{source_id}/verified-queries`로 제출한다. Runtime은 다음 순서로 검증한다.

1. Contract revision이 현재 published metadata와 일치하는지 확인한다.
2. 기존 AST/object/function/plan/budget gateway를 통해 SQL을 실행한다.
3. SQL이 참조한 relation 집합과 declared relations를 비교한다.
4. Truncation 없이 columns, row count와 result hash가 모두 일치하는지 확인한다.
5. Contract를 immutable control-plane row로 저장한 뒤 runtime revision quality map에 추가한다.

`control.verified_query_contracts`는 source, query ID와 metadata revision으로 식별하며 update와
delete를 금지한다. Runtime startup은 filesystem contract와 control-plane revision 집합을
합친 후 source generation을 load한다.

## Consequences

- Operator가 제공한 expected invariant가 실제 guarded query 결과와 다르면 L2 계약은
  publish되지 않는다.
- 같은 immutable contract의 재요청은 idempotent하지만 같은 key의 다른 payload는 거부한다.
- L0→L1 overlay publish로 revision을 확정한 후 contract를 검증하고, quality minimum만 L2로
  올려 같은 revision을 재publish할 수 있다.
- Contract 폐기가 필요하면 기존 row를 수정하지 않고 새 metadata revision과 새 contract를
  publish한다.
