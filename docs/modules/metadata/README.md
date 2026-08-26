# Metadata Module

Status: Physical package boundary active

## 목적

### 30초 요약

Metadata는 PostgreSQL reader가 실제로 볼 수 있는 table/view 구조를 읽어 **검증된 metadata
snapshot**으로 고정하고, 질문에 필요한 relation·column·join·business rule만 골라 설명한다.
쉽게 말하면 database의 안전한 지도를 만들고 질문마다 필요한 부분만 펼쳐 주는 module이다.

| 질문 | 답 |
|---|---|
| 무엇을 입력으로 받는가? | Source Catalog가 검증한 source/profile과 reader-visible PostgreSQL catalog |
| 무엇을 제공하는가? | Immutable `PreparedMetadata`, exact `metadata_revision`, 질문별 context |
| SQL도 실행하는가? | 아니요. Guarded Query가 published snapshot과 revision을 확인한 뒤 SQL을 검증·실행한다. |
| 현재 first launch에 참여하는가? | 예. ADR 0025의 두 static source를 읽지만 Control DB `MetadataStore`는 조립하지 않는다. |

현재 launch authority는
[ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)다. RLS serving과 broader result encoding
연구는 current Metadata interface가 아니다.

## 소유 책임

- Reader privilege 안에서 PostgreSQL relation/column/key/index/comment를 introspection
- Catalog snapshot type, validation, immutability와 persisted snapshot codec compatibility
- Source definition과 physical snapshot으로 exact `metadata_revision` 계산
- Metadata cache, coalesced refresh, stale upper bound, retry와 source epoch
- L0/L1/L2 publish quality 판정
- Revision-scoped retrieval index와 question-scoped relation/column disclosure
- Grain, measure, join, business term, ambiguity와 answerability context assembly
- Metadata response byte limit과 untrusted database comment sanitization
- DB owner가 선언한 physical relation의 bounded record/storage observation provider

## 소유하지 않는 책임

- Source manifest/budget schema, reader connection policy와 runtime registry mutation
- SQL AST/function/operator/final-result OID validation과 query execution
- Control DB schema, transaction, advisory lock, generation과 persisted implementation
- Caller authentication/authorization와 HTTP/MCP request/response/error rendering
- Verified result의 live SQL 실행과 canonical result hash
- Source DB의 view, reader role/grant와 business migration
- RLS source admission과 serving 재개 결정

## 현재 코드 위치

| 위치 | Metadata가 소유하는 범위 | 주의점 |
|---|---|---|
| [`metadata/catalog.py`](../../../src/query_man/metadata/catalog.py) | PostgreSQL catalog adapter와 bounded resource observation | Reader-policy/transaction 순서를 보존 |
| [`metadata/service.py`](../../../src/query_man/metadata/service.py) | `MetadataService`, cache/refresh/quality/context lifecycle | Main application use case |
| [`metadata/relevance.py`](../../../src/query_man/metadata/relevance.py) | Revision-scoped retrieval index와 deterministic ranking | Disclosure policy를 임의로 바꾸지 않음 |
| [`metadata/revision.py`](../../../src/query_man/metadata/revision.py) | Canonical metadata revision digest | Guarded Query policy material을 소비하는 shared identity |
| [`metadata/quality_level.py`](../../../src/query_man/metadata/quality_level.py) | L0/L1/L2 publish gate | Assurance verified membership을 소비 |
| [`metadata/models.py`](../../../src/query_man/metadata/models.py) | Catalog DTO, `PreparedMetadata`, provider Protocol | Source types는 `source_catalog/models.py`에서 leaf import |
| [`metadata/store.py`](../../../src/query_man/metadata/store.py) | `MetadataStore` port, domain error와 snapshot codec | PostgreSQL implementation은 [`managed/metadata_store.py`](../../../src/query_man/managed/metadata_store.py)의 Control Plane 소유 |
| [`errors.py`](../../../src/query_man/errors.py) | Metadata availability/revision domain-error 의미 | Public envelope은 Delivery 소유인 shared transition file |
| [`test_catalog.py`](../../../tests/test_catalog.py), [`test_metadata.py`](../../../tests/test_metadata.py), [`test_relevance.py`](../../../tests/test_relevance.py), [`test_revision.py`](../../../tests/test_revision.py) | Catalog, service, retrieval과 revision focused tests | Provider 의미를 고정 |
| [`test_quality_level.py`](../../../tests/test_quality_level.py), [`test_metadata_codec.py`](../../../tests/test_metadata_codec.py), [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py) | Quality, core codec와 PostgreSQL edge acceptance | Codec test는 DB 없이 persisted JSON compatibility를 고정 |
| [`test_metadata_store.py`](../../../tests/test_metadata_store.py) | Managed PostgreSQL store integration | Control Plane implementation이 Metadata port/codec을 소비하는 direct-consumer test |

Metadata core는 `src/query_man/metadata` physical package에 있고 Control Plane의 concrete PostgreSQL
store는 `src/query_man/managed/metadata_store.py`에 격리했다. `metadata/store.py`에는 port, domain
error와 codec만 둔다. Package marker는 re-export하지 않고 consumer는 leaf path를 직접 import한다.
공통 `errors.py`와 관련 cross-module test는 coordinating agent가 single-writer로 다룬다.

## 제공 인터페이스와 소유 경계

아래 Python code block은 current signature다. Result field 목록은 이해를 돕는 개념 요약이며 exact
key/type/order는 linked code와 runnable test가 고정한다. External wire, persisted codec, revision
identity와 refresh invariant는 Python interface와 별도 경계다.

### Published metadata interface

Guarded Query, Control Plane, Runtime과 Assurance가 소비하는 핵심 DTO와 service signature는 다음과 같다.

```text
CatalogSnapshot(relations: tuple[CatalogRelation, ...])
PreparedMetadata(
  snapshot: CatalogSnapshot,
  revision: str,
  freshness_age_ms: int | None = None
)

async get_published(source_id: str) -> PreparedMetadata
async get_context(
  source_id: str, question: str, max_objects: int = 2
) -> dict[str, object]
invalidate(source_id: str | None = None) -> None
async rollback(source_id: str, revision: str) -> PreparedMetadata
async resume_automatic_publish(source_id: str) -> None
async close() -> None
```

- Published DTO graph는 frozen/tuple이며 provider와 decoder는 mutable input alias를 남기지 않는다.
- `get_published`는 current source와 compatible하고 quality gate를 통과한 exact revision만 준다.
- `invalidate`는 source epoch, cache/index와 replica-local applied-revision signal을 함께 갱신한다.
- `rollback`/`resume_automatic_publish`는 store가 조립된 managed lifecycle에서만 동작한다.
- Source가 없으면 `SourceNotFoundError`, compatible current value가 없으면 `MetadataUnavailableError`다.
  Stale caller revision의 `MetadataRevisionMismatchError` 의미도 Metadata가 소유한다.

Exact nested DTO field는 [`metadata/models.py`](../../../src/query_man/metadata/models.py)와
[`test_catalog.py`](../../../tests/test_catalog.py)가 고정한다. Published relation은 Guarded Query
allowlist의 최대 범위이며 `estimated_rows`는 revision/correctness 재료가 아닌 best-effort hint다.

### Metadata context application result

`get_context`의 exact return type은 `dict[str, object]`이고 conceptual top-level shape는 다음과 같다.

```text
source_id, source_name, source_description, question
metadata_revision, sql_policy_revision
snapshot_status, quality_level, sql_capabilities, answerability
relations, joins, business_terms, composition_hints, ambiguities, truncated
```

`snapshot_status`는 `fresh|stale`, `quality_level`은 `L0|L1|L2`다. `sql_capabilities`는
`functions|cast_types|unqualified_cast_types`, `answerability`는
`status|reason_codes|messages|missing_concepts|options`를 가진다. Field/order는 application result이고
Delivery의 HTTP/MCP JSON과 compact UTF-8 accounting은 external wire다. Question-scoped column omission은
SQL deny rule이 아니며 전체 published snapshot이 relation ceiling이다.

Question-scoped disclosure의 exact column priority, truncation과 hard byte limit은
[ADR 0009](../../decisions/0009-question-scoped-column-disclosure.md), retrieval index/ranking은
[ADR 0010](../../decisions/0010-revision-scoped-retrieval-index.md)을 따른다.

### CatalogProvider interfaces

Metadata가 제공하는 정확한 Python Protocol은 다음 exact method set이다.

```text
CatalogProvider:
  async load(source: SourceProfile) -> CatalogSnapshot
  async close() -> None

RuntimeCatalogProvider extends CatalogProvider:
  async invalidate(source_id: str) -> None
  async observe_resources(source: SourceProfile) -> ResourceObservation

ResourceObservation:
  representative_records: int | None
  table_bytes: int
  index_bytes: int
  total_storage_bytes: int
```

Service와 Control staging은 작은 Provider만, Production Runtime은 generation invalidation/resource
observation 때문에 extended Provider를 요구하고 required callable을 ready 전에 검사한다. Resource read는
검증된 representative relation 하나와 최대 16개 storage relation, existing read-only pool/timeout으로
제한한다. Relation/OID/SQL을 Control payload에 넣지 않고 snapshot/revision에도 포함하지 않는다. Exact
observation meaning은 [Control Plane reference](../control-plane/observability.md)를 따른다.

### MetadataStore port

`MetadataStore`는 **Metadata가 소유하고 `MetadataService`가 소비하는 port**다.

```text
async get_active(source: SourceProfile) -> PreparedMetadata | None
async get_revision(source: SourceProfile, revision: str) -> PreparedMetadata
async publish(source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata
async activate(source: SourceProfile, revision: str) -> PreparedMetadata
async unpin(source: SourceProfile) -> None
async close() -> None
```

Runtime이 managed composition에서만 Control Plane의
`query_man.managed.metadata_store.PostgresMetadataStore`를 이 port에 주입한다. Static composition은
managed package를 import하거나 store를 조립하지 않는다.
Metadata는 Control Plane module/table/SQL/lock를 import하지 않는다. 즉 `Metadata -> Control Plane`
dependency가 아니라 Control Plane이 Metadata-owned port를 구현하는 dependency inversion이다.

`publish`는 pin된 active value를 반환할 수 있고 `activate`는 pin, `unpin`은 다음 refresh를 허용하되
activation freshness를 초기화하지 않는다. Codec은 immutable Python graph와 기존/legacy Control DB JSON
array/object를 호환한다. Exact persisted meaning은 [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md)과
[Control Plane reference](../control-plane/persistence-and-recovery.md)를 따른다.

### Metadata revision identity

`metadata_revision`은 `sha256:` prefix를 가진 canonical-JSON SHA-256이다.

| 포함 | 제외 |
|---|---|
| Source ID, allowed schema/relation kind, non-default tenant isolation | Connection, credential와 provenance |
| Guarded Query canonical-time policy material | Control generation/state version와 minimum quality level |
| Full execution budget와 semantic overlay | Row estimate, cache/freshness와 operational observation |
| Relation/column/key/index와 definition/security identity | Runtime health와 usage |

List/tuple과 dict/read-only mapping은 같은 canonical array/object다. Exact material/order/golden은
[`metadata/revision.py`](../../../src/query_man/metadata/revision.py), [`test_revision.py`](../../../tests/test_revision.py)와
[`test_metadata_codec.py`](../../../tests/test_metadata_codec.py)가 고정한다. ADR 0025의 PG18/UTF-8 및 SQL
policy v3 전환은 algorithm/canonical-time/two-source revision을 바꾸지 않았다. RLS identity 미포함은
serving 호환성이 아니라 RLS 전면 차단을 전제로 한다.

### Refresh, stale와 publish lifecycle

Reader lease 순서는 `checkout -> no-SQL PG18/server-client UTF-8 verifier -> read-only BEGIN ->
UTC/budget session verifier -> catalog SQL`이다.

| 상황 | 결과 |
|---|---|
| Fresh compatible candidate | Validate, quality/revision 계산, store가 있으면 append-only publish, committed active value만 cache |
| Ordinary transient catalog failure | Persisted activation provenance 기준 bounded stale window 안에서만 마지막 정상 revision 제공 |
| Reader connection/session mismatch | Connection close/discard, stale fallback 없이 `METADATA_UNAVAILABLE` |
| Structure/budget/schema/overlay/revision failure | Stale fallback 없이 fail-closed |
| Source epoch 교체 또는 rollback pin | Delayed refresh 거부; pin된 compatible revision 외 pointer 변경 금지 |

Cache hit는 live policy/privilege probe가 아니고 restart는 persisted freshness를 초기화하지 않는다.
Applied-revision health signal은 best-effort이며 authority/readiness/correctness가 아니다. Static launch는
two-source bootstrap으로 Store를 조립하지 않고 RLS는 Metadata 전에 차단한다. Static/offline Catalog는
base-OID로 평탄화되는 exposed scalar domain column도 publish 전에 거부한다. Exact lifecycle은
[ADR 0007](../../decisions/0007-immutable-metadata-publishing.md)과 ADR 0025를 따른다.

## 소비 인터페이스와 전제

| Provider | Metadata가 소비하는 공개 interface | 의무와 금지선 |
|---|---|---|
| [Source Catalog](../source-catalog/README.md) | `SourceReader`, `READER_CLIENT_ENCODING`, connection/session verifier와 immutable `SourceProfile` | Checkout/pre-BEGIN 순서를 지키고 writer capability를 요구하지 않는다. |
| [Guarded Query](../guarded-query/README.md) | SQL policy revision/capability descriptor와 immutable canonical-time material | Metadata revision input으로만 소비하며 query executor private API에 의존하지 않는다. |
| [Runtime](../runtime/README.md) | Operations/health reporting sink | Replica-local cache 상태만 secret-free하게 알린다. |
| Metadata-owned port | `MetadataStore` | Runtime이 implementation을 주입한다. Metadata는 Control Plane implementation에 의존하지 않는다. |

Composition 전제는 Python dependency와 구분한다. Static mode는 filesystem two-source/verified membership을,
managed mode는 empty registry에서 Control DB projection/store를 사용한다. 두 authority를 merge하거나
fallback하지 않는다. Delivery/Guarded Query/Control Plane은 위 application interface의 consumer이지
Metadata가 그 module의 private implementation을 소비한다는 뜻이 아니다.

## 불변조건

- ADR 0025 launch에서는 RLS source를 Metadata serving 대상으로 취급하지 않는다.
- Catalog connection은 checkout→no-SQL verifier→`BEGIN`→session verifier→catalog SQL 순서다.
- Deterministic reader/session/catalog validation failure를 stale metadata로 우회하지 않는다.
- Reader가 실제로 조회할 수 없는 schema/relation/key/index를 publish하지 않는다.
- Database comment와 semantic text를 명령이나 자동-approved join으로 해석하지 않는다.
- Source/profile, snapshot, revision/quality와 persisted payload drift를 fail-closed한다.
- Published graph에 mutable collection이나 decoder/builder input alias를 남기지 않는다.
- Process restart나 cache hit를 새 persisted freshness/connection verification으로 해석하지 않는다.
- Context relation/column/byte limit를 지키되 question-scoped omission을 SQL deny rule로 확대하지 않는다.
- Resource observation을 metadata snapshot/revision, correctness나 public relation allowlist에 넣지 않는다.
- Reader compatibility 추가가 metadata revision, persisted schema나 verified result hash를 암묵적으로
  바꾸지 않게 한다.

## 모듈 내부 변경

다음은 official interface와 external/persisted/policy/lifecycle 의미를 보존할 때 독립적으로 바꿀 수 있다.

- Cache lookup, refresh coalescing과 private data structure 개선
- 같은 validation/quality 결과를 만드는 helper 정리
- Revision material/canonical ordering을 바꾸지 않는 digest implementation 정리
- 동일 ranking/threshold/selection reason/ordering을 만드는 retrieval 성능 개선
- Public field/ordering/byte accounting을 보존하는 context assembly 정리
- 같은 snapshot을 만드는 catalog query 성능 개선
- Verifier order, mismatch discard/no-stale와 safe error를 보존하는 cleanup 정리

## 사용자 승인이 필요한 경계 변경

다음 중 하나라도 의미가 달라지면 구현을 멈추고 정확한 범주, provider/consumer, compatibility,
migration/rollback, 안전 영향과 검증 계획을 제시한다.

| 변경 범주 | Metadata에서 멈춰야 하는 예 |
|---|---|
| Module interface | `MetadataService`, catalog DTO, `CatalogProvider`, `RuntimeCatalogProvider`, `MetadataStore`와 domain error shape/call semantics |
| External API/wire | Context field/status/order/truncation, JSON array/object와 byte accounting |
| Persisted/versioned format | Snapshot document/codec, legacy decode와 active/pinned metadata row 의미 |
| Policy/compatibility identity | Revision material/order/digest, SQL-policy coupling, L0/L1/L2, disclosure/ranking와 stale upper bound |
| Safety/lifecycle invariant | Reader preflight, no-stale failure, catalog/comment trust, publish/pin/resume/source-epoch ordering |
| Ownership/composition boundary | Store implementation ownership, concrete catalog/staging 조립, bootstrap/managed authority와 RLS serving |

Source Catalog, Guarded Query, Control Plane, Delivery, Runtime, Assurance와 persisted history/rolling replica
영향을 함께 제시한다. Protected database/deployment action은 repository 의미 승인과 별도 실행 승인이
필요하다.

## 검증

기본 Metadata gate:

```text
uv run pytest tests/test_reader_policy.py tests/test_registry.py tests/test_catalog.py \
  tests/test_metadata.py tests/test_relevance.py tests/test_revision.py \
  tests/test_metadata_codec.py tests/test_quality_level.py
```

| 변경 영역 | 추가 검증 |
|---|---|
| Core snapshot codec/legacy JSON | `uv run pytest tests/test_metadata_codec.py` |
| Managed store/Control DB | `uv run pytest -m integration tests/test_metadata_store.py` |
| PostgreSQL catalog/reader/domain/resource | `uv run pytest -m integration tests/test_source_database_corners.py` |
| Context external projection | Delivery HTTP/MCP tests |
| Revision/quality/verified membership | Guarded Query와 Assurance consumer/golden tests |

DB privilege, source epoch/CAS 또는 reader trust boundary를 바꾸면 전체 integration gate를 실행한다.
완료 전 [활성 개발 지침](../../development-guidelines.md#tests)의 `ruff`, `mypy`, full pytest도 실행한다.

## 집중해서 읽을 범위

먼저 이 문서와 [module index](../README.md)를 읽고 작업 종류에 맞는 한 행만 확장한다.

| 작업 | 추가로 읽을 code, decision과 test |
|---|---|
| Catalog/introspection/key/index | `metadata/catalog.py`, `metadata/models.py`, `test_catalog.py`, [ADR 0008](../../decisions/0008-physical-key-and-index-disclosure.md) |
| Context/disclosure/retrieval | `metadata/service.py`, `metadata/relevance.py`, related tests, [ADR 0009](../../decisions/0009-question-scoped-column-disclosure.md), [ADR 0010](../../decisions/0010-revision-scoped-retrieval-index.md) |
| Revision/core codec/stale | `metadata/revision.py`, `metadata/store.py`, `test_revision.py`, `test_metadata_codec.py`, [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md) |
| Managed store/rollback | `managed/metadata_store.py`, Control Plane README, `test_metadata_store.py`, [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md) |
| Quality/verified membership | `metadata/quality_level.py`, `test_quality_level.py`, [ADR 0011](../../decisions/0011-metadata-quality-level-publish-gate.md), Assurance interface |
| Reader/resource/launch policy | `source_catalog/reader_policy.py`, `metadata/catalog.py`, source DB corner tests, [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md) |

MCP SDK internals, source admin HTTP parsing, Control DB table/SQL와 query cursor internals는 위 interface나
승인 대상 의미를 바꾸지 않는 한 읽을 필요가 없다.
