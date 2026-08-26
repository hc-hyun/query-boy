# Control Plane Persistence And Recovery Reference

Status: Implemented managed capability; outside ADR 0025 static first launch

이 문서는 Control Plane의 persisted/versioned format, transaction, mutation receipt, desired-state
convergence, credential encryption과 recovery 의미를 보존하는 상세 참조다. Managed capability는 구현되어
있지만 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 현재 first launch에는 Control DB나
admin mutation을 조립하지 않는다.

작업 시작점과 공식 Python interface는 [Control Plane README](README.md)다. 이 문서는
[ADR 0012](../../decisions/0012-control-plane-source-revisions.md),
[ADR 0013](../../decisions/0013-control-plane-verified-query-publishing.md)과
[ADR 0016](../../decisions/0016-centralized-source-management-plane.md)의 구현 baseline을 쉬운 순서로
풀어 쓴다. 의미가 충돌하면 이 문서에서 임의로 선택하지 않고 accepted authority와 runnable test를
기준으로 사용자에게 보고한다.

## 30초 지도

```text
관리 요청
  -> active state와 expected generation/state 확인
  -> candidate를 active runtime과 격리해 검증
  -> source-scoped lock + CAS 아래 immutable row와 active pointer commit
  -> 같은 transaction에 terminal receipt commit
  -> commit 뒤 각 Runtime replica가 polling으로 desired state 적용
```

- Control DB commit은 desired state를 원자적으로 정하지만 모든 replica의 memory까지 하나의 분산
  transaction으로 만들지는 않는다.
- Immutable history는 수정·삭제하지 않는다. Rollback도 과거 row를 고치는 대신 active pointer가
  보존된 generation/revision을 다시 가리키게 한다.
- Commit 뒤 runtime apply가 실패해도 authoritative success receipt를 503으로 뒤집지 않는다.

## Authority와 저장 범위

Managed mode의 canonical authority는 다음과 같다.

| 저장 대상 | Authority와 의미 |
|---|---|
| Source definition과 lifecycle | Control DB의 immutable source generation과 active pointer |
| Metadata snapshot | Immutable snapshot row와 active/pinned revision pointer |
| Verified query | `(source_id, query_id, metadata_revision)`로 식별되는 immutable artifact |
| Reader credential | Source generation에 묶인 encrypted ciphertext |
| Plaintext credential와 master key | Runtime/external secret boundary; Control DB, Git, response와 audit에 저장하지 않음 |
| Mutation audit/result | Authority mutation과 함께 commit되는 immutable terminal receipt |
| Runtime observation | Authority row와 분리된 latest/bounded operational projection |

Managed process는 empty registry와 empty verified map에서 시작해 Control DB만 읽는다. Filesystem source나
verified dataset을 열거나 fallback/merge/write-back하지 않는다. Lifecycle row가 없는 file source는
managed mode에서 absent다. `bootstrap|managed` mode 선택은 Runtime configuration이고 Control DB에 mode,
origin, import marker나 seed digest table을 만들지 않는다.

Source DB의 curated view, reader role/grant와 business schema는 Source DB와 DB-owner migration system의
authority다. Control DB에는 bounded `database_migration_ref`만 기록하며 외부 artifact의 존재를 대신
검증하지 않는다.

## Persisted state model

### Immutable row와 active pointer

- `source_profile_revisions`는 검증된 manifest generation과 encrypted credential을 append-only로 저장한다.
- `metadata_snapshots`는 immutable snapshot/revision history를 저장한다.
- `verified_query_contracts`는 source/query/revision-bound expected result를 append-only로 저장한다.
- `active_source_profiles`와 active metadata pointer만 명시된 transaction과 CAS 아래 변경한다.
- Publish는 source generation, metadata snapshot, source pointer와 metadata pointer를 같은 transaction에
  저장한다.
- Rollback은 source pointer와 metadata pointer를 함께 바꾸고 선택한 metadata revision을 pin한다.
- Resume은 pin을 풀어 다음 automatic metadata publish를 허용하지만 persisted activation freshness를
  초기화하지 않는다.

같은 query ID도 metadata revision이 다르면 별도 verified row다. Metadata revision 재료가 바뀌는 global
transition은 current와 rollback-preserved verified inventory 전체를 새 revision에서 재실행하고 새 row를
append한다. 이전 snapshot, generation과 verified row를 update/delete하지 않는다. Metadata revision을
보존하는 SQL-policy-only 전환은 새 persisted lifecycle을 만들지 않는다.

Owner, environment와 DB migration reference는 strict manifest v2 `provenance` 안에 generation과 함께
저장한다. 이 값만 바꾸는 publish도 새 generation을 만들지만 metadata revision은 바뀌지 않는다.
Rollback은 당시 provenance를 그대로 복원한다.

### Source identity

한 `source_id`의 host, port, database, user, TLS mode와 environment는 최초 bootstrap/profile 또는 첫
managed publish에 고정된다. 후속 generation이 다른 connection identity나 environment로 rebind하려 하면
현재 active generation을 유지하고 거부한다. Credential rotation과 owner/migration-reference 갱신은 이
identity 제약을 위반하지 않는다. 다른 endpoint로 전환하려면 새 source ID와 그 source의 verified
artifact가 필요하다.

### Snapshot codec boundary

Metadata가 소유한 snapshot codec은 immutable Python tuple/read-only mapping과 기존 Control DB JSON
array/object를 상호 변환한다. Python graph의 immutability 전환을 이유로 persisted JSON array/object,
metadata revision이나 historical row를 바꾸지 않는다. Decode 결과와 stored revision이 일치하지 않으면
fail-closed한다.

## Application state transition

`SourceAdminService`의 public Python shape는 [README](README.md#source-administration-application-interface)에
있다. 각 mutation이 시도하는 persisted transition은 다음과 같다.

| Operation | Persisted 결과 |
|---|---|
| Publish source | 새 immutable generation, metadata snapshot과 active source/metadata pointer |
| Rotate credential | 같은 source connection identity와 새 immutable generation |
| Deactivate | Active source를 disabled로 바꾸고 다음 state version 부여 |
| Rollback | Historical generation과 matching metadata revision을 active/pinned로 선택 |
| Resume metadata publish | Current pinned revision을 확인한 뒤 unpin |
| Publish verified query | Exact revision/result에 묶인 immutable verified artifact |

성공 application status는 `published`, `deactivated`, `rolled_back`, `resumed`, `verified`다. Deterministic
validation/state rejection은 safe domain error와 terminal receipt로 남고, transaction 안의 validation/SQL
failure가 partial persisted state를 남겨서는 안 된다.

Candidate publish는 active Runtime과 격리한 temporary `SourceRegistry + MetadataService +
RuntimeCatalogProvider`에서 manifest, reader connection, catalog, quality와 verified result를 검증한다.
Registry application reference는 `SourceReader`로 좁힌다. 이 staging composition root는 production
HTTP/MCP wiring이나 Metadata/Guarded Query 업무 정책을 소유하지 않는다.

별도로 활성화한 non-RLS managed candidate에서 PostgreSQL 18/UTF-8 mismatch와 deterministic
reader-session policy mismatch는 기존 validation failure로 끝난다. Timeout, transport와 그 밖의 driver
failure는 control unavailable로 분류한다. 어느 경우에도 실제 connection value나 lower-level exception을
public result에 넣지 않는다.

RLS manifest는 managed publish/credential rotate staging에서 Source Catalog validation failure로 끝난다.
Generation, metadata와 active pointer를 변경하지 않는다. Cold stored RLS row도 `SourceReloader.validate()`가
`RUNTIME_VALIDATION_REJECTED`로 거부하며 registry에 projection하지 않는다. Historical row/type은
삭제·변환하지 않는다.

## Transaction과 migration 불변조건

- 같은 source의 `source_store.py`와 `metadata_store.py`는 정확히
  `pg_advisory_xact_lock(hashtextextended(source_id, 0))` key를 사용한다.
- Active pointer 변경에는 generation과 monotonic `state_version` CAS를 함께 사용한다. 이는 lost update와
  deactivate→same-generation rollback ABA를 막는다.
- Metadata refresh publish는 active source의 `control_generation`, `control_state_version`과 `enabled`를
  함께 확인해 이전 generation의 지연 refresh를 거부한다.
- Success receipt는 source pointer 또는 verified artifact와 같은 transaction에 commit한다.
- Deterministic validation/state rejection은 authority state를 바꾸지 않는 별도 transaction에 terminal
  receipt로 남긴다.
- FK, constraint와 trigger는 application validation을 보완하고 손상된 조합을 거부한다.

Numbered migration은 filename/checksum과 적용 이력을 immutable ledger에 남기고 advisory apply lock 아래
순서대로 실행한다. 과거 migration을 수정하거나 번호를 건너뛰지 않는다. Schema를 먼저 적용한 뒤
application replica를 순차 교체하는 migration-first rollout을 사용한다. Code rollback은 적용된 migration,
table, ledger와 data를 drop하지 않는다.

Control writer는 필요한 table의 SELECT/INSERT/UPDATE와 bounded rollup cleanup에 필요한 제한된 DELETE만
가진다. Schema owner, application owner, source reader를 재사용하지 않으며 authority/history/receipt에는
DELETE/TRUNCATE 권한을 주지 않는다. Observation별 exact privilege는
[observability](observability.md)에 기록한다.

## Mutation receipt와 timeout reconciliation

### Request identity

Public mutation은 caller가 선택한 canonical lowercase UUID idempotency key와 다음 trusted context를
사용한다.

```text
idempotency_key
actor             # authenticated operator caller_id에서 파생
reason            # 1~128자의 ticket/change reference
expected_generation
expected_state_version
expected_metadata_revision  # metadata resume에서만 필수
```

새 source 첫 publish만 expected generation/state `0/0`을 사용한다. 기존 source mutation은 양의 현재값을
사용한다. 두 값 중 하나만 0이거나 resume에 필요한 metadata revision이 없으면 fail-closed한다.

Canonical request envelope에는 operation/source, payload, actor/reason과 expected state가 포함된다.
Credential과 verified question/SQL을 포함할 수 있으므로 raw input이나 일반 digest를 저장하지 않고
idempotency key를 domain에 포함한 keyed HMAC-SHA256으로만 식별한다.

### Terminal receipt

Receipt는 terminal-only immutable row다. Event ID, idempotency key, keyed request hash,
operation/source, actor/change reference, expected/resulting state, outcome/status와 recorded time만 가진다.
Credential, raw manifest, verified question/SQL, metadata snapshot, expected business value와 내부 오류는
저장하거나 반환하지 않는다.

- Same key + same canonical request는 staging/state transition을 반복하지 않고 기존 terminal outcome을
  exact replay한다.
- Same key + different request는 conflict다.
- Success receipt는 committed desired-state authority다.
- Deterministic rejection도 같은 key로 safe error를 재현하고 lookup/history에서 확인할 수 있다.
- Authentication failure와 transient Control/source dependency unavailable은 terminal success처럼 고정하지
  않는다.

### Timeout 뒤 처리

Receipt table에 pending row가 없으므로 lookup 404는 실패가 아니라 staging/in-flight일 수 있다.

1. 원래 key로 receipt lookup을 bounded polling한다.
2. Receipt가 있으면 outcome/resulting state와 source detail을 대조하고 이를 authoritative result로 쓴다.
3. Receipt가 없으면 source generation/state가 요청 전 expected state인지 확인하고 원래 요청이 끝날
   시간을 기다린다. 새 key나 변경된 payload로 blind retry하지 않는다.
4. Bounded wait 뒤에도 receipt가 없고 state가 expected와 같음을 다시 확인한 경우에만 같은 key와 같은
   semantic request를 한 번 재전송한다. Fan-out retry는 하지 않는다.
5. State가 달라졌다면 source mutation history에서 변경 주체를 먼저 확인한다.

Terminal row 생성 전 여러 replica가 같은 key를 동시에 받으면 staging I/O가 중복될 수 있지만
source/key lock와 atomic receipt가 authority commit을 한 번으로 제한한다.

## Desired-state 적용과 convergence

Control DB transaction은 desired state를 commit한다. 각 replica의 `SourceReloader`는 polling으로 다음
순서에 수렴한다.

```text
stored state 검증
-> old source catalog/query pool invalidate
-> registry projection 교체 또는 제거
-> metadata cache invalidate
-> source probe와 process-local health 갱신
```

`SourceReloader`만 Source Catalog의 `SourceProjectionWriter`를 소비하는 runtime projector다. Provider
pool/cache에는 Control Plane이 소비하는 작은 `SourcePoolInvalidator.invalidate(source_id)` port만
요구한다. Runtime이 catalog, query executor 순서로 concrete invalidator를 주입한다. Control Plane은
provider composite private API를 소유하거나 optional하게 탐색하지 않는다.

낮은 state version, 같은 version의 다른 payload, connection identity rebind와 검증 불가능한 revision은
적용하지 않는다. Commit 뒤 같은 process의 apply가 실패하면 component/source health를 unavailable로
표시하고 polling convergence에 맡긴다. Authoritative success receipt를 503으로 뒤집지 않는다.

Control DB가 일시적으로 unavailable해도 이미 적용된 data plane은 안전한 기존 state로 계속 동작할 수
있지만 management operation은 실패한다. Managed cold-start scan이 실패하면 registry는 비어 있고
readiness는 unavailable이며 filesystem bootstrap으로 fallback하지 않는다.

SQL policy/metadata revision을 함께 바꾸는 managed cutover에서는 old fleet를 완전히 drain하고 route
밖의 new fleet에서 source별 L1→all verified→L2를 완료한다. Replica convergence를 확인한 뒤에만 route한다.

## Credential persisted format

Current ciphertext format은 다음과 같다.

| 항목 | 값 |
|---|---|
| Algorithm | AES-256-GCM |
| Master key | 32 bytes |
| Nonce | 12 bytes |
| Associated data | 아래 exact ASCII bytes |

```text
query-man/source/{source_id}/generation/{generation}
```

Ciphertext를 다른 source/generation으로 옮겨 복호화할 수 없다. Plaintext와 master key는 Control DB,
response, audit와 일반 log에 저장하지 않는다. 현재 format은 envelope encryption이나 versioned keyring이
아니므로 algorithm, key/nonce size, AAD 또는 master key를 migration 없이 바꿀 수 없다. Source revision
복구에는 암호화 당시 key가 필요하며 key loss는 DB backup만으로 복구되지 않는다.

현재 direct publish API는 plaintext credential을 받는 trusted manual-admin boundary다. TLS, access-log
body 비기록과 redaction을 요구한다. Plan-only onboarding Skill은 credential을 읽거나 endpoint를 호출하지
않는다. AI executor나 자동화에 production mutation 권한을 주려면 target-bound credential broker,
plan-ID apply와 새 threat model/승인이 먼저 필요하다.

## Recovery와 rollback

Backup/restore는 migration ledger, 모든 authority/history/receipt/observation table과 encryption key를 함께
다룬다. Repository의 isolated recovery acceptance는 다음을 재현한다.

- PostgreSQL 18.4 custom archive를 별도 PostgreSQL 18.6 Control DB에 restore
- 13-table fingerprint와 번호/checksum migration history
- 원래 encryption key로 모든 source generation decrypt
- Immutable source/metadata/verified/receipt history와 logical observation retention
- Filesystem source/verified data가 없는 zero-bootstrap managed start
- 두 stable managed replica의 query와 convergence

이 fixture는 실제 production host/network, backup age, storage access, secret manager, TLS/IAM, source
business DB와 RPO/RTO를 증명하지 않는다. Protected recovery는
[disaster recovery runbook](../../disaster-recovery.md)에 따라 target/access/key/artifact/stop condition을
확인한 별도 실행 승인과 append-only change record가 필요하다.

Code rollback은 applied migration/table/data를 보존한다. Data rollback은 immutable historical row를
수정하는 작업이 아니라 검증된 generation/revision pointer를 원자적으로 다시 선택하는 application
transition이다.

## 변경 중단 조건과 검증

다음 의미는 사용자 승인 없이 변경하지 않는다.

- Control schema, numbered migration, checksum/lock/order와 role/grant
- Immutable key/history, source identity, pointer, pin과 generation/state CAS
- Publish/rotate/deactivate/rollback/resume/verified transition과 transaction atomicity
- Idempotency hash, receipt, replay/conflict와 timeout reconciliation
- Snapshot codec, verified identity와 credential ciphertext/AAD
- Desired-state validation/invalidation/apply/convergence 순서
- Backup/restore/key recovery, migration-first rollout과 protected cutover/rollback procedure

Focused verification:

```text
uv run pytest tests/test_source_admin.py tests/test_secrets.py tests/test_managed_mode.py
uv run pytest -m integration tests/test_source_store.py tests/test_metadata_store.py \
  tests/test_control_migrations.py tests/test_control_startup.py \
  tests/test_control_recovery.py
```

실제 schema/recovery action은 repository test와 별도이며 승인된 protected target에서만 수행한다.
