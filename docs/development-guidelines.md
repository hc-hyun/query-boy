# Query Man 활성 개발 지침

Status: Current repository-wide development instructions

Root [agent router](../AGENTS.md)의 빠른 시작·승인 trigger·안전 불변조건과 이 문서는 하나의 개발
지침이다. Repository 전체를 선행 학습하지 않고 primary module과 변경 종류에 해당하는 절만 읽는다.
기본 개발
방식은 Ponytail `full` 스타일이며 목표는 코드 골프가 아니라 요구사항을 안전하게 만족하는 가장
작고 단순한 변경이다.

Reference: [Ponytail](https://github.com/DietrichGebert/ponytail)

## 필요한 절만 읽는 법

| 변경 종류 | 읽을 절 |
|---|---|
| Module 하나의 구현 또는 shared file | [Module-Scoped Development](#module-scoped-development) |
| Interface·wire·format·policy·lifecycle·procedure | [Module Interface와 승인 대상 변경](#module-interface와-승인-대상-변경) |
| 새 코드·abstraction·dependency | [Decision Ladder](#decision-ladder), [Implementation Rules](#implementation-rules), [Dependencies](#dependencies) |
| 테스트 선택과 완료 gate | [Tests](#tests) |
| 문서와 완료 보고 | [Documentation And Handoff](#documentation-and-handoff) |

하나의 변경이 여러 범주에 걸치면 해당 절을 함께 읽는다. Root router와 primary module README가 이미
충분히 답한 내용을 찾으려고 이 문서의 다른 절까지 확장하지 않는다.

## Module-Scoped Development

Query Man은 하나의 repository, wheel과 deployable process를 유지하는 modular monolith다. Static
core는 owner별 여섯 physical package, 비활성 managed 기능은 같은 repository의 `query_man.managed`
package에 둔다. 논리 module의 owner, 현재 leaf-file mapping, 허용 dependency와 module interface는
[module index](modules/README.md)를 유일한 시작점으로 사용한다.

- 작업 시작 시 repository 전체를 선행 학습하지 않는다. Root router, module index, primary module의
  `README.md`, 그 문서가 지정한 package leaf·root test와 관련 ADR을 먼저 읽는다. Marker-only
  `__init__.py`에서 interface re-export를 찾거나 old flat import를 만들지 않는다.
- “관련 실행 흐름과 trust boundary를 끝까지 읽는다”는 repository 전체가 아니라 변경이 영향을
  주는 완전한 end-to-end slice를 뜻한다. Public producer/entry부터 변경 지점, 직접 consumer,
  persistence/transaction/cleanup 경계, 실패 경로와 runnable test까지 확인한다.
- 흐름이나 trust boundary가 다른 module로 넘어가면 그 module 문서와 직접 관련된
  interface/code/test만 추가로 읽는다. 보안 경계는 module 경계에서 조사를 중단할 이유가 되지 않는다.
- 다른 module의 implementation, table 또는 private symbol에 새로 의존하지 않고 owner가 공개한
  module interface를 소비한다. Production server 조립은 Runtime, 후보 source의 격리 staging 조립은
  Control Plane, offline acceptance 조립은 Assurance CLI entrypoint만 수행한다.
- 현재 shared transition file을 수정하면 module index에 표시된 모든 owner 문서를 읽고 symbol
  단위로 변경한다. Shared file 정리와 업무 변경을 한 diff에 섞지 않는다.
- Shared transition file과 공통 interface/governance 문서는 single-writer로 다룬다. 병렬 agent가
  동시에 편집하지 않고 coordinating agent가 owner와 변경 순서를 지정한다.
- Agent 한 명은 기본적으로 primary module 하나를 맡는다. Interface와 별도 경계가 고정된 서로 다른
  module의 implementation은 병렬로 개발할 수 있다.
- Coordinating agent는 병렬 작업을 시작하기 전에 각 agent에게 primary module, 승인된 change-set
  ID와 baseline commit, 수정 가능한 file allowlist, 읽기 전용 provider/consumer file, 수정 금지
  shared file, 필수 test와 interface 또는 별도 승인 대상 경계 변경 발견 시 중단 조건을 지정한다.
  할당되지 않은 file까지 정리하거나 공통 문서를 함께 갱신하지 않는다.
- 여러 agent가 같은 worktree를 공유하면 `git add`, commit, rebase, merge와 push는 coordinating
  agent만 수행한다. 별도 worktree/branch와 Git 권한을 명시적으로 할당받은 경우만 예외로 한다.
- Module별 focused test는 빠른 feedback용이며 root 전체 gate를 대체하지 않는다. Provider interface를
  사용하는 코드를 바꾸면 provider와 직접 consumer test를 함께 실행한다.

## Module Interface와 승인 대상 변경

### Official module interface

`Module interface`는 provider가 allowed dependency map과 자기 module 문서에서 다른 logical
module이 사용하도록 명시적으로 공개한 Python constant/enum/type/DTO/domain error, Protocol,
function, method, use case와 lifecycle capability다. 그 의미는 Python shape/signature와 호출 단위
input/output/domain-error semantics로 한정한다. 단순히 Python 이름이 public이거나 여러 module이
같은 동작에 관심을 가진다는 이유만으로 module interface가 되지는 않는다. Policy, ordering,
limit와 lifecycle outcome은 아래 별도 범주로 분류한다.

다음 범주는 중요하지만 그 자체로 module interface라고 부르지 않는다.

- `External API/wire format`: HTTP/MCP/CLI request, response, status, error, authentication과 protocol version
- `Persisted/versioned format`: DB/file/config schema, codec, version과 migration/rollback 의미
- `Policy/compatibility identity`: revision, fingerprint, canonical encoding/hash, allowlist,
  reader/tenant/resource policy와 limit 의미
- `Safety/lifecycle invariant`: authorize, validate, admit, transaction, cancel, rollback, reload,
  shutdown과 cleanup의 externally required outcome
- `Ownership/composition boundary`: 허용 dependency, private implementation 접근 금지와 composition-root 권한
- `Protected operational procedure`: protected inventory, freeze, DDL, cutover, rollback plan과
  stop/rollback condition
- `Operational execution authorization`: 실제 protected action의 access, scope, target, stop condition과
  change-record 책임
- `Evidence/change record`: 실행된 사실을 보존하는 append-only/immutable evidence와 provenance
- `Shared transition artifact`: 여러 owner가 함께 검토하고 single-writer가 편집해야 하는 file, 문서와 test

하나의 변경이 여러 범주에 걸칠 수 있다. 예를 들어 SQL-policy descriptor의 Python shape는 module
interface지만 digest 재료는 policy/compatibility identity이고, HTTP projection은 external wire
format이다. 범주가 여러 개라는 이유로 관련 없는 변경까지 하나의 승인안으로 묶지 않는다.

### 승인 규칙

- Module interface의 의미 변경은 additive change를 포함해 사용자의 명시적 승인 없이 진행하지 않는다.
- External API/wire, persisted/versioned format, policy/compatibility identity, safety/lifecycle invariant,
  ownership/composition boundary와 protected operational procedure의 의미 변경도 각 범주의 정확한
  영향으로 별도 승인받는다. 이를 module interface 변경이라고 부르거나 서로 자동 승인된 것으로
  간주하지 않는다.
- Protected environment의 operational action은 repository 변경이나 procedure 승인과 별개다. 실제
  access, scope, target, stop condition과 change-record 책임을 확인한 별도 실행 승인이 필요하다.
- Evidence/change record는 실행 시점의 사실을 보존한다. 과거 기록을 현재 의미에 맞춰 조용히
  수정·삭제하지 않고, 정정이 필요하면 원문과 provenance를 남긴 새 기록을 추가한다.
- Shared transition artifact라는 사실은 single-writer 요구만 만든다. 그 자체가 의미 변경이나 사용자
  승인 필요성을 뜻하지 않는다.
- 공식 interface와 위 경계의 의미를 보존하는 private helper, algorithm, lock 구현, file move와
  오탈자 수정은 module 내부 변경이다. Consumer가 의존하지 않는 내부 순서도 module interface가 아니다.
- 승인 대상 의미 변경 필요성을 발견하면 code/schema/config와 해당 의미 문서 수정을 멈추고 현재
  의미, 제안 의미와 이유, provider/consumer 또는 external/persisted/operational 영향,
  compatibility/migration/rollback, 보안·데이터 손실 영향과 검증 계획을 사용자에게 제시한다.
  읽기 전용 조사와 제안은 계속할 수 있다.
- 사용자가 정확한 변경 내용과 영향 범위를 승인한 뒤에만 진행한다. 원래 요청에 그 내용이
  구체적으로 명시되어 있으면 그 범위는 승인된 것으로 본다. 일반적인 “구현”, “refactor”, “정리”
  요청이나 coordinating/sub-agent의 동의는 승인이 아니다.
- 분류가 불명확하거나 둘 이상의 authority가 다른 의미를 주장하면 임의로 하나의 포괄 범주로
  묶지 말고 변경을 멈춰 사용자에게 보고한다. 하나의 확정된 authority와 현재 동작을 맞추는 bug
  fix와 사실 문서 정정은 새 의미 선택이 아니다.
- 승인된 change set은 하나의 coordinating workstream에서 먼저 baseline을 확정한다. 그 change
  set의 interface, format, policy, invariant, ownership 또는 procedure 의미를 사용하는 병렬 작업은
  새 baseline이 확정될 때까지 동결한다.
- 승인 뒤 owner와 직접 consumer의 module 문서, 해당되는 external/persisted format, policy,
  invariant, operational procedure, ADR/migration/onboarding 절차, evidence/change-record
  schema/template 및 interface/integration/acceptance test를 code와 같은 변경에서 갱신한다. 실제
  evidence/change record는 별도 승인된 operational action을 수행한 뒤에만 append한다. 승인 범위를
  넘으면 다시 승인받는다.
- Accepted ADR, 실제 module interface, external API/wire, persisted/versioned format,
  policy/compatibility identity, safety/lifecycle invariant, operational procedure, runnable test,
  evidence/change record와 module 문서가 충돌하면 임의로 선택하지 말고 불일치를 사용자에게
  보고한다. `implementation pending`인 목표를 현재 승인 baseline이나 지원 동작으로 오해하지 않는다.

## Decision Ladder

코드를 작성하기 전에 아래 순서로 확인하고, 요구사항을 충족하는 첫 단계에서 멈춘다.

1. 실제로 필요한가? 현재 요구나 검증된 문제가 없으면 만들지 않는다.
2. 이미 repository에 같은 역할의 코드, 설정, 문서 또는 pattern이 있는가? 먼저 재사용한다.
3. Python 표준 라이브러리로 충분한가? 충분하면 새 helper나 dependency를 만들지 않는다.
4. PostgreSQL constraint, role, transaction 또는 Docker 같은 기존 platform 기능으로 해결할 수 있는가?
   애플리케이션 코드보다 이를 우선한다.
5. 이미 설치된 dependency가 해결하는가? 새 dependency보다 기존 것을 사용한다.
6. 위 방법으로 해결되지 않을 때만 최소한의 새 코드와 dependency를 추가한다.

## Implementation Rules

- 변경 전 관련 실행 흐름, trust boundary와 테스트를 끝까지 읽는다. 작은 diff는 충분한 이해 뒤에 선택한다.
- 요청된 현재 동작만 구현한다. 미래 확장용 interface, factory, wrapper, plugin point와 boilerplate를
  미리 만들지 않는다.
- 새 abstraction은 중복된 실제 사용 사례가 생겼을 때 도입한다. 한 번 쓰는 코드는 가까운 위치에 둔다.
- 가능하면 추가보다 삭제, 영리한 기법보다 명시적이고 평범한 코드를 선택한다.
- 파일, class와 configuration layer 수를 최소화한다. 이름만 바꿔 전달하는 계층은 만들지 않는다.
- Source별 차이는 Python 분기문이 아니라 `config/sources`, budget profile과 curated database view로 표현한다.
- 복잡한 요청은 독립적으로 검증 가능한 가장 작은 end-to-end slice부터 완료한다.
- 의도적으로 단순화해 알려진 한계가 생기면 `ponytail:` comment에 한계와 확장 조건을 짧게 기록한다.

```python
# ponytail: process-local limit; move to a distributed limiter when replicas share a quota.
```

Root [agent router](../AGENTS.md#non-negotiable-safety)의 안전 불변조건은 이 구현 규칙보다 우선하며
코드 감소를 이유로 약화하지 않는다.

## Dependencies

- Dependency 추가 전 표준 라이브러리, PostgreSQL과 기존 dependency로 해결 가능한지 확인한다.
- 새 dependency는 직접 구현보다 유지보수와 보안 위험이 작을 때만 추가한다.
- PostgreSQL parser처럼 protocol/version에 묶인 dependency는 대상 PostgreSQL major version과 호환성을 테스트한다.
- Dependency 변경 시 `pyproject.toml`과 `uv.lock`을 함께 갱신한다.

## Tests

- 테스트와 공통 helper는 repository root의 `tests/`에만 둔다. Unit/integration/load 구분은 별도
  `test/` tree가 아니라 pytest marker와 파일명으로 표현한다.
- Branch, loop, parser, cache, concurrency, 비용 또는 보안 경계를 변경하면 그 동작을 깨뜨렸을 때
  실패하는 runnable test를 남긴다.
- 기존 pytest helper와 integration fixture를 우선 재사용한다. 테스트용 framework나 abstraction을
  새로 만들지 않는다.
- 단순 전달 코드에는 과도한 단위 테스트를 만들지 않는다.
- 보안 parser와 데이터 손실 경로는 최소 테스트 원칙의 예외다. 허용·거부 corpus, 우회 사례와
  property test를 유지한다.
- 완료 전 최소한 `uv run ruff check .`, `uv run mypy src`, `uv run pytest`를 실행한다. DB 경계를
  변경하면 해당 CI lane의 integration test도 실행한다. Repository 전체 `uv run pytest -m integration`
  gate는 아래 managed acceptance fixture session에서 실행한다.

### Managed acceptance fixture

Base `compose.yaml`과 `scripts/apply-db.sh`는 current 두 static source만 준비한다. Control DB와
support/commerce onboarding fixture가 필요한 managed test에서만 다음 opt-in overlay를 사용한다.
Overlay의 top-level project는 `query-man-managed-acceptance`이며 base `query-man`과 다른 PostgreSQL
container 및 `postgres_data` volume을 쓴다. Integration fixture가 내부에서 실행하는 bare
`docker compose` subprocess도 같은 project를 보도록 이 test session에는 `COMPOSE_FILE`을 전달한다.

```bash
export COMPOSE_FILE=compose.yaml:compose.acceptance.yaml
docker compose up -d --wait postgres
./scripts/apply-managed-acceptance-fixtures.sh
# 필요한 managed focused/integration test 실행
docker compose down -v --remove-orphans
unset COMPOSE_FILE
```

`down -v`는 이 명령으로 만든 managed-acceptance volume만 삭제하며 base `query-man_postgres_data`를
재사용하거나 삭제하지 않는다. 이는 disposable local/CI acceptance 절차이며 production migration이나
managed activation 승인이 아니다. CI도 `core-static`과 `managed-acceptance` job을 분리하며 static
gate가 managed fixture를 암묵적으로 준비하거나 managed test failure를 숨기지 않는다.

## Documentation And Handoff

- 구현과 같은 변경에서 관련 ADR, module interface, external API/wire, persisted/versioned format,
  policy/compatibility identity, safety/lifecycle invariant, protected operational procedure,
  onboarding 절차, evidence/change-record schema/template와 `docs/implementation-roadmap.md` checklist를
  갱신한다.
- 실제 evidence/change record는 별도 승인된 operational action을 수행한 뒤에만 append한다.
- 완료 보고는 구현 결과, 검증 결과, 의도적으로 생략한 범위와 추가해야 할 조건만 짧게 남긴다.
- 구현하지 않은 미래 기능을 완료한 것처럼 문서화하지 않는다.
