# Query Man Development Guidelines

## Scope

이 지침은 repository 전체에 적용한다. 기본 개발 방식은 Ponytail `full` 스타일이다.
목표는 코드 골프가 아니라, 요구사항을 안전하게 만족하는 가장 작고 단순한 변경이다.

Reference: [Ponytail](https://github.com/DietrichGebert/ponytail)

## Module-Scoped Development

Query Man은 하나의 deployable process를 유지하는 modular monolith다. 논리 module의 owner,
현재 평면 파일 mapping, 허용 dependency와 계약은 [`docs/modules/README.md`](docs/modules/README.md)를
유일한 시작점으로 사용한다. 아직 물리 package가 분리되지 않았다는 사실을 숨기지 않는다.

- 작업 시작 시 repository 전체를 선행 학습하지 않는다. 이 파일, module index, primary module의
  `README.md`, 그 문서가 지정한 code/test와 관련 ADR을 먼저 읽는다.
- “관련 실행 흐름과 trust boundary를 끝까지 읽는다”는 repository 전체가 아니라 변경이 영향을
  주는 완전한 end-to-end slice를 뜻한다. Public producer/entry부터 변경 지점, 직접 consumer,
  persistence/transaction/cleanup 경계, 실패 경로와 runnable test까지 확인한다.
- 흐름이나 trust boundary가 다른 module로 넘어가면 그 module 문서와 직접 관련된 계약/code/test만
  추가로 읽는다. 보안 경계는 module 경계에서 조사를 중단할 이유가 되지 않는다.
- 다른 module의 implementation, table 또는 private symbol에 새로 의존하지 않고 owner가 공개한
  contract를 소비한다. Production server 조립은 Runtime, 후보 source의 격리 staging 조립은
  Control Plane, offline acceptance 조립은 Assurance CLI entrypoint만 수행한다.
- 현재 shared transition file을 수정하면 module index에 표시된 모든 owner 문서를 읽고 symbol
  단위로 변경한다. Shared file 정리와 업무 변경을 한 diff에 섞지 않는다.
- Shared transition file과 공통 contract 문서는 single-writer로 다룬다. 병렬 agent가 동시에
  편집하지 않고 coordinating agent가 owner와 변경 순서를 지정한다.
- Agent 한 명은 기본적으로 primary module 하나를 맡는다. 계약이 고정된 서로 다른 module의
  implementation은 병렬로 개발할 수 있다.
- Coordinating agent는 병렬 작업을 시작하기 전에 각 agent에게 primary module, 승인된 contract
  ID와 baseline commit, 수정 가능한 file allowlist, 읽기 전용 provider/consumer file, 수정 금지
  shared file, 필수 test와 contract 변경 발견 시 중단 조건을 지정한다. 할당되지 않은 file까지
  정리하거나 공통 문서를 함께 갱신하지 않는다.
- 여러 agent가 같은 worktree를 공유하면 `git add`, commit, rebase, merge와 push는 coordinating
  agent만 수행한다. 별도 worktree/branch와 Git 권한을 명시적으로 할당받은 경우만 예외로 한다.
- Module별 focused test는 빠른 feedback용이며 root 전체 gate를 대체하지 않는다. Provider contract를
  사용하는 코드를 바꾸면 provider와 직접 consumer test를 함께 실행한다.

## Inter-Module Contract Changes

다른 module이 소비하는 다음 항목은 additive change를 포함해 module contract다.

- Public Python type, Protocol, function, method와 lifecycle capability
- HTTP/MCP request, response, error, authentication/authorization와 protocol version
- Source manifest, budget, persisted/versioned configuration과 public source projection
- Metadata/SQL policy revision, fingerprint, canonical encoding과 verified result hash
- Authorization, validation, admission, transaction, cancel, rollback, reload와 shutdown 순서
- Allowlist, reader/tenant policy, timeout, concurrency와 row/byte/resource limit의 의미
- Control DB schema, constraint, role/grant, lock/CAS, transaction, generation과 pin/migration 의미

Module contract는 사용자의 명시적 승인 없이 변경하지 않는다.

- 계약 변경 필요성을 발견하면 의미상 code/schema/config/contract 문서 수정을 멈추고 현재 계약,
  제안 계약과 이유, provider/consumer 영향, compatibility/migration/rollback, 보안·데이터 손실 영향,
  문서·검증 계획을 사용자에게 제시한다. 읽기 전용 조사와 제안은 계속할 수 있다.
- 사용자가 정확한 계약 변경 내용과 영향 범위를 승인한 뒤에만 진행한다. 원래 요청에 그 내용이
  구체적으로 명시되어 있으면 그 범위는 승인된 것으로 본다. 일반적인 “구현”, “refactor”, “정리”
  요청이나 coordinating/sub-agent의 동의는 승인이 아니다.
- 판단이 불명확하면 계약 변경으로 취급한다. 외부 의미가 동일한 private refactor, 파일 이동과
  오탈자 수정은 계약 변경이 아니다. 하나의 확정된 ADR/schema/test와 현재 동작에 맞추는 단순한
  사실 문서 정정도 계약 변경이 아니지만, 둘 이상의 authority가 다른 의미를 주장해 선택이
  필요하면 사용자에게 보고한다.
- 승인된 계약 변경은 하나의 coordinating workstream에서 직렬화한다. 같은 계약을 사용하는 병렬
  작업은 새 baseline이 확정될 때까지 그 경계를 동결한다.
- 승인 뒤 owner와 모든 직접 consumer의 module 문서, 필요한 ADR/migration/onboarding 절차와
  contract/integration test를 code와 같은 변경에서 갱신한다. 승인 범위를 넘으면 다시 승인받는다.
- Accepted ADR, 실제 persisted/wire schema, runnable contract test와 module 문서가 충돌하면 임의로
  선택하지 말고 불일치를 사용자에게 보고한다. `implementation pending`인 목표를 현재 계약으로
  오해하지 않는다.

## Decision Ladder

코드를 작성하기 전에 아래 순서로 확인하고, 요구사항을 충족하는 첫 단계에서 멈춘다.

1. 실제로 필요한가? 현재 요구나 검증된 문제가 없으면 만들지 않는다.
2. 이미 repository에 같은 역할의 코드, 설정, 문서 또는 pattern이 있는가? 먼저 재사용한다.
3. Python 표준 라이브러리로 충분한가? 충분하면 새 helper나 dependency를 만들지 않는다.
4. PostgreSQL constraint, role, transaction 또는 Docker 같은 기존 platform 기능으로 해결할 수 있는가? 애플리케이션 코드보다 이를 우선한다.
5. 이미 설치된 dependency가 해결하는가? 새 dependency보다 기존 것을 사용한다.
6. 위 방법으로 해결되지 않을 때만 최소한의 새 코드와 dependency를 추가한다.

## Implementation Rules

- 변경 전 관련 실행 흐름, trust boundary와 테스트를 끝까지 읽는다. 작은 diff는 충분한 이해 뒤에 선택한다.
- 요청된 현재 동작만 구현한다. 미래 확장용 interface, factory, wrapper, plugin point와 boilerplate를 미리 만들지 않는다.
- 새 abstraction은 중복된 실제 사용 사례가 생겼을 때 도입한다. 한 번 쓰는 코드는 가까운 위치에 둔다.
- 가능하면 추가보다 삭제, 영리한 기법보다 명시적이고 평범한 코드를 선택한다.
- 파일, class와 configuration layer 수를 최소화한다. 이름만 바꿔 전달하는 계층은 만들지 않는다.
- Source별 차이는 Python 분기문이 아니라 `config/sources`, budget profile과 curated database view로 표현한다.
- 복잡한 요청은 독립적으로 검증 가능한 가장 작은 end-to-end slice부터 완료한다.
- 의도적으로 단순화해 알려진 한계가 생기면 `ponytail:` comment에 한계와 확장 조건을 짧게 기록한다.

```python
# ponytail: process-local limit; move to a distributed limiter when replicas share a quota.
```

## Non-Negotiable Safety

다음 항목은 코드 감소를 이유로 생략하거나 약화하지 않는다.

- 외부 입력과 SQL AST validation
- Source, schema, relation, function과 operator allowlist
- 최소 권한 reader, read-only transaction, timeout, concurrency와 row/byte limit
- Query cancel, rollback과 client disconnect 처리
- Credential, token, SQL literal과 내부 database 오류 비공개
- Schema revision 일치, drift fail-closed와 tenant/source authorization
- 데이터 손실을 막는 오류 처리와 복구 절차

안전 정책을 prompt나 호출자 관례에 맡기지 않고 gateway와 PostgreSQL이 강제하게 한다.
더 짧은 구현과 더 안전한 구현이 충돌하면 안전한 구현을 선택한다.

## Dependencies

- Dependency 추가 전 표준 라이브러리, PostgreSQL과 기존 dependency로 해결 가능한지 확인한다.
- 새 dependency는 직접 구현보다 유지보수와 보안 위험이 작을 때만 추가한다.
- PostgreSQL parser처럼 protocol/version에 묶인 dependency는 대상 PostgreSQL major version과 호환성을 테스트한다.
- Dependency 변경 시 `pyproject.toml`과 `uv.lock`을 함께 갱신한다.

## Tests

- 테스트와 공통 helper는 repository root의 `tests/`에만 둔다. Unit/integration/load 구분은
  별도 `test/` tree가 아니라 pytest marker와 파일명으로 표현한다.
- Branch, loop, parser, cache, concurrency, 비용 또는 보안 경계를 변경하면 그 동작을 깨뜨렸을 때 실패하는 runnable test를 남긴다.
- 기존 pytest helper와 integration fixture를 우선 재사용한다. 테스트용 framework나 abstraction을 새로 만들지 않는다.
- 단순 전달 코드에는 과도한 단위 테스트를 만들지 않는다.
- 보안 parser와 데이터 손실 경로는 최소 테스트 원칙의 예외다. 허용·거부 corpus, 우회 사례와 property test를 유지한다.
- 완료 전 최소한 `uv run ruff check .`, `uv run mypy src`, `uv run pytest`를 실행한다. DB 경계를 변경하면 `uv run pytest -m integration`도 실행한다.

## Documentation And Handoff

- 구현과 같은 변경에서 관련 ADR, contract, onboarding 절차와
  `docs/implementation-roadmap.md` checklist를 갱신한다.
- 완료 보고는 구현 결과, 검증 결과, 의도적으로 생략한 범위와 추가해야 할 조건만 짧게 남긴다.
- 구현하지 않은 미래 기능을 완료한 것처럼 문서화하지 않는다.
