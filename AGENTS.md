# Query Man Development Guidelines

## Scope

이 지침은 repository 전체에 적용한다. 기본 개발 방식은 Ponytail `full` 스타일이다.
목표는 코드 골프가 아니라, 요구사항을 안전하게 만족하는 가장 작고 단순한 변경이다.

Reference: [Ponytail](https://github.com/DietrichGebert/ponytail)

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
