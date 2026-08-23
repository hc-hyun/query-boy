# Control-Plane Backup And Disaster Recovery

## Scope And Targets

Control plane의 migration ledger, source profile generations, encrypted credentials, metadata
snapshots와 verified contracts가 대상이다. Source business database backup은 각 source owner의
별도 정책을 따른다.

- 목표 RPO: control schema backup 주기 이하, production 권장 24시간 이내
- 목표 RTO: 새 control database restore와 runtime secret 주입을 포함해 60분 이내
- AES master key는 DB backup과 다른 secret manager/backup domain에 보관한다. 둘 중 하나만
  복구해서는 encrypted reader credential을 사용할 수 없다.
- 현재 ciphertext에는 key version이 없고 runtime은 하나의 direct master key만 받는다. 이
  문서는 backup/recovery를 다루며 online master-key rotation 지원을 의미하지 않는다.

## Migration

1. PostgreSQL과 application version compatibility를 확인한다.
2. Control schema backup과 master-key recovery check를 수행한다.
3. Database owner/관리자용 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`와
   `PGPASSFILE`/managed-auth를 설정하고 `./scripts/apply-control-schema.sh`를 실행한다.
   이 script는 checksum이 일치하는 pending numbered migration을 transaction별로 적용한 뒤
   NOLOGIN writer group/ACL을 반복적으로 reconcile한다. 네 fixture와 seed를 만드는
   `./scripts/apply-db.sh`는 production migration이 아니다.
4. Dedicated runtime LOGIN이 `query_man_control_writer` membership만 상속하는지, replica당
   metadata/source store pool 2개씩을 반영한 유한 connection limit와 TLS가 적용됐는지
   확인한다. Role password/certificate/IAM credential은 SQL repository 밖에서 생성·회전한다.
5. Ruff/mypy/unit/integration과 readiness를 확인한 뒤 traffic을 전환한다.
6. 적용된 filename/checksum과 최신 version을 change record에 남기고 두 번째 실행이 pending
   0건인지 확인한다. Migration은 immutable revision table의 update/delete trigger를 제거하지
   않는다.

## Backup

전용 backup identity로 encrypted backup storage에 다음을 수행한다.

```text
pg_dump --format=custom --schema=control --no-owner --no-privileges query_man
```

Backup job은 dump checksum, PostgreSQL version, migration version/checksum, authority row counts와
생성 시각을 기록하되 manifest, ciphertext나 DSN을 log 본문에 출력하지 않는다. Retention과
access audit는 secret backup과 분리한다. `--no-owner --no-privileges` archive에는 global role,
dedicated LOGIN, membership과 ACL이 포함되지 않으므로 이를 database backup과 별도 change
record/IaC로 복구한다.

## Restore

1. 격리된 새 `query_man` database에 custom archive를 `pg_restore --no-owner --no-privileges
   --exit-on-error`로 복구한다.
2. Restored ledger의 filename/checksum이 checkout과 일치하는지 확인하고 새 database에
   `./scripts/apply-control-schema.sh`를 실행한다. Runner는 pending forward migration을 적용하고
   migration이 없어도 NOLOGIN writer group과 현재 database ACL을 reconcile한다.
3. Dedicated runtime LOGIN을 별도로 복구/생성하고 `query_man_control_writer` membership,
   finite connection limit, TLS와 실제 SELECT/INSERT 권한 및 immutable table UPDATE 거부를
   확인한다.
4. Migration ledger를 포함한 모든 control table row count, FK, immutable trigger와 실제
   immutable UPDATE 거부를 확인한다.
5. 원래 `QUERY_MAN_SOURCE_ENCRYPTION_KEY`와 새 control-writer DSN을 runtime에 주입한다.
6. Runtime을 traffic 없이 시작해 operator health의 active source/component 상태를 확인한다.
7. 모든 active source의 `/meta`에서 revision/quality를 확인하고 guarded query와 verified
   invariant를 실행한 뒤 traffic을 전환한다. Generation은 control DB/change record에서
   별도로 대조한다.
8. Source credential이 별도로 rotation되었다면 restore generation을 활성화하지 말고 새
   credential을 staging/publish한다.

## Recovery Drill

`./scripts/control-plane-drill.sh`는 기존 `query_man_restore_drill` DB가 있으면 덮어쓰지 않고
중단한다. 임시 DB를 생성해 live control schema의 custom archive를 `--no-owner
--no-privileges`로 restore하고 production과 같은 migration runner를 두 번 실행한다. Migration
ledger를 포함한 6개 control table row count, 4개 FK, 3개 non-internal user trigger, 실제
immutable UPDATE 거부와 writer group ACL을 확인한 뒤 임시 DB를 삭제한다.
Production data가 아닌 격리 fixture/복제본에서 분기별로 실행하고 결과를 change record에
첨부한다.

현재 자동 drill은 같은 cluster/current PostgreSQL에서의 archive restore, ledger checksum,
current migration 2회 무오류, row count, FK/user-trigger 개수, immutable mutation 거부와 group
ACL까지 증명한다. Trigger definition 전체, archive content hash, cross-host/version 또는 실제
N-1→N old-schema upgrade는 확인하지 않는다. Source business DB/global role, dedicated LOGIN
생성·실제 인증, ciphertext decrypt, active generation/metadata의 의미 일치와 실제 source query는
위 Restore 3~7단계에서 별도로 확인해야 한다. 이를 실행하지 않은 drill을 full service
recovery로 기록하지 않는다. 위 RPO/RTO는 운영 목표이며 현재 repository drill이 실제 backup
age나 end-to-end recovery 시간을 측정했다는 뜻이 아니다. 전체 복구와 multi-replica acceptance는
`CTRL-09` 범위다.

## Master-Key Change Boundary

`QUERY_MAN_SOURCE_ENCRYPTION_KEY`를 새 값으로 바꾸면 기존 모든 source generation의
credential을 decrypt할 수 없고 rollback history도 사용할 수 없다. 현재 상태에서 환경 변수만
교체하거나 과거 ciphertext를 새 key로 암호화한 것으로 간주해서는 안 된다.

Online rotation을 도입하려면 최소한 key version 저장, old/new key 동시 decrypt, immutable
history를 보존하는 re-encryption migration, replica 전환 순서와 rollback/restore drill이 먼저
필요하다. 그 전까지는 key를 별도 backup domain에 복구 가능하게 유지한다. Reader credential
rotation은 master-key rotation과 다르며 source admin credential endpoint로 수행한다.
