# Source Mutation Receipt Audit — 2026-08-23

Status: Complete

## Scope

`CTRL-05`의 여섯 direct source-admin mutation에 공통 idempotency, authenticated actor/change
reference, expected-state compare-and-set, atomic terminal receipt와 append-only lifecycle history를
추가했다. 별도 approval/request/pending table, 이전 endpoint compatibility branch, caller별 ACL과
AI credential executor는 만들지 않았다.

## Implemented Contract

- Publish, credential rotation, verified-query publish, rollback, metadata resume와 deactivate는
  canonical lowercase UUID key, bounded change reference와 expected generation/state를 요구한다. 새
  source publish만 `0/0`이며 metadata resume은 expected pinned revision도 요구한다.
- Actor는 access policy의 authenticated operator caller ID에서 파생한다. Query/anonymous caller는
  path, query, header와 body parsing보다 먼저 거부된다.
- Canonical request envelope에는 idempotency key, operation/source, actor/reason, expected state와 전체 semantic
  payload가 포함되지만 메모리에만 존재한다. Source encryption master key에서 domain separation한
  HMAC-SHA-256만 receipt에 저장한다.
- `control.source_mutation_receipts`의 terminal row 하나가 idempotency receipt와 lifecycle event를
  겸한다. Source pointer/contract와 success receipt는 같은 transaction에서 commit한다. 결정적인
  validation/conflict rejection은 source state를 바꾸지 않고 immutable receipt만 commit한다.
- Terminal receipt가 있는 same key/same request는 저장된 결과를 반환하며 staging, query 실행과
  state transition을 다시 하지 않는다. Same key에서 credential, manifest/array order, actor, reason, operation 또는 expected
  state가 달라지면 bounded 409다. JSON object key order는 canonicalization한다.
- Receipt/result projection은 operation별 exact field만 허용한다. Plaintext credential, raw
  manifest, metadata snapshot, verified question/SQL과 내부 database error는 table, response와
  metric에 없다.
- `GET /admin/mutations/{idempotency_key}`와
  `GET /admin/sources/{source_id}/mutations`는 operator-only terminal lookup과 event-ID keyset
  history를 제공한다.

## Evidence

| Boundary | Evidence | Result |
|---|---|---|
| Exact replay | 같은 publish key/request의 순차·동시 호출이 정확히 한 generation/state transition과 한 receipt를 만들고 나머지는 authoritative replay를 얻는다. Terminal receipt 뒤 순차 replay는 staging도 반복하지 않는다. | PASS |
| Conflict | 같은 key의 다른 hash/actor/reason/expected state/payload를 staging 전에 409로 거부한다. | PASS |
| Keyed hash | 같은 key/request의 HMAC은 deterministic하고 idempotency key/request/master key 변경에는 달라지며 digest에 low-entropy payload가 나타나지 않는다. | PASS |
| Atomic success | Receipt identity-sequence failure를 강제하면 source revision, metadata/pointer와 receipt가 모두 rollback된다. | PASS |
| Atomic verified/resume | Commit-time generation/state/active-revision guard와 receipt 실패가 verified contract 또는 metadata unpin만 남기지 않는다. | PASS |
| Rejection replay | Deterministic validation rejection은 state를 바꾸지 않은 terminal receipt가 되고 exact replay도 같은 공개 오류다. | PASS |
| Immutable audit | Writer ACL과 owner trigger가 receipt UPDATE/DELETE를 모두 거부하며 original row를 보존한다. | PASS |
| Safe projection | Decoder와 result exact-shape corpus, receipt GET/list pagination이 secret/manifest/question/SQL 없는 bounded projection만 허용한다. | PASS |
| Authorization order | Query/anonymous의 malformed path/body/header도 403/401이고 backend를 호출하지 않는다. Operator의 누락·중복·범위 밖 header/body/query는 bounded 400이다. | PASS |
| Parser hardening | Duplicate JSON member, ambiguous Content-Type, excessive nesting과 attacker-controlled extra key를 bounded 400으로 거부하고 secret/SQL-like key를 반사하지 않는다. | PASS |
| Six-operation HTTP contract | 모든 mutation이 actor, reason, expected state와 resume revision을 service에 정확히 전달하고 receipt/history 404/503이 private detail을 숨긴다. | PASS |
| N-1 migration | Version-1 authority fingerprint 보존, failed DDL/DML/ledger rollback, concurrent pending apply serialization과 같은 writer session의 rolling compatibility를 검증한다. | PASS |
| Least privilege | Writer가 receipt SELECT/INSERT와 identity USAGE만 갖고 table UPDATE/DELETE와 sequence SELECT/UPDATE는 갖지 않는다. PUBLIC/direct over-grant와 inherited parent role은 회수하고 non-owner delegated ACL, grant option 또는 writer ownership은 exact postcondition에서 fail-closed한다. | PASS |
| Post-commit reload | Local apply 실패 뒤에도 committed success receipt를 반환하고 reload component를 unavailable로 표시한다. | PASS |

## Commands And Results

```text
uv run pytest tests/test_source_admin.py -q
  37 passed
uv run pytest tests/test_http.py -q
  29 passed
uv run pytest -m 'not integration' tests/test_source_store.py -q
  51 passed
COMPOSE_PROJECT_NAME=query-man uv run pytest -m integration \
  tests/test_source_store.py -q
  3 passed
COMPOSE_PROJECT_NAME=query-man uv run pytest -m integration \
  tests/test_control_migrations.py -q
  11 passed
uv run ruff check .
  PASS
uv run mypy src
  PASS
uv run pytest
  386 passed, 44 deselected
COMPOSE_PROJECT_NAME=query-man uv run pytest -m integration
  32 passed, 398 deselected
COMPOSE_PROJECT_NAME=query-man ./scripts/control-plane-drill.sh
  PASS (7 tables, 4 FKs, 4 triggers, immutable history/receipts, writer ACL)
```

Integration은 UUID disposable Control DB와 invocation별 container migration staging path를
사용한다. 종료 뒤 scratch database/path를 삭제하고 development authority fingerprint가 전후
같은지 확인한다.

## Test-Derived Corrections And Maintainability

두 번째 migration에 identity sequence 권한이 필요하므로 table SELECT/INSERT와 별도로 writer의
sequence USAGE를 추가하고 PUBLIC sequence 권한을 제거했다. Receipt insertion failure injection은
receipt를 mutation 뒤 별도 commit하면 authority만 남을 수 있음을 검증해 success receipt를 각
state transaction 안에 고정했다. Verified contract와 metadata resume도 service precheck만 믿지
않고 source lock 뒤 current generation/state/revision을 다시 확인한다.

Source admin route registration과 operator-first manual parsing을 별도 module로 추출해
`build_app` C90을 55에서 37로 낮췄다. Mutation service의 operation별 staging/validation branch는
서로 다른 rollback 의미가 있어 이번에 generic command framework로 합치지 않았다. 새 operation이
추가되거나 공통 lifecycle 변경이 다시 반복되면 preflight/rejection/finalization만 작은
coordinator로 추출한다.

첫 전체 integration은 두 가지 회귀를 찾았다. 기존 onboarding acceptance가 새 mutation headers와
receipt response envelope를 아직 사용하지 않아 400이었으므로 여섯 operation과 같은 helper로
expected state를 receipt에서 연쇄 전달하게 고쳤다. 더 중요하게는 concurrent runner가 numbered
migration advisory lock을 통과한 뒤 `reconcile-security.sql`을 동시에 실행해 PostgreSQL role
catalog에서 `tuple concurrently updated`가 났다. Security reconciliation도 같은 database
advisory lock을 가진 single transaction으로 직렬화했고 deterministic overlap test와 전체
integration을 다시 통과했다.

Mixed-checkout barrier test는 old runner가 첫 ledger 검증을 통과한 뒤 newer checkout이 migration과
ACL을 commit하는 순서를 재현했다. Final security reconciliation과 같은 lock/transaction 안에서
전체 filename/checksum ledger를 다시 검증해 old checkout이 새 grant를 회수하기 전에 fail-closed한다.
`psql`의 rejection branch는 stdout을 버리는 운영 경로에서도 이유와 non-zero status가 남도록
`\warn`과 `ON_ERROR_STOP` SQL 오류를 사용한다.

독립 security review는 transient catalog 장애가 validation rejection으로 영구 고정되던 경로,
verified-query의 deterministic 오류 receipt 누락, cross-key HMAC 상관관계, duplicate JSON과 과대
validation response, transition/result와 audit operation의 결합 누락, stale writer over-grant를
찾았다. Dependency unavailable은 receipt 없이 재시도 가능하게 분리하고 나머지는 bounded parser,
key-bound envelope, operation/result binding과 revoke-then-grant reconciliation으로 보완했다. Cancel
path도 operator 확인 뒤 UUID를 검증하도록 같은 authorization-first 계약에 맞췄다.

보완 뒤 첫 전체 integration은 source reader session policy mismatch가 transient catalog 장애와 같이
503/no-receipt로 분류된 회귀를 찾았다. Connection/network/catalog availability는 receipt 없이 재시도
가능하게 유지하되 reader-policy mismatch와 metadata contract/quality 위반은 deterministic 400
rejection receipt로 분리했고, 단독 시나리오와 최종 전체 32개 integration을 통과했다.

## Deliberate Limits And Future Triggers

- Receipt storage는 terminal-only다. Lookup 404는 staging/in-flight일 수 있으므로 실패로 해석하지
  않고 bounded polling과 source-state reconciliation 뒤에만 같은 key로 단일 재시도한다. Receipt
  생성 전 같은 key가 여러 replica에 동시에 도착하면 catalog staging 또는 verified query가 중복될
  수 있으나 authority/receipt commit은 한 번이다. 실제 admin 중복 비용이 관측되면 Control DB
  transaction을 source I/O 동안 잡지 않는 만료형 distributed in-flight lease를 별도 설계한다.
- Post-commit local reload 실패는 durable success를 뒤집지 않는다. 현재 health degradation과
  periodic reload가 복구 경계이며 replica별 desired/applied 관측은 `CTRL-06`에서 추가한다.
- Migration은 additive라 old writer가 schema 적용 전후 동작하지만 old application은 receipt를
  만들지 않는다. Rolling release 중 admin mutation traffic은 새 replica로만 보내고 old admin
  replica를 drain한다.
- Migration advisory lock은 database-local이고 writer role/membership은 cluster-global이다. 현재는
  같은 cluster의 production migration, restore drill과 disposable migration job을 운영/CI에서
  직렬화한다. 여러 Control DB를 병렬 관리할 요구가 생기면 global role reconciliation 분리 또는
  고정 coordination database lock을 먼저 설계한다.
- Membership/ACL remediation은 이미 이전 parent role로 `SET ROLE`한 session을 퇴출하지 않는다.
  Admin mutation traffic을 닫고 모든 control-writer session/pool을 drain/recycle한 뒤 fresh
  connection의 effective privilege를 검증한다.
- Actor/reason은 감사 fact이지 다단계 RBAC나 approval workflow가 아니다. User/organization별 ACL,
  tier/quota, AI executor와 credential broker는 별도 요구/threat model 전에는 추가하지 않는다.
- Source/event 규모의 SLA 증거가 생기기 전에는 JSON catalog expression index, receipt retention
  partition과 cleanup daemon을 추가하지 않는다.
- Runtime writer는 authority table도 갱신하는 trusted application role이며 store가 operation과 실제
  transition result를 receipt에 결합한다. 현재 DB CHECK는 operation enum과 result object/크기까지
  방어하지만 operation별 JSON 의미 전체를 직접 SQL writer에 강제하지는 않는다. Non-application
  writer를 허용하거나 compromised-writer tamper resistance가 요구되면 이미 적용한 migration을
  고치지 말고 forward migration에서 exact JSON constraint를 추가하고 direct receipt INSERT를
  제한된 database function으로 회수한다.
