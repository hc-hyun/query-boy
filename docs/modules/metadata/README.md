# Metadata Module

Status: Logical boundary; physical package split pending

## 목적

Metadata는 reader가 실제로 볼 수 있는 PostgreSQL physical catalog와 versioned semantic overlay를
검증된 persisted immutable metadata revision으로 만들고, 사용자 질문에 필요한 bounded context를
제공한다.

Metadata는 SQL 실행기가 아니다. 질문에 어떤 relation, column, grain, join과 business rule을
사용해야 하는지 설명하고 Guarded Query가 확인할 published revision을 제공한다.

## 소유 책임

- Reader privilege 안에서의 `pg_catalog` relation/column/key/index/comment introspection
- Catalog snapshot shape, validation, serialization contract와 compatibility 확인
- Source definition과 physical snapshot으로부터 `metadata_revision` 계산
- Metadata refresh coalescing, cache TTL, stale window, retry와 source epoch
- Persisted immutable revision publish/active/pin을 소비하는 MetadataStore port
- L0/L1/L2 publish quality gate
- Revision-scoped relevance index와 question-scoped relation/column disclosure
- Grain, measure, join, business term, ambiguity와 answerability response projection
- Metadata response byte limit과 untrusted comment sanitization
- DB-owner-declared physical relation의 bounded catalog estimate/size observation

## 소유하지 않는 책임

- Source manifest/budget schema와 runtime registry mutation
- SQL AST/function/operator validation과 query execution
- Control DB schema, transaction, advisory lock와 source generation transition
- HTTP/MCP input schema, authentication과 public error serialization
- Verified result의 live SQL 실행과 canonical hash ownership
- Source DB의 view, reader role와 grant migration

## 현재 코드 위치

- [`catalog.py`](../../../src/query_man/catalog.py): PostgreSQL physical introspection adapter
- [`metadata.py`](../../../src/query_man/metadata.py): lifecycle, cache, validation과 context projection
- [`relevance.py`](../../../src/query_man/relevance.py): revision-scoped retrieval index
- [`revision.py`](../../../src/query_man/revision.py): metadata revision digest
- [`quality_level.py`](../../../src/query_man/quality_level.py): runtime publish quality gate
- [`errors.py`](../../../src/query_man/errors.py): metadata unavailable/revision mismatch 의미;
  public rendering은 Delivery 계약
- [`models.py`](../../../src/query_man/models.py): catalog snapshot/prepared metadata type, 작은
  `CatalogProvider`와 Runtime 전용 `RuntimeCatalogProvider`
- [`metadata_store.py`](../../../src/query_man/metadata_store.py): MetadataStore port, persisted snapshot codec와
  PostgreSQL implementation이 함께 있는 transition hot spot
- Focused tests: [`test_catalog.py`](../../../tests/test_catalog.py),
  [`test_metadata.py`](../../../tests/test_metadata.py),
  [`test_relevance.py`](../../../tests/test_relevance.py),
  [`test_revision.py`](../../../tests/test_revision.py),
  [`test_quality_level.py`](../../../tests/test_quality_level.py),
  [`test_metadata_store.py`](../../../tests/test_metadata_store.py),
  [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py)

`PostgresMetadataStore`의 Control DB transaction과 pool은
[Control Plane](../control-plane/README.md)이 소유한다. Metadata는 store capability와 snapshot
codec/compatibility contract만 소유한다.

`MetadataService`와 Control Plane candidate staging은 `load/close`만 제공하는 작은
`CatalogProvider`를 계속 소비한다. Runtime은 이를 확장한 `RuntimeCatalogProvider`를 요구하고
concrete `PostgresCatalog`는 두 Protocol을 구조적으로 구현한다.
Assurance의 offline workflow는 `assurance_cli.py`에서만 concrete catalog와 `MetadataService`를
조립하며 quality/verified core는 이미 조립된 application service만 소비한다.

Control DB의 persisted snapshot/revision history와 process 안의 published Python graph는 서로 다른
경계에서 모두 immutable하다. `CatalogColumn`, key/index, `CatalogRelation`, `CatalogSnapshot`과
`PreparedMetadata`의 모든 public sequence는 tuple이고 dataclass는 frozen이다. Catalog row 조립과
persistence decode는 private mutable builder를 사용할 수 있지만 cache/provider boundary 전에
새 immutable graph로 freeze하고 입력 collection alias를 남기지 않는다. 이 보장은
[결정 가이드의 D3-A](../../module-contract-decision-guide.md#d3-공유-data의-deep-immutability)와
`MOD-07`의 구현 결과다.

## 제공 계약

### Published metadata contract

Guarded Query, Control Plane과 Assurance는 다음을 신뢰한다.

```text
get_published(source_id) -> snapshot + exact metadata_revision
```

- Snapshot은 current source definition과 compatible해야 한다.
- Snapshot과 `PreparedMetadata`의 도달 가능한 public graph는 재귀적으로 immutable하며 provider는
  mutable builder나 decoder 입력의 alias를 반환하지 않는다.
- Revision mismatch, schema drift, reader policy drift와 stale upper bound 초과는 fail-closed한다.
- Published snapshot relation은 Guarded Query relation allowlist의 최대 범위다.
- Row estimate는 fresh best-effort hint이며 persisted revision과 correctness 판단의 재료가 아니다.

### Metadata context contract

Delivery는 다음 application result를 transport와 무관하게 소비한다.

```text
source_id, source_name, source_description, question
metadata_revision, sql_policy_revision, snapshot_status, quality_level
sql_capabilities, answerability, relations, joins, business_terms
composition_hints, ambiguities, truncated
```

`snapshot_status`는 `fresh|stale`, `quality_level`은 `L0|L1|L2`다. `sql_capabilities`는
`functions`, `cast_types`, `unqualified_cast_types`를 가진다. `answerability`는 `status`,
`reason_codes`, `messages`, `missing_concepts`, `options`를 가진다.

Relation projection은 `rank`, `name`, `sql_name`, `kind`, `role`, `description`, `database_comment`,
`grain`, `default_time_column`, `selection_reasons`, `measures`, `primary_key`, `foreign_keys`, `indexes`,
`indexes_truncated`, `column_count`, `returned_column_count`, `columns_truncated`, `columns`와 fresh
catalog에서만 가능한 optional `estimated_rows`를 제공한다. Column은 `name`, `sql_name`, `ordinal`,
`data_type`, `nullable`, `description`, `aliases`, `value_hints`, `semantic_roles`를 제공한다.
Approved join, business term, composition hint와 ambiguity의 현재 field/ordering도 contract다.

이 exact top-level/nested shape, compact UTF-8 JSON byte accounting과 revision format은 HTTP/MCP가
공동으로 사용하는 계약이다. Context에서 column을 생략하는 것은 SQL deny rule이 아니며, query
allowlist는 published snapshot relation과 Guarded Query policy가 결정한다.
Python snapshot tuple/read-only mapping은 projection 경계에서 명시적으로 list/dict로 바꾸므로
HTTP/MCP application result와 JSON은 기존 array/object shape를 유지한다.

### Metadata revision contract

Revision은 다음 current source/catalog material의 canonical JSON SHA-256(`sha256:` prefix)이다.

- `source_id`, allowed schemas/relation kinds와 non-default tenant isolation
- 전체 execution budget과 semantic overlay
- Relation schema/name/kind/comment/definition hash/security-invoker
- Ordered primary/foreign key와 index definition
- Column name/ordinal/type/nullability/comment

Connection/credential, source provenance(owner/environment/database migration reference), control
generation/state version, `minimum_quality_level`, `estimated_rows`, freshness/cache state는
revision에서 제외한다. Provenance-only publish는 새 source generation을 만들 수 있지만 query
metadata revision은 바꾸지 않는다. 포함·제외 재료, list canonical ordering과 digest format은
persisted snapshot 및 rolling replica compatibility contract다.
Canonicalizer는 list와 tuple, dict와 immutable mapping을 같은 canonical array/object로
정규화하며 representation만으로 digest를 바꾸지 않는다.

### Metadata lifecycle contract

- Fresh candidate를 validate하고 source definition과 함께 revision을 계산한다.
- Store가 있으면 append-only persisted snapshot을 publish하고 committed active value만 cache한다.
- Rollback pin, resume와 stale activation provenance를 보존한다.
- Source generation 교체 시 epoch와 current profile을 함께 확인하여 지연 refresh를 거부한다.
- Transient catalog failure는 bounded stale window 안에서만 마지막 정상 revision을 제공한다.
- Reader-session policy drift와 schema/overlay validation failure는 stale fallback 없이 fail-closed한다.
- Rollback으로 다른 revision이 pin되어 candidate publish가 거부된 경우에만 bounded cached revision을
  별도 stale path로 제공할 수 있다.
- Fresh cache hit는 PostgreSQL reader policy를 다시 조회하지 않으며 drift는 다음 refresh에서
  검출한다. Cache hit를 live privilege probe로 해석하지 않는다.

### Resource observation provider (`CTRL-07A`, implemented)

Runtime-only catalog capability는 Source Catalog가 검증한 optional observability definition의 exact
physical relation만 조회한다. 대상 전체를 열거하지 않고 system schema와 unsupported relkind를
거부하며 한 representative relation과 최대 16개의 distinct storage relation으로 제한한다. Existing
`PostgresCatalog`의 max-one reader pool, read-only transaction과 metadata statement timeout을
재사용하므로 monitoring role이나 별도 connection budget을 추가하지 않는다.

Provider는 representative `pg_class.reltuples`가 non-negative일 때 rounded rows와 configured
relations의 `pg_table_size`, `pg_indexes_size`, `pg_total_relation_size` 합계만 반환한다. Relation 이름,
OID, catalog row와 SQL은 Control payload에 넣지 않는다. Ordinary view `COUNT(*)`, caller SQL,
`EXPLAIN ANALYZE`와 provider billing/statistics는 이 capability에 없다. `reltuples`가 unavailable이면
record sample만 생략하고 storage failure는 전체 resource attempt를 실패시킨다. Observation data는
`CatalogSnapshot`, metadata cache/persistence와 metadata revision에 들어가지 않는다.

### Runtime observation signal (`CTRL-06`)

Metadata는 public context나 persisted snapshot shape를 바꾸지 않고 Runtime operations의 internal
reporting sink에 실제 replica-local cache 상태만 알린다.

- Fresh publish, persisted restore 또는 pinned active value를 cache에 적용한 뒤 exact
  `metadata_revision`을 기록한다.
- Source cache invalidate와 disabled source apply는 applied metadata revision을 제거한다.
- Probe/cache 실패는 `METADATA_PROBE_FAILED`와 unavailable health를 기록하되 credential, source
  definition, question, SQL, raw exception 또는 Runtime timestamp를 observation에 넣지 않는다.
- Control Plane candidate staging 등 `suppress_source_health_updates()`가 활성화된 동안은 health와
  metadata observation을 모두 억제한다. Staging cache가 production replica의 applied revision을
  덮어쓰지 않는다.
- 이 signal은 best-effort latest observation이며 metadata cache, published revision, readiness 또는
  query correctness의 authority가 아니다. Existing context response, revision digest, cache TTL/stale
  window와 MetadataStore transaction 의미는 그대로다.

### Catalog provider capability contract

```text
CatalogProvider:
  async load(source) -> CatalogSnapshot
  async close() -> None

RuntimeCatalogProvider extends CatalogProvider:
  async invalidate(source_id: str) -> None
  async observe_resources(source) -> ResourceObservation
  async close() -> None  # inherited
```

Runtime composite는 source generation 교체 때 pool을 반드시 invalidate하기 위한 조립 계약이다.
`MetadataService`의 `CatalogProvider` type contract는 invalidate capability를 요구하거나 노출하지
않는다. Provider의 `load` 결과는 위의 recursively immutable `CatalogSnapshot`이어야 한다. Runtime이
같은 concrete adapter를 주입하므로 이는 runtime sandbox가 아니다.

### MetadataStore port

```text
get_active(source)
get_revision(source, revision)
publish(source, candidate) -> committed active value
activate(source, revision) -> pinned active value
unpin(source)
close()
```

`publish`는 active revision이 pin되어 있으면 candidate가 아니라 기존 active value를 반환할 수 있다.
`activate`는 revision을 pin하고 `unpin`은 다음 refresh를 허용하지만 persisted activation freshness를
0으로 초기화하지 않는다. Metadata는 이 capability를 소비하지만 PostgreSQL table과 transaction
구현을 직접 소유하지 않는다.

Snapshot codec은 immutable Python tuple을 Control DB JSON array로 명시적으로 encode하고 legacy
array/object document를 새 immutable graph로 decode한다. Persisted JSON shape, revision과 기존 row의
호환성은 그대로이며 `MOD-07`에는 schema/data migration이 없다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 `SourceReader`로 얻는 current source profile,
  semantic overlay와 budget
- Source Catalog의 reader-session safety contract
- [Guarded Query](../guarded-query/README.md)의 SQL policy revision/capability descriptor
- [Control Plane](../control-plane/README.md)이 구현하는 MetadataStore persistence capability
- [Runtime](../runtime/README.md)의 operations reporting contract
- Runtime composition이 source mode에 따라 단일 authority에서 주입하는 immutable verified-revision
  membership. Bootstrap은 filesystem Assurance contract만, managed는 empty map에서 시작해 Control
  Plane contract만 사용하며 둘을 합치거나 fallback하지 않는다. Input shape와 L2 해석은 Metadata가
  소유하고 provider의 내부 implementation은 알지 않는다.

현재 `metadata.py`가 SQL validator constant를 직접 import하지만, 의미상 소비 대상은 immutable
policy descriptor다. 이 dependency를 바꾸는 refactoring은 외부 context 결과를 그대로 유지한다.
`MetadataService`의 registry dependency는 `SourceReader`이며 source projection mutation capability를
소비하지 않는다.

## 불변조건

- Reader가 실제로 조회할 수 없는 schema/relation을 metadata에 발행하지 않는다.
- DB comment와 semantic text를 명령으로 실행하거나 join 규칙으로 해석하지 않는다.
- Allowed schema/kind, tenant policy, budget, overlay와 revision material drift를 fail-closed한다.
- Persisted snapshot payload와 revision이 다르면 저장값을 사용하지 않는다.
- Published source/metadata graph에 mutable collection 또는 decoder/builder input alias를 남기지 않는다.
- Process restart가 persisted activation freshness를 초기화하지 않는다.
- Cache나 process health state를 Control DB authority로 사용하지 않는다.
- Metadata response와 retrieval은 source별 budget의 relation/column/byte 상한을 지킨다.

## 모듈 내부 변경

다음은 context shape, ranking acceptance와 revision 의미를 보존할 때 독립적으로 변경할 수 있다.

- Cache lookup, refresh coalescing과 internal data structure 개선
- 같은 quality result를 만드는 validation/helper 정리
- Revision 재료와 canonical order를 바꾸지 않는 digest 구현 정리
- 동일한 relation ranking, selection reason, threshold, default fallback과 deterministic ordering을
  만드는 relevance 내부 개선
- Public field와 byte accounting을 보존하는 response assembly 정리
- PostgreSQL query 결과를 같은 snapshot으로 변환하는 catalog query 성능 개선

## 사용자 승인이 필요한 계약 변경

- Metadata context input/output, answerability status, truncation 또는 byte accounting 변경
- `metadata_revision` 재료, ordering, digest format 또는 compatibility 의미 변경
- Applied metadata observation의 기록/제거 시점, staging suppression 또는 failure reason 의미 변경
- Catalog snapshot/persisted JSON shape나 decoder compatibility 변경
- Allowed physical object, comment trust, schema drift 또는 stale fail-closed 정책 변경
- L0/L1/L2 publish 조건과 verified revision 의미 변경
- MetadataStore method/state semantics 또는 rollback pin/resume 의미 변경
- Question-scoped disclosure를 query authorization으로 사용하도록 변경
- Wide relation의 필수 column 우선순위, 필수 수의 target 초과, index hiding과 truncation 의미 변경
- Retrieval threshold, default relation fallback, selection reason 또는 deterministic ordering 변경
- SQL capability를 context에 포함하는 방법이나 SQL policy revision coupling 변경
- `CatalogProvider` 또는 `RuntimeCatalogProvider` method/shape와 invalidate/close 의미 변경
- Published runtime object graph의 mutability, aliasing 또는 public collection type 변경

승인 요청에는 Source Catalog, Guarded Query, Control Plane, Delivery와 Assurance 영향 및 기존
immutable history/rolling replica compatibility를 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_catalog.py tests/test_metadata.py tests/test_relevance.py \
  tests/test_revision.py tests/test_metadata_store.py tests/test_quality_level.py
```

Persisted store의 PostgreSQL integration case는 기본 pytest marker에서 제외되므로 다음을 별도로
실행한다. 같은 파일의 unmarked snapshot codec·legacy compatibility test는 위 focused gate에서
실행된다.

```text
uv run pytest -m integration tests/test_metadata_store.py
uv run pytest -m integration tests/test_source_database_corners.py
```

Catalog reader, source epoch/CAS 또는 PostgreSQL privilege 경계를 변경하면 전체 integration gate를,
live retrieval acceptance가 필요하면 configured database에서 `uv run query-man-evaluate`도 실행한다.
완료 전 root `AGENTS.md`의 전체 gate를 실행한다.

## 집중해서 읽을 범위

Metadata 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 metadata code와 focused tests
3. Source Catalog의 소비 필드와 MetadataStore port
4. [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md),
   [ADR 0008](../../decisions/0008-physical-key-and-index-disclosure.md),
   [ADR 0009](../../decisions/0009-question-scoped-column-disclosure.md),
   [ADR 0010](../../decisions/0010-revision-scoped-retrieval-index.md)과
   [ADR 0011](../../decisions/0011-metadata-quality-level-publish-gate.md) 중 변경과 직접 관련된 결정
5. Context 또는 revision을 소비하는 module contract

MCP SDK 구현, source admin HTTP route와 query pool internals는 계약을 변경하지 않는 한 읽을
필요가 없다.
