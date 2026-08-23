# ADR 0016: Centralized Source Management Plane

Status: Accepted

Date: 2026-08-23

## Context

Control DB는 hot-added source의 immutable generation, encrypted credential, metadata snapshot과
active pointer를 이미 저장한다. 그러나 운영자는 source 전체 목록, 담당자, 변경 이력, replica
적용 상태, 데이터 규모와 비용 신호를 한곳에서 조회할 수 없다. Bootstrap YAML, startup access
policy, process-local metric과 외부 DB 상태가 서로 다른 위치에 있어 개발자와 운영자가 분리되면
현재 상태를 재구성하기 어렵다.

Production source를 Git manifest와 Control DB에 동시에 기록하면 어느 쪽이 기준인지, 누가
reconcile하는지와 rollback이 어느 상태로 돌아가는지가 불명확해진다. 반대로 실제 업무 데이터,
secret, DB migration과 고빈도 metric을 모두 Control DB로 복사하면 책임 경계와 저장 비용이
커진다.

## Decision

Production managed source의 authoritative state는 Control DB로 통일한다. 최초 publish 이후의
canonical manifest generation, active/deactivated state, metadata revision과 verified contract는
Control DB만 기준으로 삼고 repository YAML로 write-back하거나 양방향 동기화하지 않는다.

`config/sources/*.yaml`과 filesystem verified contract는 local/CI bootstrap 또는 일회성 seed다.
`config/onboarding/*.yaml`은 deterministic fixture다. Managed production mode는 bootstrap 파일이
없어도 시작할 수 있어야 하며, seed를 import한 뒤에는 같은 source의 Control DB 상태가
재시작·rollback·deactivate에서도 우선한다. 이 전환이 구현되기 전의 현재 dual-origin 동작은
운영 제약으로 명시한다. Import는 필요한 filesystem verified contract도 Control DB로 이관하며,
managed lifecycle record가 생긴 source에는 이후 filesystem contract를 합치지 않는다.

Control Plane은 하나의 operator management surface를 제공한다. 관리자 HTTP API와 그 위의 UI,
CLI 또는 승인된 Skill은 다음 정보를 같은 source ID로 조회한다.

- 비밀이 제거된 source 정의, owner, environment, classification과 DB migration provenance
- Desired generation/state와 replica별 applied generation/revision, health와 freshness
- Change plan, requester/approver, reason, plan hash, outcome과 rollback history
- Effective budget, caller visibility와 quality level
- Relation 또는 source별 row/storage observation, 측정 방법, 시각, freshness와 증가량
- Gateway/DB-native usage rollup, provider allocation과 산정 신뢰도

실제 저장 위치는 책임에 따라 분리할 수 있다.

| Data | Authority |
|---|---|
| Source definition, lifecycle, approval and audit | Control DB |
| Curated view, reader role and grants | Source DB and its migration system |
| Reader plaintext secret and binding registry | External credential broker/secret system; never Control DB or Git |
| Encrypted reader credential | Control DB source generation |
| Management authentication credential | External authenticator/secret system |
| Management role and source-scope binding | Control DB |
| Query caller identity and tenant authentication | External authenticator or versioned deployment identity configuration |
| Query caller `allowed_sources`/`all_sources` grant after import | Control DB |
| Budget hard-limit template | Versioned Query Man platform configuration |
| High-frequency raw metrics and provider billing | Metrics/billing system |
| Unified sanitized projection | Control Plane management API |

Management identity is separate from query caller identity. The target roles are read-only viewer,
source operator, approver and platform administrator; DB owner and application developer do not gain
production source mutation authority. Production policy may require requester and approver separation.
The query MCP remains non-administrative.

Management authentication and authorization have different authorities. An external authenticator or
secret-backed identity provider owns credentials and produces an immutable issuer/subject in a dedicated
management audience; Control DB owns role and source-scope bindings. Query-audience credentials are
rejected by management endpoints. Initial setup accepts a deployment-provided one-time platform-admin
credential only in explicit bootstrap mode while the binding table is empty, records the bootstrap event,
and cannot overwrite bindings later. Normal role changes and break-glass recovery are Control DB audited
mutations. Tokens and authentication secrets are never stored in Control DB.

Bootstrap is a one-time state transition, not merely an empty-table check. One Control DB transaction and
lock verifies an unconsumed bootstrap marker, creates the first platform-admin binding, appends its audit
event and permanently marks bootstrap consumed. Deleting all bindings does not reset the marker; concurrent
or replayed bootstrap attempts fail. Normal mutation cannot remove the final active platform administrator.
Disaster recovery restores the marker and bindings together; separately authorized break-glass recovery is
audited and does not reopen bootstrap.

Query caller authentication continues to derive stable caller/tenant identity from its external
authenticator or versioned deployment identity configuration. After `CTRL-07`, Control DB is authoritative
for versioned `allowed_sources` and `all_sources` grants. The existing access-policy source scope is imported
once and ignored thereafter; restart never merges it back. Source lifecycle changes and caller grants remain
separately approved, and effective visibility intersects the current active source set with the active grant.
Only an explicit platform-admin migration owns import; runtime replicas never import on startup. It hashes the
complete canonical seed and atomically writes all grants, actor/digest audit and a permanent consumed marker
under one Control DB lock/transaction. Partial failure rolls back everything. A concurrent call may return the
recorded result for the exact digest, while a different digest or later re-import fails closed. Managed grant
mode also fails closed if the consumed marker is absent.

Configuration revisions and operational observations use separate lifecycles. Row count, storage,
growth and usage collection never creates a source generation or metadata revision. A measurement stores
its scope, value/unit, method, canonical definition revision, observed time, freshness and relevant
metadata revision. Default collection uses bounded catalog/provider estimates; it does not run
unrestricted `COUNT(*)`. Exact counts require an explicitly approved cheap counter or bounded relation.
Values measured with different methods or definition revisions are not compared as one growth series.
Cost allocation also records a versioned allocation method so changed allocation rules start a new series.
Observation availability distinguishes `not_configured`, `pending`, `available`, `stale` and `unavailable`.
It records the last attempt time and a bounded reason code when collection has not produced a usable value;
missing or failed observations are never rendered as zero.

The management API must support authoritative list/detail/history, non-publishing validation and plan,
explicit approval, idempotent apply and timeout reconciliation before an AI executor can publish. Every
mutation records actor, reason, expected and resulting state, canonical plan hash and bounded outcome
without credential, ad-hoc question, SQL text or database error detail.

Plaintext reader credentials enter validation/apply only through a server-owned credential broker. A
change request selects an allowlisted opaque binding, not a URL, environment variable or provider path.
An external secret administrator/DB owner, not a Query Man operator or AI executor, creates or rotates that
binding for exactly one source ID, resolved host/port/database/user/TLS identity and `query_reader` purpose.
The broker returns attested target/purpose and exact provider version transiently. The server rejects any
manifest mismatch and binds the binding ID, target identity, purpose and provider version into the plan
hash without the value. Apply re-resolves and rechecks every field, fails closed on drift, and stores only
the existing encrypted generation credential. No management response, audit or plan contains the plaintext
value or provider path.

Control DB migrations are versioned and production, development and integration-test stores are isolated.
Backup/restore, retention and encryption-key recovery cover every authoritative table. A management-plane
outage rejects new mutations; the data plane may retain its last verified applied state under the existing
availability contract.

Detailed rollout and active work are defined in
[source management plane](../source-management-plane.md) and `CTRL-*` in
[active development TODO](../development-todo.md).

## Consequences

- Operators can add and manage production sources without application-repository commits or deployment.
- Git remains the platform/schema/fixture authority, not a second production source catalog.
- A single management experience does not require copying business data, secrets or raw metrics into one
  physical database.
- Control DB availability, backup, audit integrity and access control become production-critical.
- Existing bootstrap-only startup, boolean operator authorization, mutation-only admin API, process-local
  metrics and shared fixture history are migration gaps, not completed management-plane capabilities.
- Provider currency cost is shown only when an external billing source and allocation method exist; row
  count or PostgreSQL planner cost alone is never presented as money.
