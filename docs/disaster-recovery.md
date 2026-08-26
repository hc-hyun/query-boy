# Control-Plane Backup And Disaster Recovery

Status: Managed recovery runbook; ADR 0025 static launch uses the operations rollback path

현재 static launch는 Control DB를 authority로 사용하지 않는다. 두 reviewed source, pinned artifact와
repository configuration의 stop/cutover/rollback은 [operations](operations.md)의
`LAUNCH-01-A` 절차를 따른다. 이 문서는 구현이 보존된 managed mode를 별도로 활성화할 때만
적용하며, local recovery fixture가 protected environment의 backup/TLS/route 증거를 대신하지 않는다.

## Scope And Targets

Control plane의 migration ledger, source profile generations, encrypted credentials, metadata
snapshots, verified-query records, immutable mutation receipts, runtime replica latest observation,
resource current/previous와 latest attempt/last-success, gateway usage rollup/cursor가 대상이다.
Observation table은 source authority나 완전한 billing ledger가 아니라 bounded operational projection을
복원한다. Source business database backup은 각 source owner의 별도 정책을 따른다. Repository
source/verified file은 static launch authority이며 managed desired-state backup이나 recovery
authority가 아니다.

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

Migration 3~5의 schema-first rolling release와 application rollback 절차는
[operations](operations.md#control-db-migration-and-environment-isolation)를 따른다.

## Backup

전용 backup job identity로 encrypted backup storage에 다음을 수행한다. Reconciliation의 direct ACL
grantee는 Control object owner와 `query_man_control_writer`만 허용하므로 backup identity에 table
권한을 직접 부여하지 않는다. Managed backup plane을 사용하거나, logical dump 동안에만 보호된
owner role membership을 부여하고 `--role`로 전환한 뒤 즉시 회수한다. 이 identity는 Control DB
전체를 읽을 수 있으므로 runtime credential과 분리하고 짧은 수명·강한 감사 경계를 적용한다.

```text
pg_dump --role=<control-owner-role> --format=custom --schema=control \
  --no-owner --no-privileges query_man
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
   immutable UPDATE/DELETE 거부를 확인한다. Runtime writer가 receipt table에는 SELECT/INSERT와
   identity sequence USAGE만, replica observation 두 table에는 SELECT/INSERT/UPDATE만 갖고
   DELETE/TRUNCATE는 못 하는지도 실제 role로 검증한다. Resource/attempt/cursor table도
   SELECT/INSERT/UPDATE만, gateway rollup은 SELECT/INSERT/UPDATE/DELETE만 있고 TRUNCATE가 없는지
   확인한다.
5. `QUERY_MAN_SOURCE_MODE=managed`, 원래 `QUERY_MAN_SOURCE_ENCRYPTION_KEY`, 새 control-writer
   DSN, version 2 query/admin access policy와 deployment slot별 원래 `QUERY_MAN_REPLICA_ID`를 runtime에
   함께 주입한다. Bootstrap mode, API-token/anonymous auth나 DSN/key/replica ID 일부만으로 복구하지
   않는다. 같은 slot은 원래 ID를 재사용하고 동시에 실행되는 slot끼리 ID를 공유하지 않는다.
6. Source/verified file에 의존하지 않는 상태로 runtime을 traffic 없이 시작해 admin health의
   active source/component 상태를 확인한다. Cold Control scan 실패 시 file fallback 없이
   readiness가 unavailable이어야 한다.
7. 모든 active source의 `/meta`에서 revision/quality를 확인하고 guarded query와 verified
   invariant를 실행한다. 각 source의 replica endpoint에서 복구한 모든 expected slot이 새
   incarnation으로 `available`, `drift=[]`인지 확인한 뒤 traffic을 전환한다. Generation은 control
   DB/change record에서 별도로 대조한다. 복구하지 않은 과거 slot은 새 report가 없으면 기존
   `fresh_until` 이후 stale로 남는 것이 현재 lifecycle policy다.
8. Source credential이 별도로 rotation되었다면 restore generation을 활성화하지 말고 새
   credential을 staging/publish한다.

## Recovery Drill

`./scripts/control-plane-drill.sh`는 기존 `query_man_restore_drill` DB가 있으면 덮어쓰지 않고
중단한다. 임시 DB를 생성해 live control schema의 custom archive를 `--no-owner
--no-privileges`로 restore하고 production과 같은 migration runner를 두 번 실행한다. Migration
ledger를 포함한 13개 control table row count, 15개 FK, 4개 non-internal user trigger, 실제
immutable history/receipt UPDATE·DELETE 거부와 writer group/receipt-sequence 및 observation-table ACL을
확인한 뒤 임시 DB를 삭제한다.
Production data가 아닌 격리 fixture/복제본에서 분기별로 실행하고 결과를 change record에
첨부한다.

현재 빠른 drill은 같은 cluster/current PostgreSQL에서의 archive restore, ledger checksum,
current migration 2회 무오류, row count, FK/user-trigger 개수, immutable mutation 거부와 group 및
replica-table ACL까지 증명한다. Trigger definition 전체, archive content hash, 별도 service/version,
실제 N-1→N old-schema upgrade, dedicated LOGIN 실제 인증, ciphertext decrypt와 runtime query는
확인하지 않으므로 이 결과만 Control recovery fixture acceptance로 기록하지 않는다.

## Isolated Control Recovery Fixture Acceptance

`CTRL-09`의 별도 integration acceptance는 위 schema drill의 공백을 production interface/format 변경 없이
격리된 다른 PostgreSQL service에서 재현한다. `.env`와 현재 source fixture가 준비되고
migration/restore job이 다른 Control 작업과
겹치지 않는 상태에서 실행한다.

```bash
docker compose up -d --wait postgres
./scripts/apply-db.sh
uv run pytest -m integration -q tests/test_control_recovery.py
```

이 test는 기존 `postgres-control-recovery-source` service가 있으면 덮어쓰지 않는다. `recovery`
profile의 tmpfs PostgreSQL 18.4에 13개 table을 모두 채우고 custom archive를 만들어 현재 18.6의
완전히 빈 random-prefix DB로 single-transaction restore한다. Migration runner 2회 뒤 UTC/C row
fingerprint를 비교하고 archive와 분리된 원래 key로 모든 generation decrypt, wrong-key 거부,
별도 finite writer LOGIN, 기존 receipt replay, 31일 밖 rollup의 물리 보존/조회 제외를 확인한다.
Source/verified directory 없이 원래 두 stable slot을 다시 시작해 새 incarnation,
`available`, `drift=[]`, L2 metadata와 guarded verified query까지 통과해야 한다. 임시 service,
archive, DB, LOGIN과 connection은 항상 정리한다. 상세 결과는
[control recovery acceptance](verification/2026-08-25-control-recovery-acceptance.md)에 기록한다.

Source business DB, production TLS/IAM과 실제 secret manager 복구는 여전히 위 Restore 절차와 각
deployment authority가 확인한다. RPO/RTO는 운영 목표이며 fixture test 시간이 실제 backup age나
end-to-end production recovery 시간을 측정했다는 뜻이 아니다. Fixture source에도 현재 migration을
적용한 뒤 archive하므로 실제 N-1 schema archive upgrade 증거도 아니다.

## Master-Key Change Boundary

`QUERY_MAN_SOURCE_ENCRYPTION_KEY`를 새 값으로 바꾸면 기존 모든 source generation의
credential을 decrypt할 수 없고 rollback history도 사용할 수 없다. Mutation request HMAC key도
같은 master key에서 domain separation해 파생하므로 기존 key의 exact replay hash도 더는 만들 수
없다. 현재 상태에서 환경 변수만 교체하거나 과거 ciphertext를 새 key로 암호화한 것으로
간주해서는 안 된다.

Online rotation을 도입하려면 최소한 key version 저장, old/new key 동시 decrypt, immutable
history를 보존하는 re-encryption migration, replica 전환 순서와 rollback/restore drill이 먼저
필요하다. 그 전까지는 key를 별도 backup domain에 복구 가능하게 유지한다. Reader credential
rotation은 master-key rotation과 다르며 source admin credential endpoint로 수행한다.
