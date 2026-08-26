# 일정에 없는 후속 작업

Status: Parked — 조사 기록이며 현재 구현 일정이나 변경 승인이 아님

이 문서는 나중에 필요할 수 있는 주제를 잊지 않기 위해 보존합니다. 지금 할 일은
[Active TODO](development-todo.md)의 `LAUNCH-02` 하나뿐입니다.

아래 ID와 설계 문서가 있다는 사실은 구현 시작 승인이 아닙니다. 실제 요구, 우선순위와 정확한
변경 범위를 다시 승인받은 뒤 새 baseline을 정해야 합니다.

## RLS source 제공

현재 상태: 모든 RLS source를 등록·metadata·queue·DB 접근 전에 거부합니다.

시작 조건: 실제 RLS source 제공 요구와 지원할 relation/policy 범위, attestation, migration,
cutover·rollback의 정확한 승인이 모두 필요합니다.

- `RLS-01`: [ADR 0024](decisions/0024-rls-policy-drift-attestation.md)의 연구를 현재 launch v3 기준에
  다시 맞추고 지원 범위를 결정합니다.
- `RLS-02`: 승인된 attestation, snapshot/codec, query-time admission과 provider/consumer test를
  구현합니다.
- `RLS-03`: Protected source inventory, migration, rollback과 cross-tenant acceptance를 별도 실행
  승인 아래 수행합니다.

작은 예외로 RLS 차단만 우회하지 않습니다.

## 결과 type 확대

현재 상태: PostgreSQL 결과 OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용합니다.

시작 조건: 이 일곱 type으로 표현할 수 없는 실제 질문과 필요한 PostgreSQL type corpus가 있어야
합니다. 성공 범위를 넓힐 때 SQL policy v3를 덮어쓰지 않고 v4 이상으로 전환합니다.

- `ENC-01`: [ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 연구에서 실제로
  필요한 type만 다시 고르고 lossless value/OID/reader policy를 제안합니다.
- `ENC-02`: 승인된 loader, encoder, revision, verified migration과 rollback을 구현·검증합니다.

## Managed canonical-time cutover

- `TIME-03`: 실제 managed environment가 canonical-time R2 이후 정책으로 전환될 때 protected
  inventory, drain, current/rollback verified membership과 route evidence를 별도 실행 승인 아래
  남깁니다. Static first launch의 선행 조건은 아닙니다.

## DB-native 비용과 사용량 경보

현재 상태: Timeout, concurrency, row/byte와 resource limit는 이미 강제합니다. Gateway usage
lower-bound projection은 있지만 provider 금액은 `not_configured`입니다.

시작 조건: PostgreSQL statement aggregate가 실제 운영 의사결정에 필요하고 monitoring 권한과
retention 범위를 다시 승인해야 합니다.

- `COST-01`: [ADR 0021](decisions/0021-database-native-cost-attribution.md)의 monitoring identity와
  최소 권한을 다시 결정합니다.
- `COST-02`: 승인된 bounded collector와 failure isolation을 구현합니다.
- `COST-03`: Aggregate persistence와 operator projection을 구현합니다.
- `COST-04`: Base evidence가 생긴 뒤에만
  [ADR 0023](decisions/0023-database-native-usage-spike-alert.md)의 threshold, event와 retention을
  별도 결정합니다.
- `COST-05`: Protected fixture에서 reset, failover, explicit zero와 rollback을 검증합니다.

## Workflow trace

현재 상태: Server가 만든 request, MCP call과 query ID를 사용합니다.

시작 조건: 이 ID들로 해결되지 않는 실제 end-to-end correlation 요구와 header trust boundary 승인이
있어야 합니다.

- `TRACE-01`: [ADR 0022](decisions/0022-w3c-workflow-trace-context.md)의 context 선택을 다시 결정합니다.
- `TRACE-02`: 승인된 HTTP/MCP context와 Runtime scope를 구현합니다.
- `TRACE-03`: Redaction과 metric-cardinality 경계를 검증합니다.
- `TRACE-04`: Parallel, retry, disconnect와 multi-replica acceptance를 실행합니다.

## 계속 하지 않을 일

- Prompt, Skill 또는 caller 관례로 authorization, SQL validation, reader privilege나 resource limit을
  대신하지 않습니다.
- 실제 요구 없이 query별 통화 비용, user별 chargeback, distributed global quota와 다단계
  management RBAC를 미리 만들지 않습니다.
- Parked 연구를 현재 module interface나 launch baseline으로 문서화하지 않습니다.
