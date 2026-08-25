# ADR 0019: Canonical Time Stability

Status: Accepted

Date: 2026-08-25

Repository implementation: Complete — production cutover remains an environment-specific change

## Context

PostgreSQL `timestamptz`는 같은 instant도 reader session `TimeZone`에 따라 다른 offset의
Python `datetime`으로 반환한다. 기존 result encoder는 그 offset을 그대로 `isoformat()`으로
직렬화했기 때문에 public value와 verified result hash가 session 설정에 의존했다.

반대로 `discovered_on`, `received_on`과 월별 시장 VOC는 한국 업무 달력을 뜻한다. Transport와
reader session을 UTC로 고정하더라도 이 business calendar 의미는 UTC 날짜로 바뀌면 안 된다.

## Decision

변경은 두 release로 직렬화한다.

### R1: business calendar 명시

- `ai.issue_overview.discovered_on`은
  `(discovered_at AT TIME ZONE 'Asia/Seoul')::date`로 계산한다.
- `ai.voc_overview.received_on`은
  `(received_at AT TIME ZONE 'Asia/Seoul')::date`로 계산한다.
- 월별 시장 VOC verified SQL은 SELECT와 GROUP BY 모두
  `date_trunc('month', received_at, 'Asia/Seoul')`을 사용한다.
- View definition hash가 바뀌므로 development와 market metadata revision을 새로 발행하고,
  해당 두 source의 bootstrap verified contract 9개를 모두 새 revision으로 재실행한다.
- Managed source에서는 실제 적용한 R1 database migration artifact를 source provenance의
  `database_migration_ref`로 함께 갱신한다. Repository fixture는 기존 ref가 가리키는 migration
  file 자체가 R1로 versioned되므로 별도 manifest field를 추가하지 않는다.
- R1에서는 reader session과 result encoder를 바꾸지 않는다. Column, row count와 result hash는
  모두 R0와 같아야 하며 다르면 R2로 진행하지 않는다.
- Support와 commerce의 R0 revision, rollback baseline과 immutable verified row는 보존한다.

### R2: transport/session canonical time

Catalog와 Query reader transaction은 `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`
직후의 첫 settings statement에서 transaction-local `TimeZone=UTC`를 설정한다. 공통 reader
verifier는 catalog 조회, resolved-object 검사, `EXPLAIN` 또는 사용자 SQL보다 먼저
`current_setting('TimeZone') = 'UTC'`를 확인한다. Commit, rollback, cancel과 pool 재사용 뒤에는
local setting이 남지 않아야 한다. Database와 role의 default `TimeZone`은 바꾸지 않는다.

Guarded Query는 다음 immutable canonical-time policy material version 1을 소유한다.

```text
version: 1
reader_session_timezone: UTC
aware_datetime: utc_isoformat_plus_00_00
naive_datetime: preserve_isoformat
date: preserve_isoformat
time: preserve_isoformat
timetz: preserve_isoformat
```

- `utcoffset() is not None`인 Python `datetime`만 UTC로 바꾼 뒤 `isoformat()`으로 직렬화한다.
  UTC 표기는 `+00:00`이며 `Z`로 축약하지 않고 Python의 기존 automatic microseconds 동작을
  유지한다.
- Naive `datetime`, `date`, `time`, `timetz`는 기존 `isoformat()` 결과를 보존한다.
- Mapping과 sequence는 같은 규칙을 재귀적으로 적용하고 interval과 나머지 scalar 의미는
  바꾸지 않는다.
- 이 material을 SQL policy digest와 모든 source의 metadata revision material에 함께 넣고
  SQL policy version을 2로 올린다. 따라서 R2에서는 모든 metadata revision과
  `SQL_POLICY_REVISION`이 바뀌며 stale token은 executor 진입 전에 fail-closed한다.
- Public DTO/file/Control table shape는 바꾸지 않고 Control DB schema migration도 만들지 않는다.
  기존 immutable row를 수정하지 않으며 새 metadata revision에 새 verified row를 발행한다.

## Cutover

R1을 별도 commit/release로 먼저 검증한다. R2는 mixed old/new serving fleet를 허용하지 않는
coordinated cutover다.

1. Source/admin/verified mutation을 동결한다.
2. Old fleet의 신규 유입을 막고 active query와 source connection이 0이 될 때까지 drain한다.
3. Route에서 제외한 R2 fleet를 시작한다.
4. 각 source를 L1로 두고 current 및 rollback-preserved verified contract 전체를 새 revision으로
   재실행·재발행한 뒤 L2로 승격한다. Repository fixture 11개도 전부 재실행한다.
5. Replica의 source generation, metadata revision과 ready 상태가 수렴한 뒤 traffic을 연결한다.

R1의 SQL policy revision은
`sha256:83729139d7ccedbe8e299b0c4a8bdefb97d42ca870d5fc3b9c227578c65855d9`다.
Rollback은 R2 fleet를 drain한 뒤 R1 release와 보존된 R1/pre-R2 generation, snapshot, contract를
CAS/rollback으로 다시 활성화하고 L2/ready를 확인한 뒤 old fleet에 route한다. R2 snapshot,
generation과 verified row는 삭제하지 않는다.

Managed contract의 question/SQL/expected 전체를 열거하는 public read API는 현재 없다. 따라서
production cutover 전에는 승인된 operator가 Control DB migration identity로 immutable
`verified_query_contracts`를 제한된 read-only export하여 current와 rollback-preserved inventory를
외부 change record에 고정해야 한다. Inventory 완전성을 증명할 수 없으면 cutover를 중단한다.

## Stop Conditions

- Current/rollback verified inventory가 완전하지 않다.
- R1에서 예상하지 않은 업무 결과 변화나 다른 implicit business-time expression이 발견된다.
- UTC setting 또는 verifier가 승인된 순서보다 늦다.
- SQL policy/metadata revision이 바뀌지 않거나 stale token이 executor에 도달한다.
- R2의 column/row count가 바뀌거나 설명되지 않은 hash 차이가 난다.
- Old fleet drain과 source connection 0을 증명할 수 없다.
- Rollback이 R1 generation/revision/L2/ready를 복구하지 못한다.
- Accepted ADR, persisted schema, runnable test와 module 문서가 서로 다른 의미를 주장한다.

## Consequences

- Instant의 public value와 hash는 database/session default timezone과 무관하게 안정적이다.
- 한국 업무 날짜와 월 경계는 reader UTC 정책과 분리되어 보존된다.
- R2는 wire field나 table migration 없이도 digest와 immutable row가 전환되므로 rolling mixed-fleet
  배포 대신 짧은 coordinated downtime이 필요하다.
- Verified hash가 실제로 달라지는 contract뿐 아니라 current와 rollback-preserved contract 전체를
  새 revision에서 재실행하여 stale evidence를 재사용하지 않는다.

## Implementation Evidence

R1은 별도 commit에서 한국 업무 달력을 SQL에 명시하고 bootstrap 9개 contract의 기존 result hash를
보존했다. R2는 SQL policy version 2와 모든 source의 새 metadata revision을 발행하고 repository
fixture 11개를 모두 재실행했다. UTC/서울/뉴욕 role default, DST, pool reset, old/new immutable
Control row 공존과 local two-replica coordinated cutover 결과는
[canonical time verification](../verification/2026-08-25-canonical-time-stability.md)에 기록한다.

이 evidence는 production의 protected contract inventory, 실제 R1 `database_migration_ref`, backup,
route와 old-fleet drain을 증명하지 않는다. 운영자는 [operations runbook](../operations.md#canonical-time-coordinated-cutover)의
stop condition을 환경별 change record로 충족한 뒤에만 production cutover를 수행한다.
