# ADR 0012: Control-Plane Source Revisions

Status: Accepted

Date: 2026-08-23

## Context

Filesystem manifest와 process-start environment secret만으로는 source 추가, 비활성화와
credential rotation에 runtime 재시작이 필요하다. 관리 입력 검증이 실패했을 때 현재 정상
source나 metadata revision을 변경해서도 안 된다.

## Decision

관리자 source input format은 manifest document와 reader secret을 별도 필드로 받는다. Manifest는
filesystem과 같은 strict schema, source-scoped credential 이름, budget, overlay 검증을
재사용한다. 현재 schema version은 2이며 `provenance.owner`, `provenance.environment`,
`provenance.database_migration_ref`를 관리자가 모두 명시한다. 개발 중 schema cutover이므로 이전
version을 runtime에서 자동 변환하지 않고 fail-closed한다.

Reader secret은 단일 256-bit runtime master key로 AES-256-GCM 암호화한다. Ciphertext는
`source_id`와 generation을 associated data로 묶어 다른 source 또는 revision에서 재사용할
수 없게 한다. Master key와 평문은 control DB에 저장하지 않으며 API 응답에도 포함하지
않는다. 현재 구현은 envelope encryption이나 versioned keyring이 아니므로 master key를
제자리에서 교체할 수 없다.

검증된 source profile은 immutable `control.source_profile_revisions`에 generation별로
저장한다. `control.active_source_profiles`가 활성 generation과 enabled 상태를 가리킨다.
Source profile revision, metadata snapshot과 두 active pointer는 동일한 PostgreSQL
transaction에서 publish한다. 새 immutable profile에는 계속 증가하는 generation을
부여하지만 rollback은 이전 generation을 다시 가리킬 수 있다. 별도 `state_version`은
publish, deactivate와 rollback을 포함한 모든 active pointer 전이마다 증가한다.
Optimistic `(generation, state_version)` check가 동시 관리자 update의 lost update와
deactivate→same-generation rollback ABA를 막는다. Runtime poller는 더 오래된 state와 같은
version의 충돌 payload를 적용하지 않는다.

Owner는 권한 principal이 아니라 운영 책임 팀의 bounded slug이고, environment는 source DB의
`production|staging|development|test` 구분이다. DB migration reference는 source DB의 외부
변경 기록을 가리키는 bounded reference이며 Query Man이 그 외부 artifact의 존재를 대신
검증하지 않는다. 이 세 값은 manifest generation과 함께 immutable하게 저장되어 rollback 때도
같이 복원된다. Owner 또는 migration reference만 바꾼 publish는 새 generation을 만들지만 query
metadata revision은 바꾸지 않는다.

Production managed mode의 canonical manifest generation, active/deactivated state와 history는
처음부터 Control DB가 authority다. Runtime은 empty registry에서 lifecycle을 load하며 filesystem
manifest를 열거나 fallback으로 사용하지 않는다. Publish가 filesystem manifest를 생성하거나
write-back하지 않는다. 전체 managed source catalog, zero-bootstrap과 일회성 cutover는
[ADR 0016](0016-centralized-source-management-plane.md)이 확장한다.

Publisher는 `port_env`를 실제 port로 resolve하고 저장 document에서는 환경 변수 참조를
제거한다. `source_id`는 bootstrap profile 또는 최초 control-plane publish의 host, port,
database, user, TLS mode와 environment에 고정되며 후속 generation이 다른 endpoint나
environment로 바꾸려 하면 기존 active generation을 유지하고 거부한다. Credential rotation과
owner/migration-reference 갱신은 이 identity 제약에서 제외된다.

Staging catalog 연결은 session/database identity와 default read-only뿐 아니라 login role의
superuser, database/role 생성, 상속, replication, RLS bypass, 유한한 양수 connection limit,
database TEMP와 공개 schema CREATE 금지를 검사한다. Fixture validation은 숨긴 base schema와
교차 DB 권한도 별도로 검사한다.

Disabled source의 credential rotation은 거부한다. Rollback은 current source identity,
credential, metadata와 verified-query revision membership을 재검증한 뒤 이전 generation을
활성화하고 metadata automatic publish를 pin한다. 원인 점검 뒤 operator가 resume endpoint를 호출하기 전에는 새
source generation publish도 허용하지 않는다.

## Consequences

- 실패한 staging이나 transaction은 기존 active source와 metadata pointer를 유지한다.
- Credential rotation도 같은 manifest와 metadata를 새 generation으로 publish하므로 이전
  secret으로 rollback할 수 있다.
- Source revision ciphertext 복구에는 암호화 당시 master key가 필요하다. Key loss는 database
  backup만으로 복구할 수 없고, 현재 single-key 형식에서는 online master-key rotation도
  지원하지 않는다. Versioned key ID, dual-key read와 immutable history re-encryption migration을
  구현하기 전에는 key를 변경하지 않는다.
- Runtime poller와 관리자 HTTP endpoint는 이 persisted generation model과 lifecycle rules를 사용한다. 각 runtime은 새
  generation을 검증한 뒤 source별 catalog/query pool과 metadata cache를 교체한다.
- 다른 endpoint로 전환하려면 별도 source ID와 새 verified-query record를 사용해야 한다.
- Owner/환경/DB migration provenance와 secret-free generation read API는 같은 immutable manifest를
  재사용한다. Actor/reason/receipt audit와 replica 상태는 ADR 0016의 `CTRL-05`/`CTRL-06`으로
  구현됐으며 내부 규모/사용량 observation은 `CTRL-07A`, public availability/usage projection은
  `CTRL-08`로 구현됐다. Provider monetary cost는 이후 별도 범위다.
