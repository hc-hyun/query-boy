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
- Optional manifest v2 observability definition과 bounded physical resource targets
- System schema 거부, overlay referential integrity와 source identity 검증
- Runtime `SourceRegistry`의 논리적 read projection
- 논리적으로 Control Plane만 사용하는 registry upsert/remove projection capability
- Public source summary에서 credential과 internal state를 제거하는 규칙
- Plan-only source onboarding Skill의 trigger, owner/admin handoff와 secret/mutation 금지 경계

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
- `config/onboarding/<source>.yaml`과 `config/onboarding/<source>-l2.yaml`: Control Plane
  candidate staging이 소비하는 fixture source/semantic input; 같은 directory의
  `<source>-verified-query.yaml`은 Assurance 소유
- [`query-man-source-onboarding`](../../../skills/query-man-source-onboarding/SKILL.md): production
  authority가 아닌 plan-only DB-owner/administrator handoff workflow
- [`tests/test_registry.py`](../../../tests/test_registry.py),
  [`tests/test_runtime_config.py`](../../../tests/test_runtime_config.py): focused tests

`models.py`에는 Metadata type도 함께 있으므로 현재는 type별 소유권을 구분한다. Source 관련
type 이동은 동작 변경과 섞지 않는 별도 mechanical refactoring으로 수행한다.

`registry.py`는 read-only `SourceReader`와 이를 확장하는 `SourceProjectionWriter` Protocol을
제공한다. Concrete `SourceRegistry`는 두 Protocol을 구조적으로 구현하고 기존 다섯 method와
`load`를 그대로 제공한다. 이는 wrapper나 runtime sandbox가 아니라 일반 consumer의 annotation을
좁혀 accidental mutation을 mypy와 review에서 찾는 개발 경계다. 승인 범위와 compatibility는
[결정 가이드 D2](../../module-contract-decision-guide.md#d2-source-조회와-수정-capability)와
[ADR 0018](../../decisions/0018-module-ownership-and-contract-governance.md)에 기록한다.

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

### Resource observation definition contract (`CTRL-07A`, implemented)

Manifest v2의 optional `observability`는 `representative_records.grain`, 하나의
`physical_relation`과 그 relation을 포함하는 1~16개의 distinct `storage_relations`를 가진다. Relation은
system schema가 아닌 같은 database의 ordinary table 또는 materialized view여야 한다. 이는 DB
owner가 승인한 관측 target일 뿐 `allowed_schemas`, `allowed_relation_kinds`나 query metadata에 추가되지
않으며 public source summary에도 relation 이름을 노출하지 않는다.

Validated `SourceProfile`은 이 definition을 immutable tuple/value로 제공한다. Definition 변경은
source generation을 만들지만 `create_metadata_revision` 재료는 아니다. Missing object는 관측 미구성
의미이고 기존 v2 manifest와 호환된다. Parser는 SQL, predicate, counter expression, provider account와
arbitrary method를 받지 않는다.

`SourceProfile`에는 resolved plaintext reader password가 들어 있으므로 process 내부의 sensitive
object다. Profile 자체를 wire response, persisted JSON, log 또는 metric에 serialize하지 않는다.
다른 module은 필요한 field만 memory 안에서 소비한다.

### Published source immutability contract

`SourceReader.get()`이 반환하는 `SourceProfile`과 도달 가능한 semantic graph는 재귀적으로
immutable하다. Public sequence는 실제 tuple이고 `column_aliases`, `value_hints`와 join column
pair는 원본 dict를 복사한 read-only mapping이며 mapping 안의 sequence도 tuple이다. Dataclass
constructor와 YAML provider는 입력 collection을 복사해 published graph 밖의 mutable alias로
내용을 바꿀 수 없게 한다. 따라서 registry는 같은 profile identity를 여러 reader에 안전하게
공유할 수 있고 consumer는 in-place mutation 대신 새 검증 profile을 writer boundary에 전달한다.

이 Python runtime representation은 Source manifest와 Control Plane의 stored manifest를 바꾸지
않는다. YAML/JSON sequence와 object는 계속 array/list와 object/dict로 decode·serialize하며
`SourceProfile` 자체는 여전히 wire나 persistence에 내보내지 않는다.

### Shared source validation type contract

Delivery의 admin path/query wire validation은 현재 Source Catalog가 정의한 다음 type을 소비한다.

- `SourceEnvironment`: `production|staging|development|test`
- `Identifier`: PostgreSQL identifier pattern과 최대 63자
- `StableSlug`: lowercase alphanumeric hyphen slug pattern과 최대 80자

이 type은 manifest validation과 admin wire acceptance가 공유하는 cross-module 계약이다. Pattern,
허용값 또는 길이를 바꾸면 Source Catalog와 Delivery 영향을 함께 검토한다.

### Source read contract

Delivery, Metadata, Guarded Query와 Assurance application code는 다음 `SourceReader`만 소비한다.

```text
SourceReader:
  list() -> list[dict[str, str]]
  get(source_id) -> SourceProfile | None
  source_ids() -> frozenset[str]
```

`list()`는 `source_id` 순으로 정렬하며 위 세 field 외 connection, credential, provenance와
internal control state를 반환하지 않는다. 이 소비자들은 registry를 직접 변경하지 않는다.

### Source projection write contract

Control Plane의 runtime projector/reloader는 read contract를 확장한 다음
`SourceProjectionWriter`를 소비한다.

```text
SourceProjectionWriter extends SourceReader:
  upsert(source) -> None
  remove(source_id) -> None
```

Managed mode에서는 이 projector만 검증된 state를 `upsert/remove`한다.
Bootstrap mode는 process 시작 시 filesystem manifest로 initial registry를 만들며 두 authority를
한 process에서 합치지 않는다. `state_version`은
증가하지만 rollback은 과거 generation을 다시 활성화할 수 있으므로 generation 자체를 monotonic으로
가정하지 않는다. 적용 순서와 pool/cache invalidation은
[Control Plane](../control-plane/README.md)의 계약이다.

### Reader-session safety contract

Metadata catalog와 Guarded Query executor는 같은 reader database/user, read-only isolation,
role restriction, schema privilege, resource setting, search path, RLS와 tenant context 검사를
사용한다. 한쪽만 완화하거나 별도 구현으로 복제하지 않는다.
두 경로 모두 `BEGIN ... REPEATABLE READ READ ONLY` 다음 첫 settings statement로
transaction-local `TimeZone=UTC`를 설정하고, 공통 probe가 UTC를 확인한 뒤에만 catalog 또는
planning/query를 실행한다. Role/database default는 source 소유 값 그대로 두며 success,
rollback, timeout과 cancel 뒤 pool 재사용에서 그 default가 복원돼야 한다.

현재 공통 policy는 `DateStyle`, `IntervalStyle`, `extra_float_digits`,
`standard_conforming_strings`, `transform_null_equals`, `array_nulls`, `client_encoding`과
`timezone_abbreviations`를 설정·검사하지 않고 server encoding과 effective database/column collation을
snapshot/revision material로 admission하지 않는다. 실제
PostgreSQL 18 characterization에서 이 default가 ambiguous date, backslash string, NULL 비교와 NULL
array literal의 SQL 의미, interval availability, finite-float hash, timezone abbreviation instant와
SQL text/binary identity를 흔들고 collation-only DDL은 same-revision result를 바꾸는 것이 확인됐다.
Direct `bytea` loader/Base64는 `bytea_output=hex|escape`에서 같지만 허용된 `bytea::text` cast는
setting별 text/hash가 달랐고, `default_text_search_config`도 hidden curated view의 같은 SQL 의미를
바꿨다.
[Proposed ADR 0020](../../decisions/0020-lossless-interval-and-json-numeric-encoding.md)의
`ENC-01-A|B|C`가 정확히 승인되기 전에는 shared `reader_policy.py`나 metadata revision material을
변경하지 않는다.

Exact A 제안에서 Source Catalog의 추가 역할은 Catalog/Query pool startup의
`client_encoding=UTF8`, UTC-first transaction-local setting과 PostgreSQL-18/UTF8 reader admission으로
한정된다. Catalog semantics SQL, fingerprint material/hash와 snapshot codec/revision은 Metadata
소유이며 Source Catalog에 복제하지 않는다. 이 경계도 exact A 승인 전에는 현재
contract가 아니다.

별도 [RLS policy drift security finding](../../verification/2026-08-26-rls-policy-drift.md)은 공통
session probe가 정상이어도 hidden base table policy를 `USING (true)`로 바꾸거나 RLS를 disable하면
같은 snapshot/revision 아래 cross-tenant row가 성공함을 재현했다. 이는 accepted contract가 아니라
열린 보안 결함이다. `RLS-01` exact dependency/policy admission 계약 승인 전 임의의 manifest field나
reader check를 추가하지 않으며, production RLS source가 안전하다고 주장하지 않는다.

## 소비 계약

- Runtime configuration이 선택한 mutually exclusive `bootstrap|managed` authority, budget file과
  bootstrap mode의 source directory/environment
- Control Plane이 복호화하고 검증 대상으로 전달하는 credential과 stored manifest
- Accepted source/reader/tenant ADR의 identity와 privilege 정책
- Plan-only `query-man-source-onboarding` workflow에 한해 Control Plane public administration,
  Delivery public admin transport와 Assurance onboarding acceptance 문서를 읽어 human handoff를
  만든다. Python import, API 호출, concrete composition과 production mutation은 하지 않는다.

Production Source Catalog code는 Control DB table이나 HTTP/MCP type을 직접 알지 않는다. 위
plan-only 공개 문서 소비를 production dependency로 확대하지 않는다.

## 불변조건

- `source_id`별 Python branch를 추가하지 않는다.
- Source manifest와 public summary에 plaintext credential을 저장하거나 반환하지 않는다.
- System schema, manifest v2가 아닌 입력과 알 수 없는 budget version은 자동 변환 없이
  fail-closed한다.
- `SourceRegistry.upsert` 자체는 generation 사이 connection identity를 검증하지 않는다. 같은
  `source_id`의 host/port/database/user/TLS/environment 재지정 거부는 Control Plane과의 필수 cross-module
  invariant이며 그 경로를 우회해 registry를 갱신하지 않는다.
- Budget, overlay와 tenant policy 변경이 metadata revision에 미치는 영향을 숨기지 않는다.
- Published source/semantic graph에 mutable collection이나 외부 mutable alias를 남기지 않는다.
- Runtime projection writer는 하나다. Ordinary reader, isolated Control staging과 Assurance
  `assurance_cli.py`의 application reference는 `SourceReader`로 좁히며 `upsert/remove`를 호출하지
  않는다. Runtime composition은 같은 concrete instance를 reader consumer와 Control writer에
  capability별로 주입할 수 있다.

## 모듈 내부 변경

다음은 제공 계약과 serialized shape를 보존할 때 module 내부에서 독립적으로 변경할 수 있다.

- Manifest validator의 오류 처리와 중복 제거
- Registry lookup/list 구현과 copy-on-write 내부 표현
- 같은 결과를 만드는 parser/helper 정리
- 기존 schema 안에서 source configuration fixture 추가
- Public summary shape를 바꾸지 않는 정렬/조회 성능 개선

## 사용자 승인이 필요한 계약 변경

- Source manifest v2, provenance, budget profile 또는 access-related source field의 shape/version 변경
- Delivery가 소비하는 `SourceEnvironment`, `Identifier`, `StableSlug`의 허용값, pattern 또는 길이 변경
- `SourceProfile` 필드 의미나 public source summary 변경
- Published source/semantic collection의 tuple/read-only mapping 표현이나 alias-copy 보장 변경
- Allowed schema/relation kind, tenant isolation 또는 reader policy의 완화/확대
- Metadata revision에 참여하는 source/budget/overlay 의미 변경
- `SourceReader` 또는 `SourceProjectionWriter` method, argument, return shape나 capability 관계 변경
- Registry writer 권한이나 hot-reload state ordering 변경
- 같은 source ID의 connection/environment identity 재지정 허용
- Credential resolution, redaction 또는 storage trust boundary 변경

승인 요청은 영향받는 Metadata, Guarded Query, Control Plane, Delivery와 migration/compatibility를
함께 설명한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_runtime_config.py tests/test_metadata.py \
  tests/test_query.py tests/test_http.py tests/test_source_admin.py tests/test_quality.py \
  tests/test_verified.py tests/test_managed_mode.py tests/test_onboarding_skill.py
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
4. [ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md),
   [ADR 0005](../../decisions/0005-initial-query-budgets.md),
   [ADR 0012](../../decisions/0012-control-plane-source-revisions.md),
   [ADR 0014](../../decisions/0014-trusted-rls-tenant-context.md),
   [ADR 0016](../../decisions/0016-centralized-source-management-plane.md)과
   [ADR 0017](../../decisions/0017-shared-source-access-and-resource-tier.md) 중 변경과 직접 관련된 결정
5. 변경이 닿는 제공/소비 계약 문서

Physical metadata response, MCP SDK 내부와 query pool 구현은 계약이 바뀌지 않는 한 읽을 필요가
없다.
