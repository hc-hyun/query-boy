# Query Man Agent Router

이 지침은 repository 전체에 적용한다. 상세 개발·병렬 작업·테스트·handoff 규칙의 canonical
source는 [활성 개발 지침](docs/development-guidelines.md)이다. Primary module을 고른 뒤 변경 종류에
해당하는 절만 구현 전에 읽는다. 목표는 코드 골프가 아니라 요구사항을 안전하게 만족하는 가장
작고 단순한 변경이다.

## 60초 작업 시작

1. [문서 안내](docs/README.md)와 [용어 사전](docs/glossary.md)에서 현재/기록/비활성 문서를 구분한다.
2. [module index](docs/modules/README.md)에서 primary module 하나를 고른다.
3. 그 module README의 `30초 요약`과 `집중해서 읽을 범위`를 따라 관련 code·test만 읽는다.
4. 변경 지점부터 직접 consumer, transaction·cleanup, 실패 경로와 runnable test까지 확인한다.
5. 다른 module이 쓰는 interface나 external/persisted/policy/lifecycle/procedure 의미를 바꿔야 하면
   구현을 멈추고 아래 승인 trigger와 [상세 절차](docs/development-guidelines.md#승인-규칙)를 따른다.

현재 launch authority는 [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md)이고 실제 active
작업은 [development TODO](docs/development-todo.md)에서 확인한다. 과거 roadmap이나 verification의
`Complete`를 현재 serving 범위로 자동 해석하지 않는다.

병렬 작업의 coordinating agent는 시작 전에 primary module, 승인된 change-set ID와 baseline commit,
수정 가능한 file allowlist, 읽기 전용 provider/consumer file, 수정 금지 shared file, 필수 test와 중단
조건을 지정한다. 여러 agent가 같은 worktree를 공유하면 Git 작업은 coordinating agent만 수행한다.

## 승인 Trigger

`Module interface`는 provider가 allowed dependency map과 자기 module 문서에서 다른 logical module이
쓰도록 명시적으로 공개한 Python symbol과 lifecycle capability다. 그 의미는 Python shape/signature와
호출 단위 input/output/domain-error semantics로 한정한다.
Module interface의 의미 변경은 additive change를 포함해 사용자의 명시적 승인 없이 진행하지 않는다.

다음 의미의 변경도 정확한 영향 범주를 각각 제시하고 별도 승인받는다.

- `External API/wire format`
- `Persisted/versioned format`
- `Policy/compatibility identity`
- `Safety/lifecycle invariant`
- `Ownership/composition boundary`
- `Protected operational procedure`

Protected environment의 실제 action은 repository나 procedure 승인과 별개로 access, scope, target,
stop condition과 change-record 책임을 확인한 실행 승인이 필요하다. Evidence/change record는
append-only/immutable 사실로 보존한다. Shared transition artifact와 공통 governance 문서는
single-writer로 편집한다.

승인 필요성, authority 충돌 또는 분류 불명확성을 발견하면 code/schema/config와 의미 문서 수정을
멈춘다. 현재 의미, 제안 의미와 이유, provider/consumer 또는 external/persisted/operational 영향,
compatibility/migration/rollback, 보안·데이터 손실 영향과 검증 계획을 사용자에게 제시한다. 일반적인
“구현”, “refactor”, “정리” 요청이나 다른 agent의 동의는 승인이 아니다. 전체 분류와 절차는
[활성 개발 지침](docs/development-guidelines.md#module-interface와-승인-대상-변경)을 따른다.

## Non-Negotiable Safety

다음 항목은 코드 감소를 이유로 생략하거나 약화하지 않는다.

- 외부 입력과 SQL AST validation
- Source, schema, relation, function과 operator allowlist
- 최소 권한 reader, read-only transaction, timeout, concurrency와 row/byte limit
- Query cancel, rollback과 client disconnect 처리
- Credential, token, SQL literal과 내부 database 오류 비공개
- Schema revision 일치, drift fail-closed와 tenant/source authorization
- 데이터 손실을 막는 오류 처리와 복구 절차

안전 정책을 prompt나 호출자 관례에 맡기지 않고 gateway와 PostgreSQL이 강제하게 한다. 더 짧은
구현과 더 안전한 구현이 충돌하면 안전한 구현을 선택한다.
