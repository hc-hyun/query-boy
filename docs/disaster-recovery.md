# Control-Plane Backup And Disaster Recovery

## Scope And Targets

Control plane의 source profile generations, encrypted credentials, metadata snapshots와 verified
contracts가 대상이다. Source business database backup은 각 source owner의 별도 정책을 따른다.

- 목표 RPO: control schema backup 주기 이하, production 권장 24시간 이내
- 목표 RTO: 새 control database restore와 runtime secret 주입을 포함해 60분 이내
- AES master key는 DB backup과 다른 secret manager/backup domain에 보관한다. 둘 중 하나만
  복구해서는 encrypted reader credential을 사용할 수 없다.

## Migration

1. PostgreSQL과 application version compatibility를 확인한다.
2. Control schema backup과 master-key recovery check를 수행한다.
3. `./scripts/apply-db.sh`의 idempotent migration을 먼저 적용한다.
4. Ruff/mypy/unit/integration과 readiness를 확인한 뒤 traffic을 전환한다.
5. Migration은 immutable revision table의 update/delete trigger를 제거하지 않는다.

## Backup

전용 backup identity로 encrypted backup storage에 다음을 수행한다.

```text
pg_dump --format=custom --schema=control --no-owner --no-privileges query_man
```

Backup job은 dump checksum, PostgreSQL version, row counts와 생성 시각을 기록하되 manifest,
ciphertext나 DSN을 log 본문에 출력하지 않는다. Retention과 access audit는 secret backup과
분리한다.

## Restore

1. 격리된 새 database에 control schema dump를 restore한다.
2. 모든 control table row count, FK와 immutable trigger 존재를 확인한다.
3. 원래 `QUERY_MAN_SOURCE_ENCRYPTION_KEY`와 새 control-writer DSN을 runtime에 주입한다.
4. Runtime을 traffic 없이 시작해 operator health에서 stored generation/metadata/quality를
   확인한다.
5. 세 source의 `/meta`, guarded query, verified invariant를 실행한 뒤 traffic을 전환한다.
6. Source credential이 별도로 rotation되었다면 restore generation을 활성화하지 말고 새
   credential을 staging/publish한다.

## Recovery Drill

`./scripts/control-plane-drill.sh`는 기존 `query_man_restore_drill` DB가 있으면 덮어쓰지 않고
중단한다. 임시 DB를 생성해 live control schema를 stream restore하고 5개 control table의
row count를 비교한 뒤 임시 DB를 삭제한다. Production data가 아닌 격리 fixture/복제본에서
분기별로 실행하고 결과를 change record에 첨부한다.
