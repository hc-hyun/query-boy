# Assurance Module

Status: Physical package boundary active

## 목적

### 30초 요약

Assurance는 production publication 조건을 별도 artifact로 판정하지 않는다. Source publication에 필요한
marker와 semantic completeness는 Metadata가 직접 검사한다. 이 module은 versioned security corpus와
하나의 작은 test-local PostgreSQL source로 product safety와 end-to-end lifecycle을 검증하는
integration/container/load/soak repository gate를 소유한다.

Source별 질문·예상 결과 registry, offline result hash command와 자연어 metadata score는 current
interface가 아니다. 대표적인 deterministic SQL은 test-local fixture로만 둘 수 있으며 source onboarding
artifact나 Runtime dependency가 되지 않는다.

Generic Registry, HTTP, MCP와 operator 검사는 active source 이름·개수를 복제하지 않고 discovered
inventory의 completeness, 정렬, public projection과 secret redaction을 검증한다. Named source는 필요한
행동을 만드는 test-local fixture로만 사용한다.

## 소유 책임

- Versioned security evaluation corpus와 fail-closed assertions
- HTTP/MCP, PostgreSQL integration, container와 lifecycle repository gates
- Bounded load/soak와 cancel·rollback·disconnect evidence
- Result OID/canonical encoding negative tests
- Exact commit/CI provenance와 protected evidence의 분리

## 소유하지 않는 책임

- Source publication, view marker 또는 semantic admission 판정
- Source별 business question/result baseline이나 Runtime startup registry
- SQL allowlist, metadata revision 또는 result encoding policy 자체
- Production composition, protected DB apply 또는 environment change record
- Source package/YAML, DB view definition과 reader grant ownership

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`assurance/__init__.py`](../../../src/query_man/assurance/__init__.py) | Marker-only package boundary |
| [`config/security-evaluation.yaml`](../../../config/security-evaluation.yaml) | Versioned malicious/invalid input corpus |
| [`test_security_evaluation.py`](../../../tests/test_security_evaluation.py) | Security corpus contract와 fail-closed execution |
| [`test_database_integration.py`](../../../tests/test_database_integration.py) | 작은 합성 source를 통한 real PostgreSQL safety kernel |
| [`tests/fixtures/config/sources`](../../../tests/fixtures/config/sources) | Production inventory와 분리된 test-local source package |
| [`test_integration.py`](../../../tests/test_integration.py) | DB 없이 검증하는 composition·disconnect failure path |
| [`test_mcp_server_soak.py`](../../../tests/test_mcp_server_soak.py) | MCP soak/lifecycle behavior |
| [`scripts/verify-container.sh`](../../../scripts/verify-container.sh) | Built container acceptance |
| [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml), [`.github/workflows/mcp-soak.yml`](../../../.github/workflows/mcp-soak.yml) | Repository CI lanes |

## 제공 인터페이스와 소유 경계

Assurance의 제공 interface는 Python result registry가 아니라 runnable repository commands와 observable
pass/fail이다. Tests는 production Source Catalog, Metadata, Guarded Query, Delivery와 Runtime 경로를
사용하며 안전 정책을 test-only shortcut으로 우회하지 않는다.

Security corpus는 unknown version/field, duplicate ID와 incomplete coverage를 거부한다. Integration은
하나의 test-local source에서 PostgreSQL reader privilege, read-only transaction, live catalog/revision,
row/byte/OID limit, cancel/rollback과 connection reuse를 검사한다. AST/relation/function/operator
allowlist와 DB가 필요 없는 disconnect/composition 실패 경로는 빠른 unit gate에서 검사한다. Container
gate는 built image의 hardening, readiness와 작은 HTTP/MCP public smoke를 확인한다.

Deterministic test SQL은 fixture가 예상한 최소 사실을 확인하기 위한 test-local assertion이다. 별도
source artifact, Runtime admission dependency, 사용자 SQL allowlist 또는 protected data correctness
증명으로 승격하지 않는다.

Production source package는 repository에서 layout, manifest와 desired-view SQL 정책을 정적으로
검증한다. Required CI가 각 업무 DB의 schema·seed를 복제하거나 business row count를 판정하지 않는다.
실제 dependency, output과 grant compatibility는 별도 승인된 DBA apply와 Runtime direct admission에서
확인한다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | Active source package와 reader policy | Test가 YAML/SQL을 다른 authority로 재해석하지 않음 |
| Metadata | Marker/direct admission, revision/context | Publication shortcut을 만들지 않음 |
| Guarded Query | Production validation/execution/result path | Literal·credential·DB error 비공개를 유지 |
| Delivery/Runtime | HTTP/MCP/composition/lifecycle | Test cleanup이 실제 process lifecycle을 반영 |

## 불변조건

- Repository gate는 production safety policy와 cleanup 경로를 우회하지 않는다.
- Security corpus의 unknown/incomplete definition은 fail-closed한다.
- Exact seven result OID `20, 21, 23, 25, 1082, 1184, 1700`과 canonical encoding 경계를 검증한다.
- Source별 expected result나 자연어 score를 publication 조건으로 복원하지 않는다.
- Generic test는 production source count나 complete ID list를 별도 acceptance authority로 복제하지 않는다.
- 실DB gate는 test-local package 하나만 사용하고 production source별 schema·seed fixture를 요구하지 않는다.
- Test-local SQL은 onboarding artifact나 Runtime config가 아니다.
- Repository PASS는 exact commit/CI provenance에 연결하고 protected 실행 완료로 표현하지 않는다.
- Protected evidence는 별도 승인 뒤 environment의 append-only/immutable change record에 남긴다.

## 모듈 내부 변경

같은 안전 의미를 보존하는 test helper, tiny fixture ordering, CI job naming과 report formatting은 내부
변경이다. Flaky test 완화가 timeout/cancel/resource bound를 약화하지 않도록 owner test와 함께 검토한다.

## 사용자 승인이 필요한 경계 변경

- Required CI/container/load/soak gate와 pass/fail 의미
- Security corpus version/coverage와 safety policy identity
- Result OID/canonical encoding, revision mismatch와 cancellation lifecycle
- Source publication용 별도 acceptance authority나 offline composition 재도입
- Protected apply/evidence procedure 또는 fixture topology의 운영 의미

## 검증

```bash
uv run pytest tests/test_security_evaluation.py tests/test_integration.py
uv run pytest -m integration tests/test_database_integration.py
uv run pytest tests/test_mcp_server_soak.py
bash scripts/verify-container.sh
```

전체 repository gate는 root README와 CI workflow의 pinned command를 따른다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Security corpus | `config/security-evaluation.yaml`, `test_security_evaluation.py`, policy owner tests |
| PostgreSQL integration | test-local fixture SQL/compose, `test_database_integration.py`, catalog/query lifecycle |
| Container | Docker/Compose, `verify-container.sh`, CI workflow |
| Load/soak | load/soak tests, operations counters, cancel/shutdown paths |
| Evidence/procedure | development guidelines, operations, verification index와 active TODO |
