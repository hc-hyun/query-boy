# Source Catalog Module

Status: Logical boundary; physical package split pending

## 목적

Source Catalog는 Query Man이 사용할 PostgreSQL source의 정의와 process-local runtime
projection을 소유한다. 다른 module은 `source_id`로 source를 조회하고, 검증된 profile에 들어
있는 schema, budget, semantic overlay와 tenant policy를 소비한다.

이 module은 PostgreSQL physical catalog를 읽는 module이 아니다. `pg_catalog` introspection과
metadata revision/context 생성은 [Metadata](../metadata/README.md)가 담당한다.

## 소유 책임

- Source manifest v2와 budget profile의 strict schema 및 이전 version의 fail-closed cutover
- `SourceProfile`, resolved connection, allowed schema/relation kind, tenant isolation과 provenance
- Grain, measure, join, business term과 question rule을 포함한 semantic overlay definition
- Source-scoped credential environment naming과 host/port resolution 검증
- Owner, environment와 source DB migration reference의 bounded provenance definition
- System schema 거부, overlay referential integrity와 source identity 검증
- Runtime `SourceRegistry`의 read projection
- Control Plane만 사용하는 registry upsert/remove projection capability
- Public source summary에서 credential과 internal state를 제거하는 규칙

## 소유하지 않는 책임

- Source DB의 physical catalog introspection과 metadata cache/revision
- SQL AST validation, execution, timeout, result limit과 cancellation
- Source generation, encrypted credential, Control DB transaction과 rollback
- Caller authentication/authorization와 HTTP/MCP serialization
- PostgreSQL reader role/grant 자체의 생성이나 source DB migration

## 현재 코드 위치

- [`registry.py`](../../../src/query_man/registry.py): manifest/budget parser, validator와 runtime registry
- [`models.py`](../../../src/query_man/models.py): budget, connection, semantic와 source profile types
- [`reader_policy.py`](../../../src/query_man/reader_policy.py): Metadata와 Guarded Query가 공유하는
  reader-session safety contract
- [`config/sources`](../../../config/sources): local/CI bootstrap source definitions; managed authority가 아님
- [`config/budget-profiles.yaml`](../../../config/budget-profiles.yaml): versioned resource tier definitions
- [`tests/test_registry.py`](../../../tests/test_registry.py),
  [`tests/test_runtime_config.py`](../../../tests/test_runtime_config.py): focused tests

`models.py`에는 Metadata type도 함께 있으므로 현재는 type별 소유권을 구분한다. Source 관련
type 이동은 동작 변경과 섞지 않는 별도 mechanical refactoring으로 수행한다.

## 제공 계약

### Source definition contract

소비 module은 source profile의 다음 의미를 신뢰할 수 있다.

- `source_id`는 opaque하고 process 안에서 unique하다.
- Connection은 server-side에서 resolve되며 client input으로 받지 않는다.
- `allowed_schemas`와 `allowed_relation_kinds`는 physical catalog/query의 최대 허용 범위다.
- `budget`은 Metadata와 Guarded Query가 함께 사용하는 source-wide hard limit다.
- `semantic_overlay`는 source별 Python branch를 대신하는 versioned business semantics다.
- `provenance`는 bounded owner, `production|staging|development|test` environment와 외부 source DB
  migration reference다. 권한 principal이나 migration artifact 검증 결과가 아니다.
- `minimum_quality_level`과 `tenant_isolation`은 publish/query gate의 입력이다.
- `control_generation`과 `control_state_version`은 Control DB projection의 freshness/CAS identity다.

`SourceProfile`에는 resolved plaintext reader password가 들어 있으므로 process 내부의 sensitive
object다. Profile 자체를 wire response, persisted JSON, log 또는 metric에 serialize하지 않는다.
다른 module은 필요한 field만 memory 안에서 소비한다.

### Source read contract

Delivery, Metadata, Guarded Query와 Assurance는 source 조회 capability만 소비한다.

```text
list() -> [{"source_id": str, "name": str, "description": str}]
get(source_id) -> current SourceProfile | not found
source_ids() -> immutable set-like snapshot
```

`list()`는 `source_id` 순으로 정렬하며 위 세 field 외 connection, credential, provenance와
internal control state를 반환하지 않는다. 이 소비자들은 registry를 직접 변경하지 않는다.

### Source projection write contract

Managed mode에서는 Control Plane의 runtime projector만 검증된 state를 `upsert/remove`할 수 있다.
Bootstrap mode는 process 시작 시 filesystem manifest로 initial registry를 만들며 두 authority를
한 process에서 합치지 않는다. `state_version`은
증가하지만 rollback은 과거 generation을 다시 활성화할 수 있으므로 generation 자체를 monotonic으로
가정하지 않는다. 적용 순서와 pool/cache invalidation은
[Control Plane](../control-plane/README.md)의 계약이다.

### Reader-session safety contract

Metadata catalog와 Guarded Query executor는 같은 reader database/user, read-only isolation,
role restriction, schema privilege, resource setting, search path, RLS와 tenant context 검사를
사용한다. 한쪽만 완화하거나 별도 구현으로 복제하지 않는다.

## 소비 계약

- Runtime configuration이 선택한 mutually exclusive `bootstrap|managed` authority, budget file과
  bootstrap mode의 source directory/environment
- Control Plane이 복호화하고 검증 대상으로 전달하는 credential과 stored manifest
- Accepted source/reader/tenant ADR의 identity와 privilege 정책

Source Catalog는 Control DB table이나 HTTP/MCP type을 직접 알지 않는다.

## 불변조건

- `source_id`별 Python branch를 추가하지 않는다.
- Source manifest와 public summary에 plaintext credential을 저장하거나 반환하지 않는다.
- System schema, manifest v2가 아닌 입력과 알 수 없는 budget version은 자동 변환 없이
  fail-closed한다.
- `SourceRegistry.upsert` 자체는 generation 사이 connection identity를 검증하지 않는다. 같은
  `source_id`의 host/port/database/user/TLS/environment 재지정 거부는 Control Plane과의 필수 cross-module
  invariant이며 그 경로를 우회해 registry를 갱신하지 않는다.
- Budget, overlay와 tenant policy 변경이 metadata revision에 미치는 영향을 숨기지 않는다.
- Runtime projection writer는 하나이며 ordinary reader가 mutation capability를 얻지 않는다.

## 모듈 내부 변경

다음은 제공 계약과 serialized shape를 보존할 때 module 내부에서 독립적으로 변경할 수 있다.

- Manifest validator의 오류 처리와 중복 제거
- Registry lookup/list 구현과 copy-on-write 내부 표현
- 같은 결과를 만드는 parser/helper 정리
- 기존 schema 안에서 source configuration fixture 추가
- Public summary shape를 바꾸지 않는 정렬/조회 성능 개선

## 사용자 승인이 필요한 계약 변경

- Source manifest v2, provenance, budget profile 또는 access-related source field의 shape/version 변경
- `SourceProfile` 필드 의미나 public source summary 변경
- Allowed schema/relation kind, tenant isolation 또는 reader policy의 완화/확대
- Metadata revision에 참여하는 source/budget/overlay 의미 변경
- Registry writer 권한이나 hot-reload state ordering 변경
- 같은 source ID의 connection/environment identity 재지정 허용
- Credential resolution, redaction 또는 storage trust boundary 변경

승인 요청은 영향받는 Metadata, Guarded Query, Control Plane, Delivery와 migration/compatibility를
함께 설명한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_runtime_config.py
```

Manifest, budget, tenant 또는 reader DB 경계를 바꾸면 관련 metadata/query tests와 reader
policy를 검증하는 `uv run pytest -m integration`을 추가한다. Registry projection 또는 connection
identity 경계를 바꾸면 `uv run pytest tests/test_source_admin.py`도 실행한다. 완료 전 root
`AGENTS.md`의 전체 gate도 실행한다.

## 집중해서 읽을 범위

Source Catalog 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상인 `registry.py`, source-related `models.py`, `reader_policy.py`
3. 위 focused tests와 관련 config fixture
4. Source identity, budget, reader 또는 tenant 관련 ADR
5. 변경이 닿는 제공/소비 계약 문서

Physical metadata response, MCP SDK 내부와 query pool 구현은 계약이 바뀌지 않는 한 읽을 필요가
없다.
