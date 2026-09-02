# Query Man Agent Router

이 지침은 repository 전체에 적용한다. 상세 개발·병렬 작업·테스트·handoff 규칙의 canonical
source는 [활성 개발 지침](docs/development-guidelines.md)이다. Primary module을 고른 뒤 변경 종류에
해당하는 절만 구현 전에 읽는다. 목표는 코드 골프가 아니라 요구사항을 안전하게 만족하는 가장
작고 단순한 변경이다.

구조를 나누는 목적은 사람과 agent가 필요한 범위만 빠르게 이해하고, owner가 보장하는 동작을 보존한
내부 변경의 영향이 다른 module로 불필요하게 번지지 않게 하는 것이다. 논리 module은 독립 배포나
임의 구현 교체를 약속하지 않는다. Folder, package, repository 또는 service 수를 늘리는 것 자체는
목표가 아니다. 구조 변경 전에는
[설계 목적과 분리 판단](docs/development-guidelines.md#설계-목적과-분리-판단)을 적용한다.

## 60초 작업 시작

1. [문서 안내](docs/README.md)와 [용어 사전](docs/glossary.md)에서 독자별 current 문서를 고른다.
2. [module index](docs/modules/README.md)에서 primary module 하나를 고른다.
3. 그 module README의 `30초 요약`과 `집중해서 읽을 범위`를 따라 owner package의 관련 leaf
   module·root `tests/`만 읽는다. Package `__init__.py`는 marker-only이므로 interface re-export를
   기대하지 않는다.
4. 변경 지점부터 직접 consumer, transaction·cleanup, 실패 경로와 runnable test까지 확인한다.
5. External/persisted/policy/lifecycle/ownership/procedure 의미를 바꿔야 하면
   구현을 멈추고 아래 승인 trigger와 [상세 절차](docs/development-guidelines.md#승인-규칙)를 따른다.

현재 launch authority는 [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md), source package
authority는 [ADR 0034](docs/decisions/0034-source-view-package-and-direct-admission.md), budget authority는
[ADR 0030](docs/decisions/0030-git-reviewed-yaml-source-authority.md)의 Git-reviewed YAML이다. 실제 active
작업은 [development TODO](docs/development-todo.md)에서 확인한다. 삭제한 과거 roadmap·verification은
[Git 기록 안내](docs/verification/README.md)의 archive commit에서만 읽고 현재 serving 범위로 해석하지 않는다.

병렬 작업의 coordinating agent는 시작 전에 primary module, 승인된 change-set ID와 baseline commit,
수정 가능한 file allowlist, 읽기 전용 provider/consumer file, 수정 금지 shared file, 필수 test와 중단
조건을 지정한다. 여러 agent가 같은 worktree를 공유하면 Git 작업은 coordinating agent만 수행한다.

## 승인 Trigger

`Module interface`는 allowed dependency map 안에서 provider가 다른 logical module에 보장하는 안정된
entrypoint와 호출 단위 input/output/domain-error semantics다. 문서는 중요한 동작과 entrypoint를
설명하며 모든 public Python symbol을 열거하지 않는다. External/persisted/policy/lifecycle/ownership
의미를 보존하는 내부 Python shape/signature 변경, additive helper와 consumer 동시 수정은 별도 사용자
승인 없이 같은 change set에서 구현·검증할 수 있다. 실제 교체 요구가 없는 구현에 Protocol이나 wrapper를
추가하지 않는다.

다음 의미의 변경도 정확한 영향 범주를 각각 제시하고 별도 승인받는다.

- `External API/wire format`
- `Persisted/versioned format`
- `Policy/compatibility identity`
- `Safety/lifecycle invariant`
- `Ownership/composition boundary`
- `Protected operational procedure`

Protected environment의 실제 action은 repository나 procedure 승인과 별개로 access, scope, target,
stop condition과 change-record 책임을 확인한 실행 승인이 필요하다. Protected evidence/change record는
승인된 환경 기록 시스템에 append-only/immutable 사실로 보존한다. Repository의 과거 서술 문서는
archive baseline을 기록한 뒤 current tree에서 정리할 수 있지만 Git history를 rewrite하지 않는다.
Shared transition artifact와 공통 governance 문서는 single-writer로 편집한다.

위 범주의 승인 필요성, authority 충돌 또는 분류 불명확성을 발견하면 code/schema/config와 의미 문서 수정을
멈춘다. 현재 의미, 제안 의미와 이유, provider/consumer 또는 external/persisted/operational 영향,
compatibility/migration/rollback, 보안·데이터 손실 영향과 검증 계획을 사용자에게 제시한다. 일반적인
“구현”, “refactor”, “정리” 요청이나 다른 agent의 동의는 위 범주 변경의 승인이 아니다. 전체 분류와 절차는
[활성 개발 지침](docs/development-guidelines.md#module-interface와-승인-대상-변경)을 따른다.

## Non-Negotiable Safety

다음 항목은 코드 감소를 이유로 생략하거나 약화하지 않는다.

- 외부 입력과 SQL AST validation
- Source, schema, relation, function과 operator allowlist
- 최소 권한 reader, read-only transaction, timeout, concurrency와 row/byte limit
- Query cancel, rollback과 client disconnect 처리
- Credential, token, SQL literal과 내부 database 오류 비공개
- Metadata/SQL revision 일치, 검출 가능한 schema/revision drift의 fail-closed와 tenant/source authorization
- 데이터 손실을 막는 오류 처리와 복구 절차

안전 정책을 prompt나 호출자 관례에 맡기지 않고 gateway와 PostgreSQL이 강제하게 한다. 더 짧은
구현과 더 안전한 구현이 충돌하면 안전한 구현을 선택한다.
Revision이 포착하지 못하는 privileged DDL/function/operator/collation/semantic-setting drift는 현재
runtime attestation 범위 밖이므로 ADR 0025의 승인 inventory와 serving freeze를 필수 완화로 유지한다.
