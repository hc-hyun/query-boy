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
- `active_metadata_revisions`: source별 active revision, activation time과 rollback pin

Snapshot에는 reader가 볼 수 있는 relation, column, type, comment와 view definition hash만
저장한다. Source DSN, credential, runtime budget과 caller policy는 저장하지 않는다. 읽을
때 현재 source manifest로 revision을 다시 계산하고 저장된 revision과 다르면 fail-closed
한다. 따라서 과거 manifest의 권한 범위를 현재 runtime에 암묵적으로 복원하지 않는다.

정상 refresh는 한 transaction에서 다음을 수행한다.

1. Revision-keyed snapshot을 idempotent하게 insert한다.
2. 같은 revision에 다른 payload가 있으면 거부한다.
3. Source가 pin되지 않았을 때만 active pointer를 update한다.
4. Commit된 active snapshot을 application cache에 넣는다.

Rollback은 이미 존재하고 현재 source contract와 호환되는 revision만 active로 바꾸며
source를 pin한다. Pin 상태에서도 refresh된 snapshot은 immutable history에 저장하지만
active pointer는 유지한다. 운영자가 automatic publish를 resume하면 pin을 해제하고 다음
refresh에서 새 revision을 활성화한다.

`metadata_snapshots`의 UPDATE와 DELETE는 database trigger가 거부한다. Runtime은
`QUERY_MAN_CONTROL_DSN`으로 별도 control-plane connection을 설정한다. Login은
`query_man_control_writer` group role의 최소 table 권한만 상속해야 한다. Control plane을
설정한 runtime은 저장소 장애나 payload 검증 실패 시 local catalog만으로 우회 publish하지
않는다.

## Consequences

- Process 재시작 뒤에도 active revision을 복구하고 명시적 rollback 상태를 유지한다.
- Snapshot insert와 active pointer 변경 사이의 부분 publish가 없다.
- Process cache에는 최대 metadata TTL 동안 이전 active pointer가 남을 수 있다. 다중
  replica의 즉시 invalidation은 observability/control-plane 후속 범위다.
- Snapshot은 immutable하므로 보존 기간과 archive 정책 없이 무기한 증가한다. 운영
  retention은 활성/rollback 대상 revision을 삭제하지 않는 별도 절차가 필요하다.
- Control-plane pool은 process당 최대 2 connection을 사용하며 source reader의 query 2 +
  metadata 1 connection budget과 분리한다.
- Source manifest 자체는 아직 filesystem에서 시작 시 읽는다. Hot reload와 no-deploy
  onboarding은 `ONB-01`~`ONB-08`에서 구현한다.
