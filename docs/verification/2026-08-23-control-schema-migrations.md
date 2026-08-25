# Control Schema Migration And Test Isolation Audit

Status: Complete

## Scope

`CTRL-01`의 numbered Control DB migration, checksum drift 거부, 기존 unversioned development
schema의 data-preserving adoption, repeatable security reconciliation과 disposable integration-test
database lifecycle을 검증했다. `CTRL-05`에서 실제 두 번째 migration을 추가하며 N-1→N 보존,
실패 rollback, concurrent pending apply, rolling writer와 receipt ACL/immutability 증거를 같은
audit에 확장했다.

## Evidence

| Contract | Evidence | Result |
|---|---|---|
| Deterministic migration order | Runner가 연속된 `NNNN_name.sql`만 허용하고 repository file을 정렬한다. | PASS |
| Fresh database apply | 임시 empty database가 `0001_baseline.sql`, `0002_source_mutation_receipts.sql`과 6개 authority table을 만들고 두 checksum을 ledger에 1회씩 기록한다. | PASS |
| Idempotent reapply | Sentinel metadata row, table OID와 ledger signature가 두 번째 apply 전후 동일하다. | PASS |
| Drift and DB-ahead failure | Applied checksum mismatch와 checkout에 없는 version 3가 pending numbered migration/final security reconciliation 전에 non-zero로 거부된다. | PASS |
| Existing baseline adoption | Local development authority counts `143|8|458|4|38`이 version 1 채택 전후 동일했다. | PASS |
| N-1 to N preservation | 복사한 version-1 checkout으로 만든 DB의 기존 5 authority table count/digest가 version 2 receipt table 추가 전후 동일하다. | PASS |
| Failed migration rollback | 임시 version 3의 DDL/DML 뒤 강제 실패가 table, inserted row와 ledger를 모두 rollback하고 정상 checkout reapply를 막지 않는다. | PASS |
| Concurrent pending apply | Barrier로 겹친 두 runner를 database advisory lock이 직렬화하고 version 2 table/ledger를 정확히 한 번 만든다. | PASS |
| Rolling writer and ACL | Migration 전 열린 version-1 writer session이 전후 기존 read/insert를 계속하고 receipt SELECT/INSERT/identity usage만 새로 얻으며 UPDATE/DELETE와 sequence mutation은 거부된다. | PASS |
| Receipt immutability | DB owner의 receipt UPDATE/DELETE도 trigger가 거부하고 원본 terminal row를 보존한다. | PASS |
| Restore security | `--no-owner --no-privileges` archive restore 후 같은 runner가 role/ACL을 복구하고 runtime writer의 ledger/immutable-table mutation을 거부한다. | PASS |
| Stale over-grant recovery | Migration identity가 직접 부여한 writer DB/schema/table/column/sequence/function over-grant, grant option, PUBLIC ACL과 inherited parent role을 모두 회수하고 runtime allowlist만 복원한다. | PASS |
| Delegated ACL and ownership | Non-owner grantor ACL, delegated writer grant option과 writer-owned Control object는 조용히 통과하지 않고 exact postcondition에서 fail-closed한다. 다른 grantor의 parent membership도 회수하거나 명시적으로 fail-closed한다. | PASS |
| Mixed-checkout safety | Version-1 runner가 final reconciliation 직전 멈춘 사이 version 2가 commit돼도 전체 ledger 재검증이 old runner를 먼저 거부하고 version-2 ACL을 보존한다. | PASS |
| Test isolation | 모든 Control DB mutation/upgrade scenario가 UUID database와 invocation별 container staging path를 사용하고 pool 종료 뒤 database/directory를 삭제한다. | PASS |
| Development history isolation | 각 disposable fixture 전후 6개 development authority table의 count와 row digest가 동일하다. | PASS |
| Secret boundary | Migration command와 failure에는 password, DSN, manifest, ciphertext 또는 SQL payload를 출력하지 않는다. | PASS |

Migration DDL과 ledger insert는 같은 transaction에 있고 database advisory lock으로 직렬화된다.
Global `query_man_control_writer` 생성/hardening과 DB별 ACL은 numbered history와 분리된
`reconcile-security.sql`을 매번 실행한다. Restore archive가 이미 ledger를 포함해 numbered
migration을 건너뛰더라도 role/ACL은 복구된다. Control DB의 PUBLIC CONNECT/CREATE/TEMPORARY,
writer의 direct over-grant와 inherited membership을 revoke한 뒤 필요한 권한만 다시 grant한다. 이
DB는 전용이고 writer는 migration/object owner와 분리되어야 한다. Migration identity는 target
database/schema/control object owner 권한과 writer를 create·alter하고 membership을 회수할 cluster
role 관리 권한을 모두 가져야 한다.

`CTRL-05`의 첫 전체 concurrent run은 numbered migration이 직렬화된 뒤 두 runner의 security
reconciliation이 동시에 `ALTER ROLE`을 실행해 PostgreSQL catalog `tuple concurrently updated`를
발견했다. Repeatable reconciliation도 같은 database advisory lock 안의 single transaction으로
옮겼고 deterministic overlap 및 전체 integration 재실행이 통과했다.

Mixed-checkout regression은 version-1 runner가 첫 ledger read 뒤 멈춘 사이 version 2가 commit된
경우를 고정했다. Final security reconciliation 직전에 같은 database lock과 transaction 안에서
repository의 전체 filename/checksum ledger를 다시 비교하므로 old runner가 version-2 ACL을
회수하기 전에 실패한다. `psql` 조건 분기의 진단은 stdout suppression에 사라지지 않도록 `\warn`으로
보내고 `ON_ERROR_STOP`이 처리하는 SQL 오류로 non-zero exit를 강제한다.

## Commands

```text
bash -n docker/postgres/init/05-control-plane.sh \
  docker/postgres/init/control-migrations/apply.sh \
  scripts/apply-control-schema.sh scripts/apply-db.sh scripts/control-plane-drill.sh
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -m integration
./scripts/apply-db.sh
./scripts/control-plane-drill.sh
```

초기 `CTRL-01` 검증 시 migration 전후 development authority row count는 각각
`143|8|458|4|38`이었고 ledger만 version 1로 추가됐다. 현재 restore drill 계약은 migration
ledger를 포함한 7개 table, 4개 FK, 4개 immutable trigger, 실제 immutable history/receipt
UPDATE·DELETE 거부와 writer/identity-sequence ACL이었다. `CTRL-05`의 focused migration integration은
11건 모두 통과했으며 최종 전체 suite 결과는
[source mutation receipt audit](2026-08-23-source-mutation-receipts.md)에 기록한다.

전체 integration 재검증에서 replica가 metadata revision과 quality만 먼저 관측해 새 source
generation 적용까지 끝난 것으로 오판하는 test convergence race를 발견했다. Hot-add 검증의
수렴 조건에 실제 `control_generation` 일치를 포함했고, 해당 multi-replica scenario를 3회
연속 통과한 뒤 전체 integration suite를 다시 통과했다.

## Deliberate Limits And Future Triggers

- Version-1 writer의 additive schema 호환성은 증명했지만 이전 application은 receipt를 쓰지 않는다.
  Rolling release에서 admin mutation traffic은 새 replica로만 보내고 old admin replica를 drain해야
  한다. 이 감사 당시 별도 PostgreSQL service/version restore는 아직 검증하지 않았다.
- Database advisory lock은 cluster-global writer role을 서로 다른 Control DB 사이에서 직렬화하지
  않는다. 현재 migration, restore drill과 disposable migration job은 cluster 단위로 겹치지 않게
  실행한다. 병렬 Control DB 운영이 필요해지면 fixed-database/external coordination을 설계한다.
- ACL/membership remediation 뒤 기존 `SET ROLE` session은 자동 퇴출되지 않는다. Admin traffic을
  닫고 writer pool을 drain/recycle한 뒤 fresh connection 권한을 다시 검증한다.
- Test process crash로 남은 scratch database가 반복 관측되기 전에는 prefix/age cleanup service를
  만들지 않는다. CI 비정상 종료 잔여물은 ephemeral Compose volume 폐기로 정리한다.
- Existing local development history는 사용자 데이터로 취급해 자동 삭제하지 않았다.
- Isolated cross-service/minor-version restore, encryption-key decrypt, zero-bootstrap과 multi-replica
  service
  recovery는 이후 `CTRL-09`의
  [control recovery acceptance](2026-08-25-control-recovery-acceptance.md)에서 검증했다. 이 문서의
  역사적인 migration 결과가 그 별도 증거를 대신하지 않는다.
