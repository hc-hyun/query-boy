# Source Catalog Module

Status: Physical package boundary active

## 목적

### 30초 요약

Source Catalog는 Git에서 review한 `config/sources/*.yaml`과 `config/budget-profiles.yaml`을 strict
validation해 immutable `SourceProfile`로 만든다. Runtime, Metadata, Guarded Query와 Delivery는
`SourceReader`만 소비한다. Source를 DB나 API에서 동적으로 추가·수정하는 writer는 없다.

Password 값은 YAML에 저장하지 않고 manifest가 가리키는 environment key에서 resolve한다. RLS source,
금지 schema, unknown field/version과 잘못된 reader 정책은 fail-closed한다.

## 소유 책임

- Source manifest와 budget profile YAML schema/version/validation
- `SourceProfile`, connection, allowlist, provenance, semantic overlay와 budget DTO
- `SourceReader`와 process-start `SourceRegistry` loading
- PostgreSQL 18, UTF-8, no-SQL connection/session reader policy
- Source onboarding Skill의 YAML pull-request plan, no-PII curated-view handoff와 secret/mutation 금지 경계

## 소유하지 않는 책임

- PostgreSQL catalog 수집, comment/type/precision-scale 해석과 metadata revision
- SQL AST validation, plan admission, query 실행/cancel/rollback
- HTTP/MCP 인증·인가와 operator CLI rendering
- DB DDL, reader credential 생성·저장, protected 배포
- Control DB, generation, mutation receipt, hot reload 또는 runtime source writer

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`source_catalog/models.py`](../../../src/query_man/source_catalog/models.py) | Immutable source, budget, semantic/provenance DTO |
| [`source_catalog/registry.py`](../../../src/query_man/source_catalog/registry.py) | Strict YAML parser, validator, `SourceReader`, `SourceRegistry` |
| [`source_catalog/reader_policy.py`](../../../src/query_man/source_catalog/reader_policy.py) | PostgreSQL connection/session policy |
| [`config/sources/`](../../../config/sources/) | Git-reviewed source manifests |
| [`config/budget-profiles.yaml`](../../../config/budget-profiles.yaml) | Versioned query/metadata budgets |
| [`test_registry.py`](../../../tests/test_registry.py), [`test_reader_policy.py`](../../../tests/test_reader_policy.py) | Focused behavior and interface tests |

## 제공 인터페이스와 소유 경계

`source_catalog.models`의 immutable DTO와 `source_catalog.registry.SourceReader`가 다른 runtime module에
제공되는 interface다.

```python
class SourceReader(Protocol):
    def list(self) -> list[dict[str, str]]: ...
    def get(self, source_id: str) -> SourceProfile | None: ...
    def source_ids(self) -> frozenset[str]: ...
```

`SourceRegistry.load(source_directory, budget_file, environment)`는 Runtime과 offline Assurance가
사용하는 composition capability다. 반환 graph는 tuple과 read-only mapping으로 고정하며 호출자가
manifest alias를 통해 published profile을 바꿀 수 없다. `list()`는 credential/host/database/user를
노출하지 않는 summary만 반환한다.

`RegistryConfigurationError`는 YAML, environment resolution과 validation 실패를 대표한다.
`SourceNotFoundError`의 domain 의미도 Source Catalog가 소유하며 external envelope는 Delivery가 소유한다.

`source_catalog.reader_policy`의 public API는 reader connection 확인, session policy 검증, canonical
client encoding/timezone과 transaction-local budget 값을 제공한다. 현재 Metadata와 Guarded Query는
`require_reader_connection_policy`, `require_reader_session_policy`, `reader_session_budget_values`,
`ReaderSessionPolicyError`와 관련 setting 상수를 pool checkout마다 사용한다. 이 목록은 symbol registry가
아니며 안정된 policy behavior가 interface다. 두 consumer는 connection 확인 후 read-only transaction과
transaction-local settings를 적용한다.
Database `TEMP` privilege 보유 여부는 reader admission 조건이 아니다. Query Man 사용자 SQL의
temporary relation/DDL 차단은 Guarded Query가 계속 소유하며, 자세한 경계는
[ADR 0032](../../decisions/0032-reader-temp-admission-relaxation.md)를 따른다.

Git YAML schema와 canonical source fields는 persisted/versioned format이다. `source_id`, allowed schemas,
relation kinds, budget reference, semantic overlay와 provenance 의미를 바꾸면 consumer revision과 운영
절차를 함께 검토한다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Runtime | YAML/budget path와 environment | 올바른 repository revision을 선택하고 startup failure를 ready로 숨기지 않음 |
| Metadata | Catalog/type/revision behavior | Source Catalog가 DB catalog 의미를 추측하지 않음 |
| Assurance | Verified/quality artifact membership | Source ID가 Git YAML inventory와 정확히 일치함 |

Delivery, Metadata와 Guarded Query는 `SourceReader`를 주입받는다. 이들은 YAML을 직접 재해석하거나
registry를 mutate하지 않는다.

## 불변조건

- `config/sources/*.yaml`과 budget YAML의 Git-reviewed revision만 source authority다.
- Unknown field/version, duplicate ID, missing environment, forbidden schema와 RLS는 거부한다.
- Password/DSN secret은 YAML, public projection, log와 error에 노출하지 않는다.
- Allowed schema/relation kind와 resource budget은 prompt나 caller가 완화할 수 없다.
- Budget은 `max_concurrent_queries <= max_pool_size`를 만족해야 하며 위반하면 startup validation에서
  fail-closed한다. Pool을 늘리는 것은 허용하지만 query concurrency는 pool capacity를 넘을 수 없다.
- Published DTO graph는 deep immutable하며 입력 alias mutation의 영향을 받지 않는다.
- Reader connection/session policy는 DB query 전에 검증한다.
- Database `TEMP` privilege만으로 source를 거부하거나 전역 revoke를 onboarding 조건으로 요구하지 않는다.
- Runtime hot reload, managed fallback 또는 DB-backed source mutation은 없다.

## 모듈 내부 변경

공개 shape와 YAML/reader-policy 의미를 보존하는 private parser helper, error wording, internal iteration과
focused test 정리는 module 내부 변경이다. 새 abstraction보다 기존 strict model과 plain function을
우선한다.

## 사용자 승인이 필요한 경계 변경

- Sanitized source projection과 `SourceNotFoundError` public 의미
- Source/budget YAML schema, version, field default와 environment resolution
- Source ID, allowlist, semantic overlay, provenance, budget와 revision material 의미
- RLS/reader PostgreSQL version·encoding·session policy
- Git authority, onboarding review, deployment/restart와 secret procedure
- Source writer, hot reload, DB authority 또는 다른 fallback 재도입

## 검증

```bash
uv run pytest tests/test_registry.py tests/test_reader_policy.py tests/test_revision.py
uv run pytest tests/test_assurance_cli.py tests/test_operator_shell.py tests/test_runtime_config.py
```

Source 추가는 `qm source validate`와 전체 gate를 통과해야 한다. 실제 DB 대상이면 catalog/query
integration도 수행하며 protected credential/DDL/deploy는 별도 실행 승인을 받는다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Manifest, budget, semantic validation | `source_catalog/models.py`, `registry.py`, sample YAML, `test_registry.py` |
| Reader connection/session policy | `reader_policy.py`, Metadata/Guarded Query direct consumer, `test_reader_policy.py` |
| Metadata revision 영향 | `metadata/revision.py`, `test_revision.py` |
| Onboarding workflow | `skills/query-man-source-onboarding/`, source onboarding docs, `test_onboarding_skill.py` |
| Runtime/CLI loading | `runtime/config.py`, `composition.py`, `operator_shell.py`와 focused tests |
