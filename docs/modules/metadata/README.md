# Metadata Module

Status: Physical package boundary active

## 목적

### 30초 요약

Metadata는 최소 권한 reader로 PostgreSQL catalog를 읽고, source의 공개 view가 안전하게 사용할 수 있는
상태인지 직접 검사한 뒤 immutable revision과 질문별 bounded context를 만든다. 별도 품질 단계나
source별 예상 결과 registry는 없다.

모든 reader-visible view는 comment 첫 줄에 source와 `view_contract_version` marker를 가져야 한다.
Metadata는 marker를 context에서 제거하고 사람용 설명만 공개한다. 같은 process에서 이미 본 같은
contract version의 구조가 바뀌면 stale snapshot으로 우회하지 않고 fail-closed한다.

## 소유 책임

- Bounded PostgreSQL catalog/structure 수집과 type/comment 정규화
- View contract marker syntax, source/version과 security option 수집
- 모든 발견 view의 semantic metadata 직접 admission
- Immutable `metadata_revision`과 process-local view structure signature
- Question relevance ranking, relation/column truncation과 context byte limit
- Cache, refresh, invalidation, stale와 deterministic validation failure 구분

## 소유하지 않는 책임

- Source package/manifest parsing, budget schema와 reader credential resolution
- `views.sql` 해석·실행 또는 Git/live SQL 동일성 증명
- SQL AST validation, query plan/execution/result encoding
- HTTP/MCP envelope와 authentication/authorization
- DB owner의 no-PII 확인 또는 protected DDL inventory/freeze 대행

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`metadata/models.py`](../../../src/query_man/metadata/models.py) | Catalog snapshot/relation/column/key/index DTO |
| [`metadata/catalog.py`](../../../src/query_man/metadata/catalog.py) | Reader pool, bounded catalog SQL, marker/comment parsing |
| [`metadata/service.py`](../../../src/query_man/metadata/service.py) | Direct admission, cache/refresh와 question context |
| [`metadata/relevance.py`](../../../src/query_man/metadata/relevance.py) | Deterministic relevance index와 bounded selection |
| [`metadata/revision.py`](../../../src/query_man/metadata/revision.py) | Metadata revision와 structure signature canonicalization |
| [`test_catalog.py`](../../../tests/test_catalog.py), [`test_metadata.py`](../../../tests/test_metadata.py), [`test_revision.py`](../../../tests/test_revision.py), [`test_relevance.py`](../../../tests/test_relevance.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`PostgresCatalog.load(source)`는 allowed schema와 view kind 안에서 현재 reader가 schema usage,
relation/column SELECT를 가진 object만 수집한다. Catalog transaction은 read-only이고 statement/lock/
transaction/resource budget을 적용한다. PostgreSQL 18, server/client UTF-8, domain column 금지와 reviewed
TLS/session policy를 SQL 전에 확인한다.

View comment 첫 줄은 다음 exact marker다.

```text
query-man:source=<source-id>;view-contract=<positive integer>
```

반드시 개행 뒤 non-empty description이 이어진다. Invalid marker는 catalog validation error이며 marker는
model context나 relation description에 포함하지 않는다.

`MetadataService.get_published(source_id)`는 valid·ready·non-partial snapshot과 revision을 제공한다.
`get_context(source_id, question, max_objects)`는 질문에 맞는 relation, column, join, ambiguity,
`metadata_revision`과 `sql_policy_revision`을 반환한다. `max_context_columns_per_relation` 기본값은 40이며
각 relation은 `column_count`, `returned_column_count`, `columns_truncated`를 포함한다.

Publish 전 다음을 직접 요구한다.

- 발견된 모든 view와 semantic relation entry의 exact one-to-one coverage
- 모든 view의 grain과 semantic 또는 DB relation description
- Event/comment/population role의 default time column
- Grain/default time/alias/value hint/measure/business predicate의 실제 column 존재
- Approved join relation·column 존재와 양쪽 type 일치
- 모든 view marker의 source/version 일치와 RLS source의 security-invoker 조건

`metadata_revision`은 source/budget/semantic material, view contract version, catalog relation·column·comment,
definition digest와 지원하는 security option을 포함한다. Client freshness token이며 Git SQL 승인 hash가
아니다. 별도 structure signature는 relation set/name/kind, definition digest, output column name/order/
type/nullability와 security option만 추적한다. Description/semantic/budget 변화는 contract version 없이도
metadata revision을 정상 회전시킬 수 있다.

## 소비 인터페이스와 전제

| Provider/consumer | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | Source, semantic overlay, budget와 reader policy | Manifest/package는 이미 strict validation됨 |
| Guarded Query | Published metadata revision와 allowed objects | Query가 exact current metadata/SQL revision을 제시해야 함 |
| Delivery | Question context와 metadata domain errors | Marker와 DB 내부 오류를 공개하지 않음 |
| Runtime | Catalog/service concrete composition와 lifecycle | Admin credential이나 DDL path를 주입하지 않음 |

## 불변조건

- Catalog는 allowed schema와 view kind, reader privilege와 hard row/byte/time bound 안에서만 읽는다.
- Public view는 exact marker와 non-empty human description을 갖고 active source/version과 일치한다.
- 발견 view와 semantic relation entry는 exact coverage를 이루며 참조 column/join type이 유효하다.
- Direct admission 위반은 deterministic `METADATA_UNAVAILABLE`이며 stale cache로 fallback하지 않는다.
- 같은 process에서 같은 source/version의 view 구조 변화는 version 누락 drift로 거부한다.
- Column inventory는 PostgreSQL에서 동적으로 읽고 `source.yaml`에 exhaustive list/hash를 복제하지 않는다.
- Marker, credential, SQL literal과 내부 database 오류를 context나 일반 log에 노출하지 않는다.
- Revision mismatch는 executor 전에 fail-closed하며 `metadata_revision`과 `sql_policy_revision` 의미를 유지한다.

## 모듈 내부 변경

위 admission/revision/cache 의미를 보존하는 SQL formatting, builder, ranking index와 immutable DTO shape
정리는 내부 변경이다. Catalog query field 변경은 service/revision과 focused tests를 함께 검토한다.

## 사용자 승인이 필요한 경계 변경

- Context wire field, metadata revision material/canonicalization과 mismatch 의미
- View marker syntax, contract version 또는 structure signature policy
- Direct admission 조건이나 deterministic failure의 stale fallback 여부
- Catalog visibility, type/domain/TLS/session policy와 resource bound
- Persisted metadata store, hot reload 또는 Runtime DDL capability 도입

## 검증

```bash
uv run pytest tests/test_catalog.py tests/test_metadata.py tests/test_revision.py \
  tests/test_relevance.py
uv run pytest tests/test_database_integration.py -m integration
```

DB/catalog 변경은 integration safety kernel, container와 revision mismatch tests도 실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Catalog/marker/security | `catalog.py`, `models.py`, `test_catalog.py` |
| Admission/cache/stale | `service.py`, direct consumers, `test_metadata.py` |
| Revision/signature | `revision.py`, Guarded Query mismatch path, `test_revision.py` |
| Relevance/context limits | `relevance.py`, response builder, `test_relevance.py` |
| Reader connection | Source reader policy, catalog pool, database integration tests |
