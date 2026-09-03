# Query Man Module Index

Query Man은 하나의 repository, wheel과 process를 유지하는 modular monolith입니다. Module은 독립 배포나
임의 구현 교체 단위가 아니라 필요한 코드와 owner를 빠르게 찾는 경계입니다.

## Primary module 선택

| 바꾸려는 것 | Primary module | 안내 |
|---|---|---|
| Source package, reader, budget | Source Catalog | [README](source-catalog/README.md) |
| PostgreSQL catalog, context, metadata revision | Metadata | [README](metadata/README.md) |
| SQL allowlist, execution limit, result, cancel | Guarded Query | [README](guarded-query/README.md) |
| HTTP, authentication, authorization | Delivery | [README](delivery/README.md) |
| Config, composition, readiness, shutdown, CLI | Runtime | [README](runtime/README.md) |
| Security/integration/container/load gate | Assurance | [README](assurance/README.md) |

Primary module README의 `30초 요약`과 `집중해서 읽을 범위`를 따라 owner leaf file, 직접 consumer와 root
test만 읽습니다. Package `__init__.py`는 marker-only이며 interface re-export를 제공하지 않습니다.

## 허용 의존

```text
Runtime composition -> Source Catalog, Metadata, Guarded Query, Delivery
Delivery            -> Source Catalog, Metadata, Guarded Query
Metadata            -> Source Catalog, Guarded Query policy identity
Guarded Query       -> Source Catalog, Metadata published snapshot
Assurance tests     -> 모든 production module의 public path
```

Runtime만 concrete production implementation을 조립합니다. Consumer는 owner leaf module의 public
entrypoint를 사용하고 underscore private symbol, provider storage와 YAML을 직접 읽지 않습니다. Error
rendering은 Delivery, error 발생 조건은 owner module이 책임집니다.

## 코드 ownership

경로는 `src/query_man/` 기준입니다.

- Shared: `__init__.py`, `errors.py`
- Source Catalog: `source_catalog/__init__.py`, `source_catalog/models.py`,
  `source_catalog/reader_policy.py`, `source_catalog/registry.py`
- Metadata: `metadata/__init__.py`, `metadata/models.py`, `metadata/catalog.py`,
  `metadata/revision.py`, `metadata/service.py`
- Guarded Query: `guarded_query/__init__.py`, `guarded_query/query.py`,
  `guarded_query/result_encoding.py`, `guarded_query/sql_validation.py`
- Delivery: `delivery/__init__.py`, `delivery/access.py`, `delivery/authentication.py`,
  `delivery/gateway.py`, `delivery/http_validation.py`, `delivery/app.py`
- Runtime: `runtime/__init__.py`, `runtime/config.py`, `runtime/composition.py`,
  `runtime/operations.py`, `runtime/operator_shell.py`, `runtime/server.py`
- Assurance: `assurance/__init__.py`

주요 non-Python owner는 다음과 같습니다.

| 경로 | Owner |
|---|---|
| `config/sources/`, `config/budget-profiles.yaml` | Source Catalog |
| `config/access-policies*.yaml` | Delivery |
| `config/security-evaluation.yaml`, root `tests/` | Assurance와 behavior owner |
| `Dockerfile`, `compose*.yaml`, `.env*.example` | Runtime; container는 Assurance consumer |
| `.github/workflows/ci.yml`, `scripts/verify-container.sh` | Assurance shared gate |
| `pyproject.toml`, `uv.lock` | Runtime/dependency shared artifact |

`AGENTS.md`, development guidelines, 이 index, decision/TODO/verification index와 CI workflow는 shared
single-writer artifact입니다.

## Source 추가 영향

Repository에는 `config/sources/<source-id>/source.yaml`과 `views.sql` 두 파일만 추가합니다. Generic
validation이 package를 발견하므로 source ID를 code, test, 문서나 별도 registry에 등록하지 않습니다.
DB/data owner review와 DBA apply는 repository 변경과 별도 승인입니다. 자세한 절차는
[Source extension checklist](../source-extension-checklist.md)를 따릅니다.

## 변경 승인

Allowed dependency 안의 Python shape/signature 정리와 provider/consumer 동시 refactor는 외부 의미를
보존하면 일반 구현 변경입니다. External API, persisted format, policy/revision, safety/lifecycle,
ownership/composition 또는 protected procedure 의미를 바꾸면 구현 전에
[승인 규칙](../development-guidelines.md#승인-규칙)을 따릅니다. 실제 protected action은 repository 승인과
별도의 access/target/stop/change-record 승인이 필요합니다.

## 검증

Focused test는 feedback용이며 완료 전 전체 gate를 실행합니다.

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```
