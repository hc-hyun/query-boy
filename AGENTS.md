# Query Man Agent Router

이 지침은 repository 전체에 적용한다. 상세 개발·병렬 작업·테스트·handoff 규칙의 canonical source는
[활성 개발 지침](docs/development-guidelines.md)이다. 목표는 코드 골프가 아니라 요구사항을 안전하게
만족하는 가장 작고 단순한 변경이다.

## 60초 작업 시작

1. [문서 안내](docs/README.md)에서 작업 목적에 맞는 current 문서를 고른다.
2. [Module index](docs/modules/README.md)에서 primary module 하나를 고른다.
3. Module README의 `30초 요약`과 `집중해서 읽을 범위`를 따라 관련 owner leaf, 직접 consumer와 root
   `tests/`만 읽는다. Package `__init__.py`는 marker-only이며 interface re-export를 제공하지 않는다.
4. 변경 지점부터 transaction·cleanup, 실패 경로와 runnable test까지 확인한다.
5. External/persisted/policy/lifecycle/ownership/procedure 의미를 바꿔야 하면 구현을 멈추고
   [승인 규칙](docs/development-guidelines.md#승인-규칙)을 따른다.

Current authority는 [현재 결정 index](docs/decisions/README.md), 실제 남은 작업은
[Active TODO](docs/development-todo.md), 삭제한 과거 문서는
[검증과 Git 기록](docs/verification/README.md)에서 찾는다. 과거 문서를 현재 serving 범위로 해석하지
않는다.

구조 변경은 [설계 목적과 분리 판단](docs/development-guidelines.md#설계-목적과-분리-판단)을 적용한다.
논리 module은 독립 배포나 임의 구현 교체를 약속하지 않으며 folder·package·service 수를 늘리는 것
자체가 목표가 아니다. 병렬 작업은
[Module-Scoped Development](docs/development-guidelines.md#module-scoped-development)의 allowlist,
baseline, single-writer와 Git ownership 규칙을 먼저 고정한다.

## 승인 Trigger

External/persisted/policy/lifecycle/ownership 의미를 보존하는 내부 Python shape/signature 변경, additive
helper와 consumer 동시 수정은 일반 구현 변경이다. 실제 교체 요구가 없는 구현에 Protocol이나 wrapper를
추가하지 않는다.

다음 의미를 바꾸면 영향 범주를 제시하고 별도 승인받는다.

- `External API/wire format`
- `Persisted/versioned format`
- `Policy/compatibility identity`
- `Safety/lifecycle invariant`
- `Ownership/composition boundary`
- `Protected operational procedure`

Protected environment의 실제 action은 repository나 procedure 승인과 별개로 access, scope, target, stop
condition과 change-record 책임을 확인한 실행 승인이 필요하다. 승인 필요성이나 authority가 불명확하면
code/schema/config와 의미 문서 수정을 멈추고 현재·제안 의미, 영향, compatibility/migration/rollback,
보안·데이터 손실과 검증 계획을 보고한다. 일반적인 “구현”, “refactor”, “정리” 요청은 위 의미 변경의
승인이 아니다. Protected evidence는 승인된 환경 기록 시스템에 append-only/immutable 사실로 보존하고,
shared transition artifact와 공통 governance 문서는 single-writer로 편집한다.

## Non-Negotiable Safety

다음 항목은 코드 감소를 이유로 생략하거나 약화하지 않는다.

- 외부 입력과 SQL AST validation
- Source, schema, relation, function과 operator allowlist
- 최소 권한 reader, read-only transaction, timeout, concurrency와 row/byte limit
- Query cancel, rollback과 client disconnect 처리
- Credential, token, SQL literal과 내부 database 오류 비공개
- Metadata/SQL revision 일치, 검출 가능한 schema/revision drift의 fail-closed와 tenant/source authorization
- 데이터 손실을 막는 오류 처리와 복구 절차

안전 정책은 prompt나 호출자 관례가 아니라 gateway와 PostgreSQL이 강제한다. Revision이 포착하지 못하는
privileged DDL/function/operator/collation/semantic-setting drift는 [ADR 0035](docs/decisions/0035-reviewed-source-package-inventory.md)의
reviewed inventory와 [ADR 0025](docs/decisions/0025-static-non-rls-first-launch.md)의 serving freeze로 완화한다.
더 짧은 구현과 더 안전한 구현이 충돌하면 안전한 구현을 선택한다.
