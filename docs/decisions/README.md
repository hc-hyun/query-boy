# Architecture Decision Records

ADR은 중요한 설계 선택과 그 이유를 보존하는 기록입니다. 모든 ADR을 순서대로 읽을 필요는
없습니다. 현재 첫 오픈 범위는 [ADR 0025](0025-static-non-rls-first-launch.md)가 가장 좁고 우선하는
기준입니다.

## 현재 기준을 찾는 순서

1. [ADR 0025](0025-static-non-rls-first-launch.md)에서 현재 first-launch 범위를 확인합니다.
2. 현재 Python package와 import 위치는 [ADR 0026](0026-physical-module-packages.md)을 확인합니다.
3. 작업 모듈의 README가 지정한 accepted ADR만 추가로 읽습니다.
4. 이전 ADR과 후속 ADR이 겹치면 후속 문서의 supersede·exception note를 따릅니다.
5. `research`, `parked`, `deferred` 문서는 구현 승인이 아닙니다.

## Accepted decisions

| ADR | 주제 | 현재 읽을 때 주의할 점 |
|---|---|---|
| [0001](0001-postgresql-ast-validation.md) | PostgreSQL AST 검증 | SQL parser와 fingerprint 기준 |
| [0002](0002-guarded-query-contract.md) | Guarded query 외부 동작과 안전 | 결과 성공 범위는 ADR 0025의 일곱 OID가 더 좁게 제한 |
| [0003](0003-reader-and-resolved-object-policy.md) | Reader와 DB-resolved object 정책 | RLS launch 경로는 ADR 0025가 격리 |
| [0004](0004-caller-source-authorization.md) | Caller와 source authorization | Source별 scope는 ADR 0017이 대체; 현재 RLS는 격리 |
| [0005](0005-initial-query-budgets.md) | 초기 query 제한 | 현재 `interactive` budget 기준 |
| [0006](0006-mcp-transport-and-workflow.md) | MCP transport와 Text-to-SQL 흐름 | HTTP와 같은 application service 사용 |
| [0007](0007-immutable-metadata-publishing.md) | Metadata publish와 rollback | Revision lifecycle 기준 |
| [0008](0008-physical-key-and-index-disclosure.md) | Key/index 공개 범위 | Metadata disclosure 기준 |
| [0009](0009-question-scoped-column-disclosure.md) | 질문별 column 공개 | Wide relation context 제한 |
| [0010](0010-revision-scoped-retrieval-index.md) | Revision별 metadata 검색 index | Context relation/column 선택 기준 |
| [0011](0011-metadata-quality-level-publish-gate.md) | L0/L1/L2 publish gate | Source 품질 단계 기준 |
| [0012](0012-control-plane-source-revisions.md) | Control Plane source generation | Managed mode persisted/security 기준 |
| [0013](0013-control-plane-verified-query-publishing.md) | Managed verified query publish | 현재 static launch에서는 비활성 capability |
| [0014](0014-trusted-rls-tenant-context.md) | RLS tenant context | 구현 이력은 보존하지만 현재 모든 RLS source 격리 |
| [0015](0015-containerized-local-runtime.md) | Local container runtime | Compose, readiness와 secret 경계 |
| [0016](0016-centralized-source-management-plane.md) | 중앙 source 관리 | 구현은 보존하지만 static first launch에서는 비활성 |
| [0017](0017-shared-source-access-and-resource-tier.md) | 공용 source visibility와 budget | 현재 query/admin capability 기준 |
| [0018](0018-module-ownership-and-contract-governance.md) | Module ownership과 변경 승인 | 현재 모듈 개발 지침 |
| [0019](0019-canonical-time-stability.md) | 시간 표현 안정성 | Repository 구현 완료; 환경별 cutover는 별도 작업 |
| [0025](0025-static-non-rls-first-launch.md) | Static non-RLS first launch | 현재 launch authority; protected 실행은 `LAUNCH-02` |
| [0026](0026-physical-module-packages.md) | Owner별 physical Python package | 같은 repository/wheel/process의 path와 composition 경계; serving 의미는 ADR 0025 유지 |
| [0027](0027-consent-gated-diagnostic-capture.md) | 동의 기반 진단 원문 수집 | 일반 log와 분리된 최대 7일 암호화 capture, 가명 subject와 fail-open 경계 |
| [0028](0028-interactive-operator-shell.md) | 대화형 운영 Shell | `qm`의 자동완성·입력 안내와 bounded status/log/diag/source 운영 경계 |

## 보류된 연구

아래 문서는 문제와 선택지를 보존하지만 현재 일정이나 구현 승인이 아닙니다.

| ADR | 상태 | 다시 시작하는 조건 |
|---|---|---|
| [0020](0020-lossless-interval-and-json-numeric-encoding.md) | Superseded research | 현재 일곱 결과 type으로 답할 수 없는 실제 질문과 새 승인 |
| [0021](0021-database-native-cost-attribution.md) | Parked research | DB-native 비용 귀속의 실제 운영 요구와 권한·보존 범위 승인 |
| [0022](0022-w3c-workflow-trace-context.md) | Parked research | 현재 request/query ID로 부족한 실제 workflow 추적 요구 |
| [0023](0023-database-native-usage-spike-alert.md) | Parked research | COST base evidence와 alert 의미·threshold 별도 승인 |
| [0024](0024-rls-policy-drift-attestation.md) | Deferred research | 실제 RLS source 제공 요구와 새 attestation·migration·cutover 승인 |

과거 ADR의 문장을 현재 표현에 맞춰 조용히 고치지 않습니다. 실제 의미가 달라지면 새 ADR이나
명시적인 supersede note를 남기고, 단순 링크·오기·현재 authority 설명은 사실 정정으로 표시합니다.
