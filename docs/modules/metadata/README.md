# Metadata Module

Status: Physical package boundary active

## 목적

### 30초 요약

Metadata는 Source Catalog가 제공한 source에 최소 권한으로 접속해 PostgreSQL `pg_catalog`를 읽고,
검증된 immutable snapshot과 질문별 context를 만든다. Table/column comment, PostgreSQL type과
precision/scale, key/index 같은 physical metadata와 Git YAML semantic overlay를 결합한다.

Snapshot은 process-local bounded cache에만 두며 Control DB나 persisted metadata store는 없다. Cache가
없거나 stale/drift가 확인되면 현재 source와 DB에서 다시 준비하고 불일치는 fail-closed한다.

## 소유 책임

- Catalog relation/column/key/index/type/comment DTO와 PostgreSQL collector
- Comment sanitation, catalog bounds와 allowed schema/relation-kind 검증
- Metadata revision identity와 immutable `PreparedMetadata`
- Question relevance ranking, bounded context, ambiguity/answerability projection
- Process-local cache, single-flight refresh와 invalidation
- Source minimum quality level 판정

## 소유하지 않는 책임

- Source YAML parsing, connection secret ownership과 reader policy definition
- SQL AST/function/operator/result OID policy와 query execution
- HTTP/MCP request/error rendering
- Verified expectation configuration/result comparison
- Persisted metadata snapshot, rollback/resume/generation 또는 resource observation

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`metadata/models.py`](../../../src/query_man/metadata/models.py) | Catalog DTO, prepared snapshot와 `CatalogProvider` |
| [`metadata/catalog.py`](../../../src/query_man/metadata/catalog.py) | PostgreSQL catalog collection, sanitation와 structural validation |
| [`metadata/service.py`](../../../src/query_man/metadata/service.py) | Prepare/cache/context/quality application behavior |
| [`metadata/relevance.py`](../../../src/query_man/metadata/relevance.py) | Question별 deterministic ranking |
| [`metadata/revision.py`](../../../src/query_man/metadata/revision.py) | Canonical metadata revision |
| [`metadata/quality_level.py`](../../../src/query_man/metadata/quality_level.py) | L0/L1/L2 assessment |
| [`test_catalog.py`](../../../tests/test_catalog.py), [`test_metadata.py`](../../../tests/test_metadata.py), [`test_revision.py`](../../../tests/test_revision.py), [`test_relevance.py`](../../../tests/test_relevance.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`CatalogColumn`, `CatalogRelation`, `CatalogSnapshot`, `PreparedMetadata`와 `CatalogProvider`가 Metadata의
public Python interface다. `MetadataService.get_context`, `get_published`, `invalidate`, `close`는
Runtime/Delivery/Guarded Query가 소비하는 application/lifecycle capability다.

Catalog column은 DB가 보고한 name, ordinal, data type, nullable, default, comment와 numeric/character
형상 정보를 보존한다. `obj_description`/`col_description`로 읽은 comment는 control character와 bounded
size policy를 적용한다. Comment는 업무 설명에 도움을 주지만 PII 허용, access control 또는 query
safety authority가 아니다. PII 여부 같은 policy는 reviewed source/database policy에서 강제해야 하며
실제 개인정보나 secret을 comment에 넣지 않는다.

Metadata revision은 source policy, semantic overlay, validated catalog와 Guarded Query의 immutable SQL
policy descriptor를 canonicalize한 SHA-256 identity다. 업무 row 변화만으로는 바뀌지 않지만 relation,
column, type, precision/scale, comment와 policy가 바뀌면 달라질 수 있다. Revision material 변경은
policy/compatibility identity다.

Cache는 process-local이며 bounded TTL, single-flight refresh와 source epoch로 stale publish를 막는다.
Invalidate 뒤 진행 중이던 old refresh가 새 cache value를 덮지 못한다. 실패한 refresh는 이전 snapshot을
무기한 authoritative하게 만들지 않는다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | `SourceReader`, `SourceProfile`, reader connection/session policy | YAML이나 credential 의미를 다시 정의하지 않음 |
| Guarded Query | SQL policy revision/canonical material | Query implementation private state를 읽지 않음 |
| Runtime | Operations sink와 concrete connection pool composition | Ready 전에 required capability를 확인함 |
| Assurance | Verified revision membership | Expected result 판정은 Assurance가 소유함 |

## 불변조건

- Catalog는 allowed schema/relation kind와 metadata relation/column/response limit을 넘기지 않는다.
- PostgreSQL 18, server/client UTF-8와 reader session policy를 query 전에 확인한다.
- Catalog 내부 오류, SQL literal, credential과 DSN을 public response/log에 노출하지 않는다.
- Snapshot과 nested graph는 immutable하며 caller alias가 cache를 바꾸지 못한다.
- Context는 current source epoch와 revision에서만 반환한다.
- Comment/type/precision-scale 정보는 revision에 일관되게 반영하며 comment만으로 PII 안전을 추정하지 않는다.
- Persisted snapshot이나 managed rollback/fallback은 없다.

## 모듈 내부 변경

Public DTO/revision/context semantics를 보존하는 SQL row assembly helper, ranking implementation, cache lock
구현과 test fixture 정리는 module 내부 변경이다.

## 사용자 승인이 필요한 경계 변경

- Catalog/Prepared DTO, `CatalogProvider`와 `MetadataService` public capability
- Catalog query가 수집하는 comment/type/key/index와 sanitation 의미
- Metadata revision canonical material/encoding/hash
- Cache freshness, invalidate, single-flight와 fail-closed lifecycle
- Context response shape, relevance/quality policy와 limits
- Persisted metadata store 또는 external authority 재도입

## 검증

```bash
uv run pytest tests/test_catalog.py tests/test_metadata.py tests/test_revision.py \
  tests/test_relevance.py tests/test_quality_level.py
```

Catalog SQL이나 DB type 경계를 바꾸면 `tests/test_source_database_corners.py`와 integration lane도
실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Catalog/comment/type 수집 | `metadata/catalog.py`, `models.py`, `test_catalog.py` |
| Revision | `metadata/revision.py`, SourceProfile, SQL policy descriptor, `test_revision.py` |
| Context/cache/invalidation | `metadata/service.py`, `relevance.py`, `test_metadata.py`, `test_relevance.py` |
| Quality level | `metadata/quality_level.py`, Assurance verified interface, `test_quality_level.py` |
| DB edge | `test_source_database_corners.py`, reader policy direct path |
