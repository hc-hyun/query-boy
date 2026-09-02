# Source Catalog Module

Status: Physical package boundary active

## 목적

### 30초 요약

Source Catalog는 Git에서 review한 source package와 budget YAML을 strict validation해 immutable
`SourceProfile`로 만든다. Source 하나는 정확히 다음 두 파일이다.

```text
config/sources/<source-id>/
  source.yaml
  views.sql
```

Manifest는 version 4이며 `view_contract_version`을 필수로 갖고 공개 relation 종류는 view만 허용한다.
Registry는 두 파일의 이름·위치와 YAML을 검사하지만 SQL 내용을 해석하거나 실행하지 않는다. Runtime,
Metadata, Guarded Query와 Delivery는 `SourceReader`만 소비하며 실행 중 source writer나 fallback authority는
없다.

## 소유 책임

- Source package의 exact directory/file layout와 manifest version 4 validation
- `SourceProfile`, connection, allowlist, provenance, semantic overlay와 budget DTO
- `SourceReader`와 process-start `SourceRegistry` loading
- PostgreSQL 18, UTF-8, no-SQL connection/session reader policy
- 명시적 PostgreSQL TLS mode와 `require` compatibility exception 경계
- Source onboarding의 package 변경 plan과 DB/data owner·DBA handoff

## 소유하지 않는 책임

- `views.sql` 실행, DB role/credential 생성, traffic 전환과 protected rollback
- PostgreSQL catalog 수집, view marker 해석, metadata admission과 revision
- SQL AST validation, query 실행/cancel/rollback
- HTTP/MCP 인증·인가와 operator CLI rendering
- PII 탐지·분류·마스킹 또는 DB owner의 no-PII 확인 대행
- Control DB, hot reload, runtime source writer 또는 SQL migration hook

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`source_catalog/models.py`](../../../src/query_man/source_catalog/models.py) | Immutable source, budget, semantic/provenance DTO |
| [`source_catalog/registry.py`](../../../src/query_man/source_catalog/registry.py) | Exact package와 strict YAML validation, `SourceReader` |
| [`source_catalog/reader_policy.py`](../../../src/query_man/source_catalog/reader_policy.py) | PostgreSQL connection/session policy |
| [`config/sources/`](../../../config/sources/) | Source별 `source.yaml`과 desired `views.sql` |
| [`config/budget-profiles.yaml`](../../../config/budget-profiles.yaml) | Versioned query/metadata budgets |
| [`test_registry.py`](../../../tests/test_registry.py), [`test_reader_policy.py`](../../../tests/test_reader_policy.py) | Focused behavior tests |

## 제공 인터페이스와 소유 경계

`source_catalog.models`의 immutable DTO와 `source_catalog.registry.SourceReader`가 내부 consumer에
제공되는 interface다.

```python
class SourceReader(Protocol):
    def list(self) -> list[dict[str, str]]: ...
    def get(self, source_id: str) -> SourceProfile | None: ...
    def source_ids(self) -> frozenset[str]: ...
```

`SourceRegistry.load(source_directory, budget_file, environment)`는 각 immediate child directory를 source
후보로 읽는다. Directory 이름과 `source_id`가 같고 regular non-symlink `source.yaml`, `views.sql`만
있어야 한다. Flat YAML, `.yml`, unknown file, nested directory와 구 format fallback은 없다.

`source.yaml`은 `allowed_relation_kinds: [view]`, 양의 정수 `view_contract_version`, 같은 package의
`views.sql`을 가리키는 `provenance.database_migration_ref`를 요구한다. `views.sql`의 존재를 확인하되
애플리케이션이 내용을 읽거나 실행하는 capability로 만들지 않는다.

`RegistryConfigurationError`는 package, YAML, environment resolution과 validation 실패를 대표한다.
`SourceNotFoundError`의 domain 의미도 Source Catalog가 소유하며 external envelope는 Delivery가 소유한다.

Reader policy API는 canonical client encoding/timezone, connection과 transaction-local session budget
검사를 제공한다. Metadata와 Guarded Query는 pool checkout마다 이를 적용한다. `sslmode`는 exact
`disable`, `require`, `verify-full` 중 하나이며 TCP와 실제 libpq transport state가 일치하지 않으면
연결을 닫고 fail-closed한다. Database `TEMP` privilege 보유 여부는 admission 조건이 아니지만 사용자
SQL의 DDL과 temporary relation은 Guarded Query가 계속 차단한다.

## 소비 인터페이스와 전제

| Consumer | 소비 항목 | 전제 |
|---|---|---|
| Runtime | Package/budget path, environment와 source projection | Startup failure를 ready로 숨기지 않고 SQL을 실행하지 않음 |
| Metadata | Source profile, semantic overlay와 reader policy | View marker·catalog·revision은 Metadata가 검증 |
| Guarded Query | Allowlist, budget, connection과 reader policy | Source 파일을 직접 다시 해석하지 않음 |
| Delivery | `SourceReader`의 public source inventory | Credential/locator를 외부에 노출하지 않음 |

## 불변조건

- Source authority는 `config/sources/<source-id>/{source.yaml,views.sql}`와 budget YAML의 reviewed revision이다.
- Source directory에는 정확히 두 regular non-symlink file만 존재한다.
- Manifest version은 4, relation kind는 view만, `view_contract_version`은 양의 정수다.
- Password 값은 Git/YAML에 없고 manifest가 지정한 environment key에서만 resolve한다.
- Unknown field/version, duplicate ID, directory/source mismatch, forbidden schema, RLS와 invalid TLS mode를 거부한다.
- Profile graph는 immutable이며 caller가 YAML alias나 mutable collection으로 바꾸지 못한다.
- Runtime은 `views.sql`을 해석·실행하지 않고 administrator credential을 받지 않는다.
- Reader는 PostgreSQL 18/UTF-8와 reviewed transport/session policy를 만족하지 않으면 fail-closed한다.

## 모듈 내부 변경

위 persisted/policy 의미를 보존하는 parser helper, immutable collection 구현, error formatting과 focused
test 정리는 내부 변경이다. Provider와 직접 consumer를 같은 change set에서 함께 수정·검증할 수 있다.

## 사용자 승인이 필요한 경계 변경

- Source package/manifest 또는 budget persisted format와 compatibility
- `view_contract_version`, allowlist, semantic overlay나 provenance 의미
- TLS/reader/session admission과 fail-closed 결과
- Source authority, runtime reload/writer/fallback 또는 DDL execution capability
- DB/data owner, DBA와 Runtime 사이의 ownership/protected procedure

Protected DB 적용은 repository 변경 승인과 별도로 exact target, access, traffic freeze, stop condition,
rollback artifact와 change-record owner의 실행 승인이 필요하다.

## 검증

```bash
uv run pytest tests/test_registry.py tests/test_reader_policy.py tests/test_operator_shell.py
```

Source/fixture 변경은 Metadata, integration, container와 static privilege 검증도 실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Package/manifest | `models.py`, `registry.py`, source package, `test_registry.py` |
| Reader/TLS/session | `reader_policy.py`, Metadata/Guarded Query consumer, `test_reader_policy.py` |
| Semantic overlay/budget | `models.py`, source YAML, Metadata validation/revision tests |
| Desired view SQL | ADR 0034, source package, fixture wiring, Metadata marker/admission tests |
| Onboarding | source extension checklist, onboarding Skill과 its tests |
