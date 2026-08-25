# ADR 0011: Metadata Quality Level Publish Gate

Status: Accepted

Date: 2026-08-23

## Context

Source마다 semantic metadata와 verified contract의 완성도가 다르다. 모든 source를 같은
품질로 표시하거나 운영자 관례만으로 publish하면 grain이 없는 relation 또는 stale
verified SQL이 L2 source로 노출될 수 있다.

## Decision

Source manifest는 최소 `minimum_quality_level`을 `L0`, `L1`, `L2` 중 하나로 선언한다.
Runtime은 catalog validation 뒤 immutable snapshot을 저장·활성화하기 전에 실제 level을
자동 판정한다.

- L0: Reader 권한과 catalog/overlay referential validation을 통과한 physical catalog
- L1: L0 + 모든 공개 relation의 semantic entry, grain, description, event/comment/population
  relation의 default time
- L2: L1 + source와 현재 metadata revision이 일치하는 하나 이상의 verified query contract

실제 level이 manifest의 minimum보다 낮으면 `METADATA_UNAVAILABLE`로 fail-closed하며
snapshot active pointer를 갱신하지 않는다. `POST /meta`와 MCP context는 판정된
`quality_level`을 반환한다. 두 MVP source는 L2를 요구한다.

Verified contract는 startup에 strict schema로 load하고 source별 revision set으로
MetadataService에 전달한다. Control-plane에서 복구하거나 rollback한 snapshot도 현재
manifest와 verified revision에 대해 같은 gate를 다시 통과해야 한다.
Global canonical-time/SQL policy가 바뀌면 모든 source metadata revision이 바뀌므로 값이 같아 보이는
contract도 새 exact revision에서 다시 실행·발행해야 L2가 된다.

## Consequences

- L0 source는 자동 catalog만으로 publish할 수 있지만 L1/L2처럼 표시되지 않는다.
- Semantic metadata가 일부만 있는 source는 L0로 판정되므로 L1 minimum을 우회하지 못한다.
- Schema revision이 바뀌면 기존 verified SQL이 있어도 L2가 아니며 새 contract 검증 전
  active publish가 중단된다.
- No-deploy onboarding의 staging/publish 단계는 같은 assessor를 재사용해야 한다.
- Canonical-time coordinated cutover는 [ADR 0019](0019-canonical-time-stability.md)를 따른다.
