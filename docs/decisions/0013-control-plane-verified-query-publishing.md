# ADR 0013: Control-Plane Verified Query Publishing

Status: Accepted

Date: 2026-08-23

## Context

No-deploy source가 L2가 되려면 현재 metadata revision과 일치하는 verified query가 필요하다.
Filesystem verified-query dataset만 startup에 읽으면 새 source는 결국 배포나 재시작 없이는 L2로 승격할
수 없다. 운영자가 기대 결과를 확인하지 않고 현재 결과를 자동 승인하는 방식도 회귀 검증의
의미를 약화한다.

## Decision

Operator는 question, SQL, expected relations, columns, row count와 canonical result hash를
`POST /admin/sources/{source_id}/verified-queries`로 제출한다. Runtime은 다음 순서로 검증한다.

1. 제출한 metadata revision이 현재 published metadata revision과 일치하는지 확인한다.
2. 기존 AST/object/function/plan/budget gateway를 통해 SQL을 실행한다.
3. SQL이 참조한 relation 집합과 declared relations를 비교한다.
4. Truncation 없이 columns, row count와 result hash가 모두 일치하는지 확인한다.
5. Verified-query record를 immutable control-plane row로 저장한 뒤 runtime revision quality map에 추가한다.

`control.verified_query_contracts`는 source, query ID와 metadata revision으로 식별하며 update와
delete를 금지한다. [ADR 0016](0016-centralized-source-management-plane.md)의 명시적 runtime mode가
verified-query authority도 process 전체에서 한 번 선택한다.
같은 query ID를 새 metadata revision에 재발행하면 새 immutable row가 생기며 이전 revision row는
rollback evidence로 남는다. Global policy 전환 때는 변경된 hash의 query만이 아니라 current와
rollback-preserved inventory 전체를 새 revision에서 재실행해 새 immutable records로 발행한다.

- `bootstrap` mode는 filesystem verified-query dataset만 사용하고 Control DB 설정을 거부한다.
- `managed` mode는 empty verified map으로 시작해 Control DB verified-query records만 load한다.
  Filesystem verified-query dataset을 열거나 poll 결과와 합치지 않는다.
- Managed lifecycle row가 없는 file source도 absent다. Restart, rollback, deactivate 또는 Control DB
  scan 실패가 filesystem verified-query dataset 또는 source fallback을 일으키지 않는다.
- Production hot-added source의 verified-query record를 filesystem에 write-back하거나 병렬 desired state로
  만들지 않는다.

Bootstrap verified-query dataset을 이관할 때는 traffic 밖의 managed instance에서 source를 L0/L1로 먼저
publish하고 기존 admin endpoint로 reviewed query와 expectation을 실행·저장한 뒤 L2 generation을 publish한다.
`minimum_quality_level`만 L1에서 L2로 바꾸는 것은 metadata revision 재료가 아니므로 같은 exact
exact-revision verified-query records를 사용한다. Startup import, source별 marker, seed digest와 새 import endpoint는
없다. 한 replica가 Control DB verified-query records와 L2 generation을 publish하면 다른 replica가
poll로 같은 managed quality gate를 통과하는 no-deploy 특성은 유지한다.

## Consequences

- Operator가 제공한 expected invariant가 실제 guarded query 결과와 다르면 L2 record는
  publish되지 않는다.
- 같은 immutable record의 재요청은 idempotent하지만 같은 key의 다른 payload는 거부한다.
- L0→L1 overlay publish로 revision을 확정한 후 verified query를 검증하고, quality minimum만 L2로
  올려 같은 revision을 재publish할 수 있다.
- Record 폐기가 필요하면 기존 row를 수정하지 않고 새 metadata revision과 새 verified-query record를
  publish한다.
- 이 verified-query publish 절차는 publish 시점의 execution gate다. Bootstrap
  `query-man-verify`는 filesystem verified-query dataset을 반복 실행하며, control-plane source의 주기적 data-invariant 재실행은 현재 운영 smoke/
  monitoring 절차로 수행한다.
- Managed runtime은 filesystem verified-query dataset entry를 자동 import하거나 L2 evidence로
  인정하지 않는다.
- Canonical-time migration의 protected inventory, L1→verified→L2 cutover와 보존형 rollback은
  [ADR 0019](0019-canonical-time-stability.md)를 따른다.
