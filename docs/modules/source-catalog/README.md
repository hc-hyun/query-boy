# Source Catalog Module

Status: Physical package boundary active

> **현재 launch에서는 두 source만 정적으로 읽는다.** Source Catalog의 managed validation과 runtime
> projection 기능은 구현 상태로 보존되지만, [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의
> 첫 launch는 `development-issues`, `market-voc`만 bootstrap한다. 새 source와 RLS는 현재 동작에
> 몰래 추가하지 않는다.

## 목적

### 30초 요약

Source Catalog는 Query Man의 **검증된 source 주소록과 규칙 묶음**이다. “어떤 source가 있는가”,
“어디까지 공개하는가”, “어떤 reader와 query 제한을 쓰는가”를 `SourceProfile`로 제공한다.

| 질문 | 답 |
|---|---|
| 언제 이 module을 고르는가? | Source manifest, budget, semantic overlay, source registry나 reader 정책을 바꿀 때 |
| 다른 module에는 무엇을 주는가? | 읽기 전용 `SourceReader`, Control Plane 전용 `SourceProjectionWriter`, strict manifest validator와 reader-policy interface |
| 무엇을 하지 않는가? | DB catalog 수집, SQL 생성·검사·실행, caller 인증, Control DB 저장과 protected 배포 |
| 가장 중요한 안전 경계는? | Secret 비노출, strict manifest, immutable profile, 최소 권한 reader와 static/managed authority 분리 |

현재와 미래를 한 문장으로 섞지 않는다.

| 구분 | 상태 |
|---|---|
| 현재 first launch | Repository의 두 non-RLS manifest, PostgreSQL 18/server·client UTF-8와 SQL 전 connection 검사; managed-only `observability` 정의 없음 |
| 구현되어 보존된 기능 | Control Plane이 검증된 managed projection을 writer로 반영하는 capability. 현재 serving에는 조립하지 않음 |
| 새 static source | 별도 inventory review, 정확한 사용자 승인, traffic-off acceptance와 재배포가 필요 |
| 미래 후보 | RLS serving과 넓은 encoding/result 범위는 [future work](../../future-work.md)이며 현재 interface나 일정이 아님 |

Source Catalog provider는 `src/query_man/source_catalog` package에 있고 managed writer consumer는
`src/query_man/managed` package에 격리돼 있다. 둘은 같은 repository·wheel·process에 있으며 package
marker는 interface를 re-export하지 않는다.

## 소유 책임

- Strict source manifest v2와 budget-profile v2 validation
- `SourceProfile`, connection, schema/relation allowlist, tenant isolation, provenance와 semantic overlay
- Source별 credential 환경 변수 이름과 host/port resolution validation
- Optional resource-observation target의 bounded manifest definition
- Process-local `SourceRegistry`, 읽기 projection과 Control Plane용 writer capability
- Published source/semantic graph의 recursive immutability
- Metadata와 Guarded Query가 함께 쓰는 reader connection/session policy interface
- Plan-only source-onboarding Skill의 owner/admin handoff와 secret·mutation 금지 경계

## 소유하지 않는 책임

- Source DB physical catalog 수집, metadata cache/revision과 질문별 context 생성
- SQL AST validation, query transaction, result encoding, timeout, cancel과 rollback
- Control DB generation, encrypted credential persistence, transaction, convergence와 recovery
- Caller 인증·인가와 HTTP/MCP/CLI request, response, error rendering
- PostgreSQL reader role/grant, curated view와 source DB migration의 실제 생성
- Runtime process 조립, protected environment 배포, traffic cutover와 rollback 실행

쉽게 구분하면 Source Catalog는 **source 정의와 읽는 방법**을 소유한다. DB 구조로 context를 만드는
일은 [Metadata](../metadata/README.md), SQL을 안전하게 실행하는 일은
[Guarded Query](../guarded-query/README.md), managed 이력과 적용 순서는
[Control Plane](../control-plane/README.md)이 소유한다.

## 현재 코드 위치

| 위치 | Source Catalog가 소유하는 범위 | 주의점 |
|---|---|---|
| [`source_catalog/registry.py`](../../../src/query_man/source_catalog/registry.py) | Manifest/budget parser, strict validator, `SourceReader`, `SourceProjectionWriter`, concrete registry | Control Plane은 validator/writer, 일반 consumer는 reader만 사용 |
| [`source_catalog/models.py`](../../../src/query_man/source_catalog/models.py) | Source, budget, semantic, provenance와 observation-definition type | Metadata-owned catalog DTO와 분리된 canonical Source type leaf |
| [`source_catalog/reader_policy.py`](../../../src/query_man/source_catalog/reader_policy.py) | PostgreSQL connection/session compatibility와 최소 권한 검사 | Metadata·Guarded Query가 leaf path를 직접 소비 |
| [`errors.py`](../../../src/query_man/errors.py) | `SourceNotFoundError`의 domain 발생 의미 | Public HTTP/MCP envelope은 Delivery 소유 |
| [`config/sources`](../../../config/sources) | `development-issues`, `market-voc` static inventory | ADR 0025 current authority; managed desired state가 아님 |
| [`budget-profiles.yaml`](../../../config/budget-profiles.yaml) | Versioned resource-tier definitions | Source 하나를 위해 임의 override하지 않음 |
| `config/onboarding/<source>.yaml`, `config/onboarding/<source>-l2.yaml` | Managed candidate acceptance fixture | Static inventory나 production desired state가 아님 |
| [`query-man-source-onboarding`](../../../skills/query-man-source-onboarding/SKILL.md) | 변경하지 않는 owner/admin planning workflow | Production authority, credential broker나 mutation executor가 아님 |
| [`test_registry.py`](../../../tests/test_registry.py), [`test_reader_policy.py`](../../../tests/test_reader_policy.py), [`test_runtime_config.py`](../../../tests/test_runtime_config.py) | Protocol, immutability, validation, reader policy와 bootstrap input test | Provider와 직접 consumer 변경을 함께 확인 |

Package split은 완료됐고 old flat forwarding module은 없다. Consumer는 위 leaf path를 직접 import한다.
이후 file move나 interface 의미 변경을 private 구현 정리와 한 diff에 섞지 않는다.

Manifest v2의 optional `observability` schema와 managed onboarding fixture는 보존한다. Current static
`config/sources` 두 manifest는 managed reporter를 조립하지 않으므로 이 field를 선언하지 않는다.

## 제공 인터페이스와 소유 경계

이 절에서 official module interface는 다른 logical module이 사용하도록 공개한 Python
constant/type/Protocol/function과 호출 단위 input/output/domain-error 의미다. Manifest schema,
reader compatibility 숫자, RLS 정책, lifecycle 순서와 protected operation은 중요하지만 각각 별도
format, policy, safety 또는 operational boundary다.

아래 code block은 모두 **현재 코드의 exact Python shape**다. 개념 예시나 미래 signature가 아니다.

### Published source interface immutability guarantee

`SourceProfile`은 검증과 secret resolution이 끝난 process 내부 projection이다.

| Field 묶음 | 소비 의미 |
|---|---|
| `source_id`, `name`, `description` | Source의 opaque identity와 public summary |
| `connection` | Server가 resolve한 host/port/database/user/password/TLS. Caller input이 아님 |
| `allowed_schemas`, `allowed_relation_kinds` | Metadata와 query가 볼 수 있는 최대 범위 |
| `budget` | Metadata와 Guarded Query가 함께 강제하는 source-wide hard limit |
| `semantic_overlay` | Source별 Python branch 없이 grain, alias, join과 업무 의미를 제공 |
| `provenance`, `minimum_quality_level`, `tenant_isolation` | Owner/environment/migration, publish quality와 isolation 입력 |
| `control_generation`, `control_state_version` | Managed projection의 immutable generation과 pointer 순서 입력 |
| `observability` | 대표 grain/physical relation과 1~16개 distinct storage relation의 선택적 관측 정의 |

`observability`는 query relation allowlist나 metadata revision 재료가 아니며 public summary에 relation
이름을 추가하지 않는다.

`SourceProfile`과 도달 가능한 semantic graph는 recursively immutable하다. Sequence는 tuple, nested
mapping은 입력 alias를 복사한 read-only mapping이다. Profile 안에는 resolved plaintext reader
password가 있으므로 wire, persisted JSON, log, metric이나 public summary로 serialize하지 않는다.

### `SourceReader` interface

Delivery, Metadata, Guarded Query, Runtime probe와 Assurance application code는 이 read-only Protocol만
소비한다.

```python
class SourceReader(Protocol):
    def list(self) -> list[dict[str, str]]: ...
    def get(self, source_id: str) -> SourceProfile | None: ...
    def source_ids(self) -> frozenset[str]: ...
```

`list()`는 `source_id` 순으로 정렬한 `source_id`, `name`, `description`만 반환한다. `get()`은 없는
source에 `None`을 반환한다. 이를 어떤 domain error로 바꿀지는 호출 use case가 결정하지만
`SourceNotFoundError`의 source-existence 의미는 Source Catalog가 소유한다.

### `SourceProjectionWriter` interface

Control Plane의 runtime projector/reloader만 write capability를 받는다.

```python
class SourceProjectionWriter(SourceReader, Protocol):
    def upsert(self, source: SourceProfile) -> None: ...
    def remove(self, source_id: str) -> None: ...
```

Concrete `SourceRegistry`가 두 Protocol을 구조적으로 구현한다. Ordinary consumer에 concrete registry나
writer를 주지 않는다. Managed mode에서도 검증된 projection만 반영하고 bootstrap과 managed authority를
한 process에서 merge하거나 fallback하지 않는다.

`SourceRegistry.upsert()` 자체는 같은 source ID의 connection identity를 검사하지 않는다. Endpoint
재지정 거부, apply ordering, cache/pool invalidation과 rollback은 Control Plane과 함께 지키는 lifecycle
invariant이며 writer만 떼어 직접 호출해 우회하지 않는다.

### Manifest와 budget validation interfaces

Runtime composition과 Control Plane candidate staging이 사용하는 exact surface는 다음과 같다.

```python
@dataclass(frozen=True)
class ValidatedSourceManifest:
    profile: SourceProfile
    document: dict[str, object]

def load_budget_profiles(path: Path) -> dict[str, BudgetProfile]: ...

def validate_source_manifest(
    raw: object,
    budgets: Mapping[str, BudgetProfile],
    secret: str,
    *,
    origin: str = "control-plane source manifest",
) -> ValidatedSourceManifest: ...
```

Validation은 strict manifest v2와 known budget을 요구하고 unknown field/version, system schema, 잘못된
relation kind, source-scoped secret 환경 변수 이름 불일치와 모든 RLS manifest를 fail-closed한다.
Host/port 환경 변수는 publisher에서 resolve하며 canonical `document`에는 plaintext secret을 넣지 않는다.

`RegistryConfigurationError`는 deterministic configuration/validation 실패의 공개 domain marker다.
구체적인 Pydantic, environment와 filesystem 구현을 다른 module의 dependency로 공개하지 않는다.

Production Runtime과 Assurance offline CLI가 concrete `SourceRegistry.load(...)`를 조립하는 것은 module
index가 허용한 composition 경계다. 이 concrete construction을 ordinary module dependency나 managed
filesystem fallback으로 확대하지 않는다.

### Shared validation type interfaces

Delivery admin input validation은 다음 exact type 의미를 소비한다.

| Type | Exact allowed shape |
|---|---|
| `SourceEnvironment` | `production | staging | development | test` |
| `Identifier` | `^[A-Za-z_][A-Za-z0-9_$]{0,62}$`, 최대 63자 |
| `StableSlug` | `^[a-z0-9]+(?:-[a-z0-9]+)*$`, 최대 80자 |

Allowed value, pattern이나 길이를 바꾸는 것은 provider와 Delivery consumer가 공유하는 interface 의미
변경이다.

### Reader policy interfaces

Metadata와 Guarded Query가 함께 소비하는 exact symbols는 다음과 같다.

| Symbol | 역할 |
|---|---|
| `READER_CLIENT_ENCODING: Final = "UTF8"` | Pool startup 요청과 connection compatibility의 고정 encoding |
| `READER_SESSION_TIMEZONE_SETTER` | Transaction-local UTC setter SQL material |
| `READER_SESSION_BUDGET_SETTERS` | Transaction-local memory/temp/parallel/JIT setter SQL material |
| `ReaderSessionPolicyError` | Deterministic reader-policy mismatch marker |

```python
def require_reader_connection_policy(
    connection: AsyncConnection[Any],
) -> None: ...

def reader_session_budget_values(
    source: SourceProfile,
) -> tuple[str, str, str, str]: ...

async def require_reader_session_policy(
    connection: AsyncConnection[Any],
    source: SourceProfile,
    trusted_tenant: str = "",
) -> None: ...
```

Connection 검사와 session 검사는 서로 다른 단계다.

1. Pool checkout 직후 `require_reader_connection_policy`를 호출한다. 이 함수는 SQL을 실행하지 않고
   PostgreSQL 18 (`180000 <= server_version < 190000`), server/client `UTF8`와 driver `utf-8` codec을
   요구한다.
2. Deterministic mismatch는 실제 관측값 없는 `ReaderSessionPolicyError`로 끝내고 connection을
   close/discard한다. Driver/transport failure는 marker로 감싸지 않아 기존 transient 분류를 유지한다.
3. 그 뒤에만 `BEGIN`하고 transaction-local UTC, timeout/resource와 trusted search-path/tenant 값을
   설정한다.
4. `require_reader_session_policy`가 exact database/session user, read-only repeatable-read transaction,
   restricted role, TEMP/CREATE 금지, UTC/budget/JIT/parallel 값, `pg_catalog` search path,
   `row_security=on`과 trusted tenant context를 확인한다.
5. Metadata catalog나 user query는 위 검사가 끝난 뒤에만 실행한다. Role/database default를 이 흐름에서
   변경하지 않는다.

이 순서와 fail-closed 결과는 policy/safety invariant다. Python signature가 같아도 순서나 허용값의
의미가 바뀌면 별도 승인이 필요하다.

### 현재 launch policy와 보존된 경로

- Static authority는 `development-issues`, `market-voc` 두 bootstrap manifest뿐이다.
- `tenant_isolation=rls`는 bootstrap과 managed manifest validation에서 `RegistryConfigurationError`로
  거부한다. Injected registry, cold managed projection과 query 우회도 각 Runtime/Control Plane/Guarded
  Query 경계가 DB 접근 전에 fail-closed한다.
- `TenantIsolation` type, RLS implementation과 historical Control row는 물리 삭제하지 않지만 현재
  success path로 해석하지 않는다.
- Managed implementation은 보존하지만 Control DB, admin mutation, hot onboarding과 reload를 current
  first-launch serving에 조립하지 않는다.
- 새 source ID/database, manifest, budget/access policy 추가·교체는 별도 inventory review와 배포 승인이
  필요하다.
- 넓은 encoding과 RLS attestation은 [future work](../../future-work.md)다. ADR 0020/0024와
  [RLS finding](../../verification/2026-08-26-rls-policy-drift.md)은 연구·증거이지 현재 지원 동작이 아니다.

## 소비 인터페이스와 전제

| Consumer 또는 caller | 사용할 수 있는 경계 | 반드시 지킬 의무 |
|---|---|---|
| Delivery | `SourceReader`, `SourceEnvironment`, `Identifier`, `StableSlug`, source domain error | 인증·인가 뒤 읽기 capability만 사용하고 HTTP/MCP rendering을 Source Catalog에 넘기지 않음 |
| Metadata | `SourceReader`, immutable profile/allowlist/budget, reader-policy interface | Checkout → connection 검사 → BEGIN → UTC/session 검사 → catalog 순서를 지킴 |
| Guarded Query | `SourceReader`, immutable profile/budget, reader-policy interface | Revision/SQL validation 전후의 정해진 순서와 query transaction/cancel/rollback을 지킴 |
| Control Plane | Manifest/budget validator, `ValidatedSourceManifest`, `SourceReader`, `SourceProjectionWriter` | Candidate를 격리 검증하고 endpoint identity와 apply/invalidation lifecycle을 우회하지 않음 |
| Runtime | Bootstrap paths와 production composition capability | `bootstrap|managed` authority를 process 단위로 하나만 선택하고 file/Control fallback을 만들지 않음 |
| Assurance | `SourceReader`, bootstrap configuration과 reader policy | Offline CLI에서만 concrete adapter를 조립하고 production/managed authority를 흉내 내지 않음 |

Production Source Catalog code는 Control DB table, HTTP/MCP type 또는 다른 module의 private
implementation을 직접 알지 않는다.

Plan-only `query-man-source-onboarding` workflow는 예외적으로 Control Plane의 public administration,
Delivery admin API와 Assurance acceptance **문서**를 읽어 human handoff를 만든다. 이는 Python import,
API 호출, concrete composition, credential access나 production mutation을 허용하는 dependency가 아니다.
현재 static request는 [source onboarding 안내](../../source-onboarding.md)와 checklist를 먼저 읽고,
managed runbook은 managed activation이 별도 승인된 뒤에만 읽는다.

## 불변조건

- Static first-launch inventory는 두 source뿐이며 모든 RLS manifest를 fail-closed한다.
- `source_id`별 Python branch를 추가하지 않고 차이는 manifest, budget, semantic overlay와 curated view로
  표현한다.
- Manifest, `SourceProfile`, public summary, error, log와 metric에 plaintext credential을 노출하지 않는다.
- System schema, unknown manifest/budget version과 extra field를 자동 보정하거나 하위 호환으로 추측하지 않는다.
- Published source/semantic graph에 mutable collection이나 caller의 mutable alias를 남기지 않는다.
- 같은 source ID의 host/port/database/user/TLS/environment 재지정 거부 lifecycle을 writer 우회로 깨지
  않는다.
- Ordinary consumer는 `SourceReader`로 좁히고 runtime projection writer는 하나만 둔다.
- Bootstrap과 managed authority를 merge하거나 한쪽 장애 때 다른 쪽으로 fallback하지 않는다.
- Reader compatibility 검사는 application SQL보다 먼저 끝나고 실제 connection 값이나 DB 오류를 외부에
  노출하지 않는다.
- Plan-only Skill이나 prompt를 authorization, validation, reader privilege와 resource enforcement로
  사용하지 않는다.

## 모듈 내부 변경

다음은 official interface와 manifest/reader policy, safety/lifecycle 결과가 같을 때 Source Catalog
안에서 독립적으로 바꿀 수 있다.

- 같은 acceptance/rejection을 만드는 manifest validator helper와 오류 처리 정리
- Registry lookup/list와 copy-on-write 내부 자료구조 개선
- 동일한 immutable graph를 만드는 parser/freeze helper 정리
- Public summary field/order를 바꾸지 않는 조회 성능 개선
- Existing schema 안의 non-authoritative fixture 정리
- 같은 policy 결과와 marker를 만드는 reader 검사 내부 정리

공통 `errors.py`와 cross-module test 같은 shared transition artifact는 coordinating agent가
single-writer와 consumer 검토 순서를 정한다. Source Catalog package leaf는 이 module owner가 쓰되
official interface consumer를 함께 검토한다.

## 사용자 승인이 필요한 경계 변경

다음 중 하나라도 의미가 달라지면 구현을 멈추고 현재 의미, 제안 의미, 실제 변경 범주,
provider/consumer, compatibility, migration/rollback, 보안 영향과 검증 계획을 사용자에게 제시한다.

| 변경 범주 | Source Catalog에서 멈춰야 하는 예 |
|---|---|
| Module interface | `SourceProfile`, validation type, `SourceReader`, `SourceProjectionWriter`, manifest/reader function의 shape, signature, result 또는 domain-error 의미 |
| Persisted/versioned format | Manifest v2, provenance, budget/observability schema와 version |
| Policy/compatibility identity | Schema/relation allowlist, tenant isolation, budget, PostgreSQL/encoding/session 허용 의미 |
| Safety/lifecycle invariant | RLS admission, credential/redaction, connection→transaction 검사 순서, source identity와 writer/apply ordering |
| Ownership/composition boundary | Bootstrap/managed authority, concrete registry/writer 조립, plan-only 문서 dependency와 hot reload 위치 |
| Protected operational procedure | Static source/database inventory 추가·교체, DDL/role/settings freeze, 배포/cutover/rollback/stop condition |

Source, budget과 overlay가 metadata revision에 참여하는 의미나 public summary/credential trust boundary를
바꾸면 Metadata, Guarded Query, Control Plane과 Delivery 영향을 함께 제시한다. Protected environment의
실제 action은 repository나 procedure 승인과 별개로 target, access, artifact, stop condition과
change-record 책임을 확인한 실행 승인이 필요하다.

## 검증

기본 Source Catalog gate:

```text
uv run pytest tests/test_registry.py tests/test_reader_policy.py tests/test_runtime_config.py
```

| 변경 영역 | 함께 실행할 직접 consumer gate |
|---|---|
| `SourceReader`, profile/immutability | `tests/test_catalog.py tests/test_query.py tests/test_http.py` |
| Manifest validator 또는 writer | `tests/test_source_admin.py tests/test_managed_mode.py` |
| Reader compatibility/session | `tests/test_catalog.py tests/test_query.py tests/test_source_admin.py` |
| Manifest, tenant, reader DB 경계 | `uv run pytest -m integration` |

Focused test는 root gate를 대신하지 않는다. 완료 전 coordinating agent가 `ruff`, `mypy`, full pytest와
해당 DB/integration gate를 실행한다.

## 집중해서 읽을 범위

Repository 전체를 먼저 읽지 말고 작업 종류에 따라 다음 범위만 확장한다.

| 작업 | 먼저 읽을 것 | 추가로 읽을 직접 경계 |
|---|---|---|
| Manifest, budget, semantic validation | 이 문서, `source_catalog/registry.py`, 관련 config와 `test_registry.py` | Metadata revision 의미 또는 Control candidate staging이 바뀔 때 해당 module/test |
| `SourceReader`/writer capability | `source_catalog/registry.py`, exact Protocol tests | Delivery/Metadata/Query/Runtime/Control 중 실제 소비자와 test |
| Reader connection/session | `source_catalog/reader_policy.py`, `test_reader_policy.py` | `metadata/catalog.py`, `guarded_query/query.py`와 직접 순서 test |
| Static source inventory | [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md), [onboarding 안내](../../source-onboarding.md), Runtime/Assurance acceptance | Protected procedure와 target environment는 별도 승인 뒤에만 |
| Managed validation/projection | Public validator/writer, [Control Plane](../control-plane/README.md), `test_source_admin.py`/`test_managed_mode.py` | Store/private apply 내부는 실제 lifecycle 변경 때만 |
| Plan-only onboarding Skill | Skill, current checklist와 `test_onboarding_skill.py` | Managed 문서는 activation이 이미 별도 승인된 경우에만 |
| RLS 또는 결과 범위 확대 | [future work](../../future-work.md)와 관련 research | 실제 요구·우선순위·정확한 변경 승인 전에는 구현 범위로 읽지 않음 |

Physical metadata response, MCP SDK 내부, query-pool private implementation과 Control DB storage는
Source Catalog가 제공하거나 소비하는 경계 의미가 실제로 바뀔 때만 읽는다.
