# Source Catalog Module

Status: Logical boundary; physical package split pending

## 목적

Source Catalog는 Query Man이 사용할 PostgreSQL source의 정의와 process-local runtime
projection을 소유한다. 다른 module은 `source_id`로 검증된 source profile을 조회하고 schema,
budget, semantic overlay와 tenant policy를 소비한다.

PostgreSQL physical catalog introspection과 metadata revision/context 생성은
[Metadata](../metadata/README.md)의 책임이다. 현재 첫 launch 범위는
[ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A`다.

## 소유 책임

- Source manifest v2와 budget profile의 strict validation
- `SourceProfile`, connection, allowlist, tenant isolation, provenance와 semantic overlay 정의
- Source-scoped credential environment naming과 host/port resolution 검증
- Optional observability target의 bounded manifest definition
- Runtime `SourceRegistry`의 read projection과 Control Plane용 projection writer capability
- Published source graph의 immutability와 public summary의 credential/internal-state 제거
- Metadata와 Guarded Query가 소비하는 공통 reader policy interface
- Plan-only source onboarding Skill의 owner/admin handoff와 secret/mutation 금지 경계

## 소유하지 않는 책임

- Source DB의 physical catalog, metadata cache와 revision
- SQL AST validation, query execution, result encoding, timeout, cancel과 rollback
- Control DB의 source generation, encrypted credential, transaction과 recovery
- Caller authentication/authorization와 HTTP/MCP/CLI rendering
- PostgreSQL reader role/grant 생성, source DB migration 또는 protected 배포 실행

## 현재 코드 위치

- [`registry.py`](../../../src/query_man/registry.py): manifest/budget parser, validator,
  `SourceReader`, `SourceProjectionWriter`와 concrete registry
- [`models.py`](../../../src/query_man/models.py): source/budget/semantic/provenance/observation types;
  Metadata type도 포함된 shared transition file
- [`reader_policy.py`](../../../src/query_man/reader_policy.py): Metadata와 Guarded Query가 공유하는
  reader connection/session policy; shared transition file
- [`config/sources`](../../../config/sources): `development-issues`, `market-voc` static launch inventory
- [`config/budget-profiles.yaml`](../../../config/budget-profiles.yaml): versioned resource tiers
- `config/onboarding/<source>.yaml`과 `config/onboarding/<source>-l2.yaml`: managed candidate staging
  fixture이며 static launch inventory가 아님
- [`query-man-source-onboarding`](../../../skills/query-man-source-onboarding/SKILL.md): production
  authority가 아닌 plan-only owner/administrator handoff workflow
- [`tests/test_registry.py`](../../../tests/test_registry.py),
  [`tests/test_reader_policy.py`](../../../tests/test_reader_policy.py),
  [`tests/test_runtime_config.py`](../../../tests/test_runtime_config.py): focused tests

Source-related type 이동과 물리 package 분리는 동작 변경에 섞지 않는 별도 mechanical
refactoring으로 수행한다.

## 제공 인터페이스와 소유 경계

이 절에서 `interface`는 다른 logical module에 공개한 Python shape와 호출 의미만 뜻한다.
Manifest format, launch policy, safety invariant와 protected operation은 별도 범주다.

### Source profile interfaces

`SourceProfile`은 process 내부의 검증된 source projection이다. 주요 소비 의미는 다음과 같다.

- `source_id`는 process 안에서 unique한 opaque ID다.
- Connection은 server-side에서 resolve하며 caller input으로 받지 않는다.
- `allowed_schemas`와 `allowed_relation_kinds`는 catalog/query의 최대 허용 범위다.
- `budget`은 Metadata와 Guarded Query가 함께 사용하는 source-wide hard limit다.
- `semantic_overlay`는 source별 Python branch를 대신한다.
- `minimum_quality_level`, `tenant_isolation`, `control_generation`과
  `control_state_version`은 publish/query와 freshness 판단의 입력이다.

Manifest v2의 optional `observability`는 대표 grain/physical relation과 이를 포함하는 1~16개의
distinct storage relation을 정의한다. 이는 관측 target일 뿐 query relation allowlist나 metadata
revision 재료가 아니며 public summary에 relation 이름을 추가하지 않는다.

### Published source interface immutability guarantee

`SourceProfile`과 도달 가능한 semantic graph는 recursively immutable하다. Sequence는 tuple,
nested mapping은 입력 alias를 복사한 read-only mapping이다. Profile에는 resolved plaintext reader
password가 있으므로 wire, persisted JSON, log 또는 metric에 serialize하지 않는다.

### `SourceReader` interface

Delivery, Metadata, Guarded Query와 Assurance application code는 read-only `SourceReader`를 소비한다.

```text
SourceReader:
  list() -> list[dict[str, str]]
  get(source_id) -> SourceProfile | None
  source_ids() -> frozenset[str]
```

`list()`는 `source_id` 순으로 정렬된 `source_id`, `name`, `description`만 반환한다.
`SourceNotFoundError`의 domain 의미는 Source Catalog가 소유하고 external error rendering은 Delivery가
소유한다.

### `SourceProjectionWriter` interface

Control Plane runtime projector/reloader만 write capability를 추가한 interface를 소비한다.

```text
SourceProjectionWriter extends SourceReader:
  upsert(source) -> None
  remove(source_id) -> None
```

Concrete `SourceRegistry`는 두 Protocol을 구조적으로 구현한다. Managed mode에서는 검증된 projection만
writer로 반영하고, bootstrap과 managed authority를 한 process에서 섞거나 fallback하지 않는다.
Projection 적용 순서, pool/cache invalidation과 rollback lifecycle은
[Control Plane](../control-plane/README.md)이 소유한다.

### Shared validation type interfaces

Delivery admin validation은 Source Catalog가 정의한 다음 type을 소비한다.

- `SourceEnvironment`: `production|staging|development|test`
- `Identifier`: PostgreSQL identifier pattern, 최대 63자
- `StableSlug`: lowercase alphanumeric-hyphen pattern, 최대 80자

허용값, pattern 또는 길이는 provider/consumer가 공유하는 interface 의미다.

### Reader connection interface (`LAUNCH-01-A`)

Source Catalog는 승인된 additive interface를 제공한다.

```python
READER_CLIENT_ENCODING: Final = "UTF8"

def require_reader_connection_policy(
    connection: AsyncConnection[Any],
) -> None: ...
```

Python shape와 호출 결과가 interface다. 다음 compatibility 조건과 호출 순서는 별도의
policy/safety invariant다.

- SQL을 실행하지 않고 connection info에서 PostgreSQL 18
  (`180000 <= server_version < 190000`), server/client `UTF8`, driver `utf-8` codec을 요구한다.
- Metadata와 Guarded Query는 pool checkout 직후, `BEGIN`과 application SQL 전에 호출하고 pool
  startup parameter로 `client_encoding=UTF8`을 요청한다.
- Deterministic mismatch는 실제 관측값을 포함하지 않는 고정 `ReaderSessionPolicyError` marker다.
  Caller는 해당 connection을 close/discard한다.
- Connection-info 자체의 driver/transport failure는 marker로 감싸지 않고 기존 transient 분류를
  유지한다.

기존 `require_reader_session_policy(...)` interface와 transaction-local UTC/budget/session 검사는
계속 Metadata와 Guarded Query가 함께 소비한다. `LAUNCH-01-A`는 그 기존 session 의미, role/database
default 또는 metadata revision 재료를 바꾸지 않는다.

### Current launch policy and deferred paths

- Static launch authority는 repository가 검토한 `development-issues`, `market-voc` 두 bootstrap
  manifest뿐이다.
- 모든 `tenant_isolation=rls` manifest는 bootstrap과 managed validation에서
  `RegistryConfigurationError`로 거부한다. `TenantIsolation` type, 기존 implementation과 historical
  Control row는 삭제하지 않는다.
- Injected registry, managed cold-start와 query 우회까지 포함한 RLS quarantine 결과는
  [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md#2-rls-quarantine)가 정의하며 각
  Runtime/Control Plane/Guarded Query module이 자기 경로를 강제한다.
- Managed mode 구현은 보존하지만 static first launch에는 Control DB, admin mutation, hot onboarding과
  reload가 참여하지 않는다.
- 새 source ID/database, manifest, budget/access policy의 추가·교체는 별도 inventory review와
  배포 승인이 필요하다.

[ADR 0020](../../decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 넓은 encoding과
[ADR 0024](../../decisions/0024-rls-policy-drift-attestation.md)의 RLS attestation은 parked research다.
현재 interface나 지원 동작으로 사용하지 않는다. 조사 당시 증거는
[RLS drift record](../../verification/2026-08-26-rls-policy-drift.md)로 보존한다.

## 소비 인터페이스와 전제

- Runtime이 선택한 mutually exclusive `bootstrap|managed` authority와 bootstrap source/budget path
- Control Plane이 검증 대상으로 전달하는 stored manifest와 복호화된 credential
- Accepted source, reader와 tenant policy identity
- Plan-only onboarding workflow가 읽는 Control Plane admin, Delivery admin transport와 Assurance
  acceptance 문서

Production Source Catalog code는 Control DB table, HTTP/MCP type 또는 다른 module의 private
implementation을 직접 알지 않는다. Plan-only `query-man-source-onboarding` workflow의 공개 문서
소비를 production dependency로 확대하지 않는다. 이는 Python import, API 호출, concrete
composition 또는 protected mutation 권한이 아니다.

## 불변조건

- Static first launch inventory는 `development-issues`, `market-voc`이며 모든 RLS manifest는
  fail-closed한다.
- `source_id`별 Python branch를 추가하지 않는다.
- Source manifest/public summary에 plaintext credential을 저장하거나 반환하지 않는다.
- System schema, manifest v2가 아닌 입력과 알 수 없는 budget version을 자동 변환하지 않는다.
- Published source/semantic graph에 mutable collection이나 external mutable alias를 남기지 않는다.
- `SourceRegistry.upsert`는 connection identity를 검증하지 않는다. 같은 `source_id`의
  host/port/database/user/TLS/environment 재지정 거부는 Control Plane과의 lifecycle invariant이며
  그 경로를 우회하지 않는다.
- Ordinary consumer는 `SourceReader`로 좁히고 runtime projection writer는 하나만 둔다.
- Reader compatibility verifier는 SQL을 실행하거나 실제 connection 값을 오류에 노출하지 않는다.

## 모듈 내부 변경

다음은 제공 interface와 별도 format/policy/invariant 의미를 보존할 때 독립적으로 바꿀 수 있다.

- Manifest validator 오류 처리와 중복 제거
- Registry lookup/list와 copy-on-write 내부 표현
- 같은 결과를 만드는 parser/helper 정리
- Existing schema 안의 non-authoritative test fixture 변경
- Public summary shape를 바꾸지 않는 정렬/조회 성능 개선

## 사용자 승인이 필요한 경계 변경

승인 요청은 실제 범주를 구분하고 관련 없는 항목을 하나의 “interface 변경”으로 묶지 않는다.

- Module interface: `SourceProfile`, shared validation type, `SourceReader`,
  `SourceProjectionWriter`, reader verifier의 shape 또는 호출 의미
- Persisted/versioned format: manifest v2, provenance, budget/observability schema와 version
- Policy/compatibility identity: schema/relation allowlist, tenant isolation, budget 또는 PG/encoding
  reader compatibility의 의미
- Safety/lifecycle invariant: RLS admission, credential/redaction, generation identity와 writer ordering
- Ownership/composition boundary: bootstrap/managed authority, writer/composition 권한과 hot reload
- Protected operational procedure: static source/database inventory 추가·교체, DDL/role/settings freeze와
  실제 배포/cutover/rollback

Source/budget/overlay가 metadata revision에 참여하는 의미, public summary나 credential trust boundary를
바꾸면 Metadata, Guarded Query, Control Plane과 Delivery 영향을 함께 제시한다. Protected environment
실행은 repository/procedure 승인과 별도의 대상·접근·중단 조건·change-record 승인이 필요하다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_reader_policy.py tests/test_runtime_config.py
```

Reader policy 또는 registry projection을 바꾸면 직접 consumer gate도 실행한다.

```text
uv run pytest tests/test_catalog.py tests/test_query.py tests/test_source_admin.py \
  tests/test_managed_mode.py
```

Manifest, tenant 또는 reader DB 경계를 바꾸면 `uv run pytest -m integration`을 추가한다. 완료 전
root `AGENTS.md`의 전체 gate는 coordinating agent가 실행한다.

## 집중해서 읽을 범위

1. 이 문서와 [module index](../README.md)
2. 변경 대상 `registry.py`, source-related `models.py`, `reader_policy.py`
3. 위 focused tests와 관련 config fixture
4. [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)와 변경에 직접 관련된 accepted ADR
5. 변경이 닿는 provider/consumer interface, format, policy, lifecycle 또는 operational 문서와 test

Physical metadata response, MCP SDK 내부와 query pool private implementation은 Source Catalog가 제공하거나
소비하는 경계 의미가 바뀌지 않는 한 읽을 필요가 없다.
