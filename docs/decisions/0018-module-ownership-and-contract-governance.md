# ADR 0018: Module Ownership And Boundary Governance

> [ADR 0025](0025-static-non-rls-first-launch.md) launch exception: 아래 no-deploy source 확장 결과는 managed mode를 별도로 활성화한
> 경우의 capability다. 현재 static first launch의 새 source/database는 inventory review와 재배포가
> 필요하다. Module interface와 승인 분류 결정은 그대로 유지된다.

Status: Accepted

Date: 2026-08-23

## Context

Query Man은 Source 등록부터 metadata, guarded query, HTTP/MCP와 process lifecycle까지 한
repository와 process에 구현되어 있다. 현재 Python 파일은 평면 구조이고 일부 파일은 여러 책임을
함께 가진다. 이 상태에서 agent마다 repository 전체를 파악하게 하거나 파일 이름만으로 작업을
나누면 서로 다른 가정으로 같은 경계를 바꾸기 쉽고, 병렬 변경이 보안·transaction·wire 의미를
깨뜨릴 수 있다.

반대로 즉시 package를 대규모로 이동하면 현재 동작과 구조 변경이 섞이고, 아직 한 번만 쓰는
interface와 forwarding layer를 미리 만들 위험이 있다. 필요한 것은 배포 분리가 아니라 현재
동작을 기준으로 한 명확한 owner, 제공·소비 module interface와 변경 승인 절차다.

## Decision

- Query Man은 하나의 deployable process인 modular monolith로 유지한다.
- 개발 소유권은 Source Catalog, Metadata, Guarded Query, Control Plane, Delivery, Runtime과
  Assurance의 일곱 논리 module로 나눈다.
- [`docs/modules/README.md`](../modules/README.md)를 module owner, 허용 dependency, 현재 flat-file
  transition map과 focused-reading 절차의 canonical index로 사용한다.
- 각 module은 자기 directory의 `README.md`에서 소유/비소유 책임, 현재 code 위치, 제공/소비
  module interface, 별도 external/persisted/policy 경계, safety/lifecycle invariant, 독립 변경 범위,
  사용자 승인 trigger와 focused tests를 정의한다.
- Agent는 repository 전체를 선행 학습하지 않는다. Primary module 문서와 변경되는 완전한
  end-to-end slice, 직접 소비 interface 및 관련 ADR/test를 읽는다. Trust boundary가 다른 module로
  넘어가면 그 interface와 별도 경계에 필요한 범위만 확장한다.
- 다른 module의 private implementation이나 Control DB table에 의존하지 않는다. Runtime은
  production server를, Control Plane은 candidate source의 격리 staging을, Assurance CLI
  entrypoint는 offline 검증을 위해 필요한 concrete implementation만 조립한다.
- Provider가 allowed dependency map과 자기 module 문서에서 다른 module용으로 명시적으로 공개한
  Python constant/enum/type/DTO/domain error/Protocol/function/method/use case와 lifecycle capability만
  official module interface로 본다. 그 의미는 Python shape/signature와 호출 단위
  input/output/domain-error semantics로 한정하며 additive 변경도 포함한다.
- 다음은 module interface가 아닌 별도 범주다: `External API/wire format`,
  `Persisted/versioned format`, `Policy/compatibility identity`, `Safety/lifecycle invariant`,
  `Ownership/composition boundary`, `Protected operational procedure`,
  `Operational execution authorization`, `Evidence/change record`, `Shared transition artifact`.
  Policy, ordering, limit와 lifecycle outcome도 interface 의미에 포함하지 않는다. 의미 변경은 각
  범주의 정확한 영향과 compatibility/migration/rollback, 안전 영향 및 검증 계획을 사용자가
  명시적으로 승인한 뒤 진행한다. 범주가 다르다는 이유로 보호를 약화하거나, 같은 기능이라는 이유로
  하나의 거대한 포괄 범주로 묶지 않는다.
- Protected environment의 실제 action은 procedure나 repository 변경 승인과 별도로 access, scope,
  target, stop condition과 change-record 책임을 확인한 실행 승인이 필요하다. 실행 evidence는
  append-only/immutable 사실 기록으로 보존하고 정정은 원문과 provenance를 남긴 새 기록으로 추가한다.
- 일반적인 구현이나 refactoring 요청과 다른 agent의 동의는 interface 또는 별도 경계 변경 승인이 아니다.
- 승인된 change set은 하나의 coordinating workstream에서 먼저 baseline을 확정한다. 그 change set의
  interface, format, policy, invariant, ownership 또는 procedure 의미를 사용하는 병렬 작업은 baseline이
  확정될 때까지 동결하고 이후 독립 implementation을 병렬화한다.
- Interface와 별도 승인 대상 의미를 보존하는 서로 다른 module 내부 변경은 독립적으로 병렬 수행할 수 있다.
- 현재 shared file은 transition map의 모든 owner가 공동 영향 범위다. 물리 package 이동은 public
  의미를 보존하고 runnable tests로 검증하는 별도 mechanical refactoring으로 수행한다.

### 2026-08-24 Approved Boundary-Hardening Follow-up

사용자는 [module boundary decision guide](../module-boundary-decision-guide.md)의 `D0-A`, `D1-A`,
`D2-A`, `D3-A`, `D4-A`, `D5-A`와 “모든 권장 선택에 공통으로 유지할 불변조건”을 승인했다.
구현은 `D0 -> D1 -> D2 -> D4 -> D3 -> D5` 순서로 직렬화하고 각 승인 change-set baseline이
확정된 뒤에만 서로 다른 독립 구현을 병렬화한다. `D0-A`/`RTSAFE-01`, `D1-A`/`MOD-04`,
`D2-A`/`MOD-05`, `D4-A`/`MOD-06`, `D3-A`/`MOD-07`과 `D5-A`/`MOD-08`은 구현·검증을 완료해
[completion ledger](../implementation-roadmap.md#14-post-baseline-completion-ledger-and-active-development)로
이동했다. 여섯 boundary 강화는 모두 현재 구현 baseline이며 승인 범위를 넘어서는 interface나 별도
경계 의미 변경은 다시 승인받는다.

## Consequences

- Agent는 담당 module을 중심으로 읽고 작업하면서도 직접 consumer와 안전 경계를 놓치지 않는다.
- 병렬 작업은 합의된 change-set baseline을 기준으로 분리되고 의미 변경 race를 피한다.
- Managed mode를 별도로 활성화한 환경에서 새 PostgreSQL source를 기존
  manifest/budget/reader/query interface와 policy 안에 추가하는 일은 data onboarding이며 module
  interface나 external API 변경 또는 배포를 요구하지 않는다. ADR 0025 static launch의 새 source는
  별도 inventory review와 재배포가 필요하다.
- Module 문서와 transition map을 code 변경과 함께 유지해야 하는 문서 비용이 생긴다.
- `app.py`, `models.py`, `metadata_store.py` 같은 shared file은 물리 분리 전까지 coordination이
  필요하다.
- Module 문서는 Non-Negotiable Safety, accepted ADR, 실제 module interface, external API/wire,
  persisted/versioned format, policy/compatibility identity, safety/lifecycle invariant, protected
  operational procedure, runnable test와 evidence/change record를 임의로 덮어쓰지 않는다. 충돌이
  발견되면 구현을 멈추고 사용자 결정을 받는다.

## Alternatives Rejected

- 즉시 microservice로 분리: 배포·network·운영 경계를 추가하지만 현재 목표인 개발 독립성에
  필요하지 않다.
- 즉시 package 전체 이동: 동작 검증과 구조 변경을 섞고 불필요한 abstraction을 만들 가능성이
  크다.
- 기술 layer만 기준으로 분리: source/metadata/query/control의 state와 안전 경계 owner가 흐려진다.
- 문서 없이 agent별로 범위를 판단: 같은 shared file과 interface/policy를 서로 다른 전제로 병렬 수정할
  위험을 통제하지 못한다.
