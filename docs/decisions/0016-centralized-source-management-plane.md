# ADR 0016: Centralized Source Management Plane

Status: Accepted

Date: 2026-08-23

## Context

Control DB는 hot-added source의 immutable generation, encrypted credential, metadata snapshot과
active pointer를 이미 저장한다. 그러나 운영자는 source 전체 목록, 담당자, 변경 이력, replica
적용 상태, 데이터 규모와 비용 신호를 한곳에서 조회할 수 없다. Bootstrap YAML, process-local
metric과 외부 DB 상태가 흩어져 있어 개발자와 운영자가 분리되면 현재 상태를 재구성하기 어렵다.

Production source를 Git manifest와 Control DB에 동시에 기록하면 기준과 rollback 의미가
불명확해진다. 반대로 실제 업무 데이터, secret과 고빈도 raw metric까지 Control DB로 복사하면
책임과 저장 비용이 커진다.

초기 운영자는 한 종류의 admin이고, query 사용자는 모두 같은 active source 목록과 source별
resource tier를 사용한다. 아직 필요하지 않은 다단계 RBAC, caller grant, 사용자별 quota와
chargeback을 미리 설계하지 않는다.

## Decision

Runtime은 `QUERY_MAN_SOURCE_MODE=bootstrap|managed`로 source authority를 process startup에
한 번만 선택한다. 기본값은 local/CI용 `bootstrap`이며 production은 `managed`를 명시한다. 두
mode를 source별로 섞거나 Control DB 설정 유무로 자동 선택하지 않는다.

- `bootstrap`은 local/CI 전용이다. `config/sources/*.yaml`과 filesystem verified contract만 읽고
  Control DSN과 source encryption key를 모두 거부한다.
- `managed`는 production authority다. Control DSN과 source encryption key를 모두 요구하고 빈
  registry/verified map에서 Control DB lifecycle과 contract만 load한다. Source directory와
  filesystem verified contract가 없거나 같은 `source_id`를 담아도 열거나 합치지 않는다.

Managed source의 canonical manifest generation, active/deactivated state, metadata revision과
verified contract는 Control DB만 기준으로 삼는다. Lifecycle row가 없는 source는 managed mode에서
absent이며 file로 fallback하지 않는다. Repository YAML로 write-back하거나 양방향 동기화하지
않는다. `config/onboarding/*.yaml`은 deterministic fixture다.

Control Plane은 admin-only management surface 하나를 제공한다. 관리자 HTTP API와 그 위의 UI,
CLI는 같은 `source_id`로 다음 정보를 조회한다.

- 비밀이 제거된 source 정의, owner, environment와 DB migration provenance
- Desired generation/state와 replica별 applied generation/revision, health와 freshness
- Source가 선택한 effective `budget_profile`과 관련 metadata revision
- Actor, reason, request hash, expected/resulting state, outcome과 rollback history
- Representative record volume, storage, growth와 측정 방법/freshness
- Source/profile별 gateway 및 DB-native usage와 비용 projection 상태

실제 저장 위치는 책임에 따라 분리할 수 있다.

| Data | Authority |
|---|---|
| Source definition, lifecycle and mutation audit | Control DB |
| Curated view, reader role and grants | Source DB and its migration system |
| Encrypted reader credential | Control DB source generation |
| Plaintext reader credential and master key | Runtime/external secret system; never Git, response or audit |
| Admin and query authentication credential | External authenticator or versioned deployment configuration |
| Shared query access and source resource-tier rule | ADR 0017 and versioned platform configuration |
| Budget hard-limit template | Versioned Query Man platform configuration |
| High-frequency raw metrics and provider billing | Metrics/billing system |
| Unified sanitized projection | Control Plane management API |

Management은 단일 admin capability로 제한한다. Query credential은 admin API와 cancel에서
거부되고 query MCP에는 관리 tool을 추가하지 않는다. Viewer, source operator, approver,
platform administrator 역할 계층이나 Control DB role/source-scope binding은 현재 만들지 않는다.
관리자 mutation은 인증된 actor와 reason을 append-only audit에 남긴다. 조직에서 2인 승인이
필요해지는 시점에 별도 ADR로 승인 workflow를 추가한다.

Query access와 resource tier는
[ADR 0017](0017-shared-source-access-and-resource-tier.md)을 따른다. 모든 인증된 query
principal은 같은 active source 목록을 보고 한 source의 같은 `budget_profile` 정의를
공유한다. 따라서 Control DB caller-grant table, grant seed import/marker, user/organization tier
binding과 별도 `cost_tier`를 만들지 않는다. Stable caller/tenant identity와 source-native RLS는
audit와 row isolation을 위해 유지할 수 있지만 source visibility나 tier 선택에는 쓰지 않는다.

Configuration revision과 operational observation은 lifecycle을 분리한다. Row count, storage,
growth와 usage 수집은 source generation이나 metadata revision을 만들지 않는다. 측정값은 scope,
value/unit, method, definition revision, observed time와 freshness를 가진다. 기본 수집은 bounded
catalog/provider estimate를 사용하고 일반 view에 unrestricted `COUNT(*)` 또는
`EXPLAIN ANALYZE`를 실행하지 않는다. 다른 method나 definition revision의 값은 같은 growth
series로 비교하지 않는다.

Observation availability는 `not_configured`, `pending`, `available`, `stale`, `unavailable`을
구분한다. Last attempt와 bounded reason을 기록하며 missing/failed 값을 0으로 표시하지 않는다.
Provider billing이 없으면 통화 비용 대신 source/profile별 resource usage와 추세만 제공한다.
User/organization별 allocation과 chargeback은 현재 범위가 아니다.

기존 admin mutation endpoint를 재사용한다. 별도 change-request/approval table과 endpoint 대신
idempotency key, canonical request hash, expected generation/state, authoritative mutation receipt와
append-only lifecycle audit를 추가한다. Timeout 뒤에는 receipt/state를 조회해 reconcile하고
blind retry하지 않는다. 기존 staged validation, immutable generation, atomic pointer, rollback
검증과 credential redaction/encryption을 유지한다.

현재 plaintext credential을 받는 direct admin API는 trusted manual-admin boundary다. Plan-only
onboarding Skill은 이 API를 호출하거나 credential을 읽지 않는다. AI 또는 다른 자동화에
production mutation 권한을 주기로 결정할 때만 target-bound credential broker, plan-ID apply와
별도 threat model을 선행한다.

기존 bootstrap source의 일회성 이관은 startup importer가 아니라 traffic 밖의 managed instance와
기존 admin API를 사용한다. Source를 L0/L1로 staged publish하고 현재 revision의 reviewed verified
contract를 Control DB에 publish한 뒤 L2로 승격한다. Coverage 확인 뒤 serving replica를 managed
mode로 재시작한다. Import marker, seed digest, bulk import endpoint와 filesystem write-back은
만들지 않는다.

Control DB migration은 versioned이며 production, development와 disposable integration-test
store를 격리한다. Backup/restore, retention과 encryption-key recovery는 모든 authoritative
table을 포함한다. Management-plane outage는 새 mutation을 거부하고 data plane은 기존
availability contract에 따라 마지막 verified state를 유지할 수 있다. Cold-start scan이 실패하면
managed registry는 비어 있고 readiness는 unavailable이며 bootstrap file로 복구하지 않는다.
현재 immutable generation/pointer, metadata snapshot과 verified contract table이 이 authority를
이미 표현하므로 mode/import를 위한 schema migration이나 marker를 추가하지 않는다.

상세 rollout은 [source management plane](../source-management-plane.md), 실행 순서는
[active development TODO](../development-todo.md)의 `CTRL-*`가 관리한다.

## Consequences

- 운영자는 Git checkout 없이 모든 managed source의 상태, 이력, resource tier, 규모와 비용
  freshness를 한곳에서 확인한다.
- Git은 platform schema와 fixture authority이며 production source catalog가 아니다.
- 단일 관리 화면을 위해 business data, plaintext secret 또는 raw metric을 Control DB에
  복제하지 않는다.
- Source 추가는 모든 query 사용자에게 동시에 공개되고 별도 grant 변경이 없다.
- 관리자 한 종류와 기존 `budget_profile`만 사용하므로 초기 schema와 API가 작다.
- Control DB availability, backup, audit integrity와 admin credential 분리는 production-critical
  boundary가 된다.
- 암시적 admin compatibility path, mutation-only admin API, process-local metric과 shared fixture
  history는 구현해야 할 gap이다.
- Future per-user/org ACL, quota, tier override, multi-role approval, automated credential broker와
  chargeback은 실제 요구와 threat model이 생길 때 별도 결정으로 추가한다.
