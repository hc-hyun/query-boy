# ADR 0012: Control-Plane Source Revisions

Status: Accepted

Date: 2026-08-23

## Context

Filesystem manifest와 process-start environment secret만으로는 source 추가, 비활성화와
credential rotation에 runtime 재시작이 필요하다. 관리 입력 검증이 실패했을 때 현재 정상
source나 metadata revision을 변경해서도 안 된다.

## Decision

관리자 source 계약은 manifest document와 reader secret을 별도 필드로 받는다. Manifest는
filesystem과 같은 strict schema, source-scoped credential 이름, budget, overlay 검증을
재사용한다. 현재 schema version은 1이며 version 0의 `budget` 필드는
`budget_profile`로 명시적으로 migration한다. 알 수 없는 미래 version은 거부한다.

Reader secret은 256-bit runtime master key로 AES-256-GCM 암호화한다. Ciphertext는
`source_id`와 generation을 associated data로 묶어 다른 source 또는 revision에서 재사용할
수 없게 한다. Master key와 평문은 control DB에 저장하지 않으며 API 응답에도 포함하지
않는다.

검증된 source profile은 immutable `control.source_profile_revisions`에 generation별로
저장한다. `control.active_source_profiles`가 활성 generation과 enabled 상태를 가리킨다.
Source profile revision, metadata snapshot과 두 active pointer는 동일한 PostgreSQL
transaction에서 publish한다. Optimistic generation check가 동시 관리자 update의
lost update를 막는다.

## Consequences

- 실패한 staging이나 transaction은 기존 active source와 metadata pointer를 유지한다.
- Credential rotation도 같은 manifest와 metadata를 새 generation으로 publish하므로 이전
  secret으로 rollback할 수 있다.
- Source revision ciphertext는 복구 가능해야 하므로 master key backup과 rotation 절차가
  필요하다. 이는 운영 runbook에서 관리한다.
- Runtime poller와 관리자 HTTP endpoint는 이 저장 계약을 사용한다. 각 runtime은 새
  generation을 검증한 뒤 source별 catalog/query pool과 metadata cache를 교체한다.
