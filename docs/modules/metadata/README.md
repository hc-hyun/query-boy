# Metadata Module

Status: Logical boundary; physical package split pending

## 목적

Metadata는 reader가 실제로 볼 수 있는 PostgreSQL physical catalog와 versioned semantic overlay를
검증된 immutable metadata revision으로 만들고, 사용자 질문에 필요한 bounded context를 제공한다.

Metadata는 SQL 실행기가 아니다. 질문에 어떤 relation, column, grain, join과 business rule을
사용해야 하는지 설명하고 Guarded Query가 확인할 published revision을 제공한다.

## 소유 책임

- Reader privilege 안에서의 `pg_catalog` relation/column/key/index/comment introspection
- Catalog snapshot shape와 validation, persisted serialization format 및 compatibility 확인
- Source definition과 physical snapshot으로부터 `metadata_revision` 계산
- Metadata refresh coalescing, cache TTL, stale window, retry와 source epoch
- Persisted immutable revision publish/active/pin을 소비하는 MetadataStore port
- L0/L1/L2 publish quality gate
- Revision-scoped relevance index와 question-scoped relation/column disclosure
- Grain, measure, join, business term, ambiguity와 answerability response projection
- Metadata response byte limit과 untrusted comment sanitization
- DB-owner-declared physical relation의 bounded catalog estimate/size observation

## 소유하지 않는 책임

- Source manifest/budget schema, reader connection policy와 runtime registry mutation
- SQL AST/function/operator/result-OID validation과 query execution
- Control DB schema, transaction, advisory lock와 source generation transition
- HTTP/MCP input schema, authentication과 public error serialization
- Verified result의 live SQL 실행과 canonical result hash ownership
- Source DB의 view, reader role와 grant migration
- RLS source admission과 serving 재개 결정

## 현재 코드 위치

- [`catalog.py`](../../../src/query_man/catalog.py): PostgreSQL physical introspection adapter
- [`metadata.py`](../../../src/query_man/metadata.py): lifecycle, cache, validation과 context projection
- [`relevance.py`](../../../src/query_man/relevance.py): revision-scoped retrieval index
- [`revision.py`](../../../src/query_man/revision.py): metadata revision digest
- [`quality_level.py`](../../../src/query_man/quality_level.py): runtime publish quality gate
- [`errors.py`](../../../src/query_man/errors.py): metadata unavailable/revision mismatch domain error;
  public rendering은 Delivery 소유
- [`models.py`](../../../src/query_man/models.py): catalog snapshot/prepared metadata type,
  `CatalogProvider`와 Runtime 전용 `RuntimeCatalogProvider`
- [`metadata_store.py`](../../../src/query_man/metadata_store.py): MetadataStore port와 snapshot codec;
  같은 파일의 PostgreSQL transaction implementation은 Control Plane 소유
- Focused tests: [`test_catalog.py`](../../../tests/test_catalog.py),
  [`test_metadata.py`](../../../tests/test_metadata.py),
  [`test_relevance.py`](../../../tests/test_relevance.py),
  [`test_revision.py`](../../../tests/test_revision.py),
  [`test_quality_level.py`](../../../tests/test_quality_level.py),
  [`test_metadata_store.py`](../../../tests/test_metadata_store.py),
  [`test_reader_policy.py`](../../../tests/test_reader_policy.py),
  [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py)

`MetadataService`와 Control Plane candidate staging은 `load/close`만 제공하는 작은
`CatalogProvider`를 소비한다. Runtime은 이를 확장한 `RuntimeCatalogProvider`를 요구하고 concrete
`PostgresCatalog`는 두 Protocol을 구조적으로 구현한다. Assurance offline workflow는
`assurance_cli.py`에서만 concrete catalog와 `MetadataService`를 조립한다.

Control DB persisted snapshot과 process 안의 published Python graph는 서로 다른 경계에서 모두
immutable하다. Catalog/metadata public sequence는 tuple이고 dataclass는 frozen이다. Private builder나
decoder 입력은 provider/cache boundary 전에 새 graph로 freeze하며 alias를 남기지 않는다.

## 제공 인터페이스와 소유 경계

이 절에서 `module interface`는 다른 logical module에 공개한 Python type, Protocol, use case와 domain
error semantics만 뜻한다. External wire, persisted format, revision identity와 lifecycle invariant는
각 subsection에 별도 범주로 기록한다.

### Published metadata interface

Guarded Query, Control Plane과 Assurance가 소비하는 공식 Python module interface는
`CatalogSnapshot`, `PreparedMetadata`와 다음 application use case다.

```text
await get_published(source_id) -> PreparedMetadata(snapshot, exact metadata_revision)
```

- Source가 없으면 Source Catalog의 `SourceNotFoundError`, current compatible value를 제공할 수 없으면
  Metadata의 `MetadataUnavailableError` 의미를 따른다.
- Snapshot은 current source definition과 compatible해야 한다.
- Snapshot과 `PreparedMetadata`의 도달 가능한 public graph는 재귀적으로 immutable하며 provider는
  mutable builder나 decoder 입력의 alias를 반환하지 않는다.
- Revision mismatch, schema drift, deterministic reader-policy mismatch와 stale upper bound 초과는
  fail-closed한다.
- Published snapshot relation은 Guarded Query relation allowlist의 최대 범위다.
- Row estimate는 fresh best-effort hint이며 persisted revision과 correctness 재료가 아니다.

### Metadata context application interface and external format

Delivery는 다음 transport-independent application use case와 result를 소비한다.

```text
await get_context(source_id, question, max_objects=2) ->
  source_id, source_name, source_description, question
  metadata_revision, sql_policy_revision, snapshot_status, quality_level
  sql_capabilities, answerability, relations, joins, business_terms
  composition_hints, ambiguities, truncated
```

`snapshot_status`는 `fresh|stale`, `quality_level`은 `L0|L1|L2`다. `sql_capabilities`는
`functions`, `cast_types`, `unqualified_cast_types`를 가진다. `answerability`는 `status`,
`reason_codes`, `messages`, `missing_concepts`, `options`를 가진다.

Relation/column, approved join, business term, composition hint와 ambiguity의 기존 field와 ordering은
application result 의미다. Delivery가 이를 HTTP/MCP에 투영할 때 compact UTF-8 JSON byte accounting과
array/object shape를 보존하는 것은 external wire format이다. Question context에서 일부 column이
생략돼도 SQL deny rule은 아니며, query allowlist는 published snapshot과 Guarded Query policy가 정한다.

### Metadata revision format and compatibility

`metadata_revision`은 다음 current source/catalog material의 canonical JSON SHA-256
(`sha256:` prefix)이다.

- `source_id`, allowed schema/relation kind와 non-default tenant-isolation value
- Guarded Query가 소유한 immutable canonical-time policy material version 1
- 전체 execution budget과 semantic overlay
- Relation schema/name/kind/comment/definition hash/security-invoker
- Ordered primary/foreign key와 index definition
- Column name/ordinal/type/nullability/comment

Connection/credential, source provenance, control generation/state version, `minimum_quality_level`,
`estimated_rows`, freshness/cache state는 revision에서 제외한다. List/tuple과 dict/immutable mapping은
각각 같은 canonical array/object로 정규화한다. 이 재료, ordering, digest와 persisted snapshot codec은
policy/compatibility identity 및 persisted/versioned format이다.

[ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)는 PostgreSQL 18/UTF-8 connection
admission과 SQL policy v3를 추가했지만 metadata revision algorithm, canonical-time material과 현재
두 source revision을 바꾸지 않는다. Server/client encoding이나 RLS base-policy identity를 metadata
revision에 새로 넣지 않았다. RLS는 launch admission에서 전부 차단되므로 이 미포함 상태를 RLS
serving compatibility로 해석하지 않는다.

Broader source-semantics fingerprint와 RLS attestation 연구는
[ADR 0020](../../decisions/0020-lossless-interval-and-json-numeric-encoding.md)과
[ADR 0024](../../decisions/0024-rls-policy-drift-attestation.md)에 기록된 parked research다. 미래
proposal의 snapshot/revision version이나 helper는 현재 interface가 아니며 이 문서에 복제하지 않는다.

### Metadata refresh and publish lifecycle

Catalog load와 resource observation의 reader lease는 다음 순서를 지킨다.

1. Pool이 `client_encoding=UTF8` startup parameter를 요청한다.
2. Checkout 직후 Source Catalog의 no-SQL `require_reader_connection_policy()`를 호출한다.
3. PostgreSQL 18, server/client `UTF8`, psycopg `utf-8` codec이 확인된 뒤에만 read-only `BEGIN`,
   UTC/budget session setting, session-policy verifier와 catalog SQL을 실행한다.

Deterministic connection mismatch는 `ReaderSessionPolicyError`로 분류하고 connection을 close/discard한다.
이 시점에는 transaction이나 application SQL이 없으므로 rollback하지 않는다. Active Metadata refresh는
details 없는 `METADATA_UNAVAILABLE`로 끝나며 warm stale snapshot으로 fallback하지 않는다. Resource
observation은 성공값을 만들지 않고 Runtime의 `RESOURCE_READ_FAILED` 경계로 전달된다. Connection-info
접근 자체의 transport/driver failure는 marker로 바꾸지 않고 기존 transient 분류를 유지한다.

그 뒤 lifecycle은 다음과 같다.

- Fresh candidate를 validate하고 source definition과 함께 revision을 계산한다.
- Store가 있으면 append-only persisted snapshot을 publish하고 committed active value만 cache한다.
- Rollback pin, resume와 persisted activation freshness provenance를 보존한다.
- Source generation 교체 시 epoch와 current profile을 함께 확인해 지연 refresh를 거부한다.
- Ordinary transient catalog failure는 bounded stale window 안에서만 마지막 정상 revision을 제공한다.
- Reader-policy mismatch, catalog structure/budget rejection, schema/overlay validation failure는 stale
  fallback 없이 fail-closed한다.
- Rollback으로 다른 revision이 pin된 경우에만 bounded cached revision을 별도 stale path로 제공한다.
- Fresh cache hit는 live connection policy나 privilege probe를 다시 수행한 것으로 해석하지 않는다.

Static first launch는 reviewed `development-issues`, `market-voc` bootstrap composition이고
MetadataStore/Control DB를 조립하지 않는다. 구현된 managed composition과 MetadataStore lifecycle은
보존하지만 dynamic onboarding과 hot reload는 첫 launch 밖이다. 어떤 production composition에서도
RLS source는 Source Catalog/Runtime admission에서 Metadata에 도달하기 전에 차단된다. Metadata에 남은
RLS validation branch는 방어·history compatibility 코드이지 지원되는 serving path가 아니다.

PostgreSQL은 scalar domain의 RowDescription을 base OID로 평탄화한다. 따라서 Runtime bootstrap과
Assurance offline CLI가 조립하는 `PostgresCatalog(reject_domain_columns=True)`는 eligible column의
transient `pg_type.typtype`을 읽고 domain이면 snapshot publication 전에
`_CatalogValidationError`→details 없는 `METADATA_UNAVAILABLE`로 끝낸다. Managed Runtime/staging의
기본 Catalog는 이 static-launch guard를 활성화하지 않아 preserved lifecycle 의미를 바꾸지 않는다.
`type_kind`는 successful `CatalogSnapshot`, metadata revision과 persisted codec에 포함되지 않는다.

### Resource observation provider (`CTRL-07A`, implemented)

Runtime-only capability는 Source Catalog가 검증한 optional observability definition의 exact physical
relation만 조회한다. 대상 전체를 열거하지 않고 system schema와 unsupported relkind를 거부하며 한
representative relation과 최대 16개 distinct storage relation으로 제한한다. Existing catalog의
max-one reader pool, read-only transaction과 metadata timeout을 재사용한다.

Provider는 `pg_class.reltuples`가 non-negative일 때 rounded representative rows와 configured
relations의 table/index/total bytes만 반환한다. Relation 이름, OID, catalog row와 SQL은 Control payload에
넣지 않는다. Observation은 `CatalogSnapshot`, metadata cache/persistence와 revision에 들어가지 않는다.

### Runtime observation signal (`CTRL-06`)

Metadata는 public context나 persisted snapshot shape를 바꾸지 않고 Runtime operations interface에
replica-local cache 상태만 알린다.

- Fresh publish, persisted restore 또는 pinned active value를 cache에 적용한 뒤 exact revision을 기록한다.
- Source cache invalidate와 disabled source apply는 applied revision을 제거한다.
- Probe/cache failure는 `METADATA_PROBE_FAILED`와 unavailable health를 기록하되 credential, question,
  SQL이나 raw exception을 observation에 넣지 않는다.
- Control Plane candidate staging의 health-update suppression 동안에는 이 signal도 억제한다.
- 이 signal은 best-effort observation이며 persisted authority, readiness나 query correctness가 아니다.

### CatalogProvider interfaces

Metadata가 제공하는 공식 Python Protocol은 다음 exact method set이다.

```text
CatalogProvider:
  async load(source) -> CatalogSnapshot
  async close() -> None

RuntimeCatalogProvider extends CatalogProvider:
  async invalidate(source_id: str) -> None
  async observe_resources(source) -> ResourceObservation
```

`MetadataService`는 작은 `CatalogProvider`만 요구한다. Runtime production composition은 generation
교체와 resource observation 때문에 extended Protocol을 요구한다. `load` 결과는 recursively immutable
`CatalogSnapshot`이어야 한다. 이는 concrete adapter의 private API를 다른 module에 공개하지 않는다.

### MetadataStore port

Metadata가 제공하고 Control Plane implementation이 구현하는 공식 Python port는 다음과 같다.

```text
get_active(source)
get_revision(source, revision)
publish(source, candidate) -> committed active value
activate(source, revision) -> pinned active value
unpin(source)
close()
```

`publish`는 active revision이 pin돼 있으면 candidate 대신 기존 active value를 반환할 수 있다.
`activate`는 revision을 pin하고 `unpin`은 다음 refresh를 허용하지만 persisted activation freshness를
초기화하지 않는다. Snapshot codec은 immutable Python tuple을 기존 Control DB JSON array로 encode하고
legacy array/object document를 새 immutable graph로 decode한다. ADR 0025에는 schema/data migration,
historical row update나 delete가 없다.

## 소비 인터페이스와 전제

Metadata가 소비하는 공식 module interface는 다음으로 제한한다.

- [Source Catalog](../source-catalog/README.md)의 `SourceReader`, `READER_CLIENT_ENCODING`,
  `require_reader_connection_policy()`, `ReaderSessionPolicyError`와 reader-session verifier
- [Guarded Query](../guarded-query/README.md)의 SQL policy revision/capability descriptor와 immutable
  canonical-time material
- [Control Plane](../control-plane/README.md)이 구현하는 `MetadataStore` port
- [Runtime](../runtime/README.md)의 operations reporting interface

Composition 전제는 interface와 구분한다. Static launch는 bootstrap filesystem의 exact two-source
definition과 Assurance verified-revision membership만 사용한다. Managed mode는 empty registry에서
Control Plane projection과 persisted MetadataStore를 사용하며 bootstrap과 합치거나 fallback하지 않는다.
Managed mode 자체는 보존되지만 ADR 0025 static first launch에서는 활성화하지 않는다.

현재 `metadata.py`가 Guarded Query constant를 직접 import하지만 의미상 소비 대상은 immutable policy
descriptor다. `MetadataService`의 registry dependency는 `SourceReader`이며 mutation capability나 Control
DB implementation을 소비하지 않는다.

## 불변조건

- ADR 0025 launch path에서는 RLS source를 Metadata serving 대상으로 취급하지 않는다.
- Catalog connection은 checkout→no-SQL PG18/UTF-8 verifier→`BEGIN`→session verifier→catalog SQL 순서다.
- Deterministic connection/session policy와 catalog validation 실패는 stale metadata로 우회하지 않는다.
- Reader가 실제로 조회할 수 없는 schema/relation을 metadata에 발행하지 않는다.
- DB comment와 semantic text를 명령으로 실행하거나 join 규칙으로 해석하지 않는다.
- Allowed schema/kind, budget, overlay와 revision material drift를 fail-closed한다.
- Persisted snapshot payload와 revision이 다르면 저장값을 사용하지 않는다.
- Published source/metadata graph에 mutable collection 또는 decoder/builder input alias를 남기지 않는다.
- Process restart가 persisted activation freshness를 초기화하지 않는다.
- Cache나 process health state를 Control DB authority로 사용하지 않는다.
- Metadata response와 retrieval은 source별 relation/column/byte 상한을 지킨다.
- Reader compatibility 추가는 metadata revision, persisted schema와 canonical result hash를 바꾸지 않는다.

## 모듈 내부 변경

다음은 module interface와 별도 승인 대상 의미를 보존할 때 독립적으로 변경할 수 있다.

- Cache lookup, refresh coalescing과 internal data structure 개선
- 같은 quality result를 만드는 validation/helper 정리
- Revision 재료와 canonical order를 바꾸지 않는 digest implementation 정리
- 동일한 relation ranking, threshold, selection reason과 deterministic ordering을 만드는 relevance 개선
- Public field와 byte accounting을 보존하는 response assembly 정리
- PostgreSQL query 결과를 같은 snapshot으로 변환하는 catalog query 성능 개선
- Connection verifier call order, mismatch discard/no-stale outcome과 external error 의미를 보존하는
  private cleanup/helper 정리

## 사용자 승인이 필요한 경계 변경

승인 요청은 다음 중 실제 변경 범주를 구분해 제시한다. 목록 전체를 하나의 module interface 변경으로
부르지 않는다.

- **Module interface:** Published metadata/context use case, `CatalogProvider`,
  `RuntimeCatalogProvider`, `MetadataStore`, public DTO 또는 domain error semantics
- **External API/wire:** Context field, answerability status, truncation, ordering, JSON shape나 byte accounting
- **Persisted/versioned format:** Catalog snapshot document, decoder compatibility, Control DB metadata row 의미
- **Policy/compatibility identity:** `metadata_revision` 재료/order/digest, SQL policy coupling, L0/L1/L2
  publish 조건, stale bound와 verified-revision 의미
- **Safety/lifecycle invariant:** Checkout/pre-BEGIN verification, mismatch discard/no-stale, catalog object/comment
  trust, drift fail-closed, pin/resume와 source epoch ordering
- **Ownership/composition boundary:** MetadataStore implementation ownership, concrete catalog 조립 위치,
  bootstrap/managed authority participation 또는 RLS serving 재개

승인 요청에는 Source Catalog, Guarded Query, Control Plane, Delivery와 Assurance 영향, rolling replica와
persisted history compatibility, migration/rollback 및 관련 verification을 포함한다. Protected database나
deployment에 실제 변경을 수행하는 것은 repository 변경 승인과 별도의 operational execution 승인이
필요하다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_reader_policy.py tests/test_registry.py tests/test_catalog.py \
  tests/test_metadata.py tests/test_relevance.py tests/test_revision.py \
  tests/test_metadata_store.py tests/test_quality_level.py
```

Persisted store와 source PostgreSQL 경계는 별도로 실행한다.

```text
uv run pytest -m integration tests/test_metadata_store.py
uv run pytest -m integration tests/test_source_database_corners.py
```

Catalog reader, source epoch/CAS 또는 PostgreSQL privilege를 바꾸면 repository 전체 integration gate를,
live retrieval acceptance가 필요하면 configured database에서 `uv run query-man-evaluate`도 실행한다.
완료 전 root `AGENTS.md`의 전체 gate를 실행한다.

## 집중해서 읽을 범위

Metadata 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 metadata code와 focused tests
3. Source Catalog의 `SourceReader`/reader-policy interface와 MetadataStore port
4. [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md) 및 변경과 직접 관련된
   [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md),
   [ADR 0008](../../decisions/0008-physical-key-and-index-disclosure.md),
   [ADR 0009](../../decisions/0009-question-scoped-column-disclosure.md),
   [ADR 0010](../../decisions/0010-revision-scoped-retrieval-index.md),
   [ADR 0011](../../decisions/0011-metadata-quality-level-publish-gate.md)
5. Context/published interface나 revision identity를 직접 소비하는 module

MCP SDK 구현, source admin HTTP route와 query pool internals는 위 interface나 별도 승인 대상 경계를
변경하지 않는 한 읽을 필요가 없다.
