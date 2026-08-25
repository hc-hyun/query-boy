# ADR 0007: Immutable Metadata Publishing And Rollback

Status: Accepted

Date: 2026-08-23

## Context

Process-local metadata cache만 사용하면 재시작 후 마지막 정상 revision을 복구할 수 없고,
refresh 도중 장애가 발생했을 때 여러 row의 snapshot과 active pointer가 어긋날 수 있다.
단순히 과거 pointer로 rollback해도 다음 자동 refresh가 즉시 최신 revision을 다시
활성화하면 복구 상태가 유지되지 않는다.

## Decision

`query_man` PostgreSQL database의 `control` schema에 다음 두 table을 둔다.

- `metadata_snapshots`: `(source_id, revision)` key의 immutable JSONB physical catalog
- `active_metadata_revisions`: source별 active revision, 마지막 검증 activation time과 rollback pin

Snapshot에는 reader가 볼 수 있는 relation, column, type, comment와 view definition hash만
저장한다. Source DSN, credential, runtime budget과 caller policy는 저장하지 않는다. 읽을
때 현재 source manifest로 revision을 다시 계산하고 저장된 revision과 다르면 fail-closed
한다. 따라서 과거 manifest의 권한 범위를 현재 runtime에 암묵적으로 복원하지 않는다.
Row estimate, storage, growth와 usage는 metadata revision 재료가 아니며
[ADR 0016](0016-centralized-source-management-plane.md)의 별도 operational observation으로
관리한다. Observation 갱신은 source generation이나 metadata revision을 만들지 않는다.

정상 refresh는 한 transaction에서 다음을 수행한다.

1. Revision-keyed snapshot을 idempotent하게 insert한다.
2. 같은 revision에 다른 payload가 있으면 거부한다.
3. 다른 revision으로의 active pointer 변경은 source가 pin되지 않았을 때만 허용한다. Pin된
   revision과 같은 snapshot을 정상 재검증하면 pin을 유지하고 activation time만 갱신한다.
4. Commit된 active snapshot을 application cache에 넣는다.

Rollback은 이미 존재하고 현재 source contract와 호환되는 revision만 active로 바꾸며
source를 pin한다. Pin 상태에서도 refresh된 snapshot은 immutable history에 저장하지만
active pointer는 유지한다. 운영자가 automatic publish를 resume하면 pin을 해제하고 다음
refresh에서 새 revision을 활성화한다. Resume 자체는 freshness clock을 초기화하지 않고
즉시 refresh를 예약한다.

Stale age는 process가 snapshot을 읽은 시점이 아니라 active pointer의 `activated_at`에서
계산한다. Control database가 `clock_timestamp() - activated_at`을 millisecond로 계산해
runtime에 전달하므로 process restart나 application/database clock 차이가 stale window를
새로 시작하지 않는다. 음수 age는 fail-closed한다. 정상 refresh가 같은 revision을 다시
검증하면 activation time을 갱신한다. Pin된 revision과 같은 결과도 freshness를 갱신하지만,
다른 candidate는 active pointer와 age를 바꾸지 않아 상한 뒤 unavailable이 된다. Rollback은
대상 source/metadata를 현재 정책으로 재검증한 운영자 선택이므로 activation time을 갱신한다.

Control-plane source의 metadata publish, rollback과 unpin은 active source의
`(generation, state_version, enabled)`를 같은 transaction에서 확인한다. Deactivate 뒤 같은
generation으로 rollback하는 ABA 전이가 있어도 이전 process의 publish가 active pointer를
덮을 수 없다.

`metadata_snapshots`의 UPDATE와 DELETE는 database trigger가 거부한다. Managed runtime은
`QUERY_MAN_SOURCE_MODE=managed`, `QUERY_MAN_CONTROL_DSN`, source encryption key와 ADR 0016의
stable replica ID를 함께 설정한다.
Bootstrap mode는 Control DB 설정을 거부한다. Login은
`query_man_control_writer` group role의 최소 table 권한만 상속해야 한다. Control plane을
설정한 runtime은 저장소 장애나 payload 검증 실패 시 local catalog만으로 우회 publish하지
않는다. Production schema migration은 표준 libpq `PG*` 환경에서
`scripts/apply-control-schema.sh`만 실행하며 fixture 전체를 만드는 `scripts/apply-db.sh`를
사용하지 않는다.

## Consequences

- Process 재시작 뒤에도 active revision을 복구하고 명시적 rollback 상태를 유지한다.
- Process 재시작 뒤에도 저장된 activation provenance를 이어 사용하며 stale 허용 시간이
  늘어나지 않는다.
- Snapshot insert와 active pointer 변경 사이의 부분 publish가 없다.
- Process cache에는 최대 metadata TTL 동안 이전 active pointer가 남을 수 있다. 다중
  replica의 즉시 invalidation은 observability/control-plane 후속 범위다.
- Snapshot은 immutable하므로 보존 기간과 archive 정책 없이 무기한 증가한다. 운영
  retention은 활성/rollback 대상 revision을 삭제하지 않는 별도 절차가 필요하다.
- Metadata store와 source store는 process당 각각 최대 2개, 합계 최대 4개의 control
  connection을 사용한다. Dedicated LOGIN의 유한 connection limit는 replica 수 × 4로 계산하며
  source reader의 별도 query/metadata connection budget과 섞지 않는다.
- Bootstrap mode는 filesystem source/verified fixture만 사용하고 managed mode는 empty registry에서
  ADR 0012의 Control DB lifecycle과 bounded poll만 반영한다. Production authority, zero-bootstrap과
  일회성 admin-API cutover는 ADR 0016을 따른다.
