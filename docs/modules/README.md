# Query Man Module Boundaries

Status: Active development governance

## 목적

Query Man은 하나의 repository와 하나의 deployable process를 유지하는 modular monolith다. 이 문서는
독립적으로 이해하고 변경할 수 있는 여섯 논리 module의 소유권, 허용 의존 방향과 변경 승인 절차를
정의한다. 논리 module은 별도 배포나 임의 구현 교체 단위가 아니며 package graph를 완전한 DAG로 만드는
것도 목표가 아니다.

Python 구현은 `src/query_man` 아래 `source_catalog`, `metadata`, `guarded_query`, `delivery`, `runtime`,
`assurance` 여섯 physical package로 나뉜다. Package `__init__.py`는 marker-only이며 interface를
재수출하지 않는다. 제거된 managed/Control Plane 구현은 현재 module이 아니다. 현재 authority와 제거
경계는 [ADR 0030의 retired managed capability](../decisions/0030-git-reviewed-yaml-source-authority.md#retired-managed-capability)를
따르고, 과거 구현·검증은 [Git 기록 안내](../verification/README.md)의 archive commit에서만 확인한다.

## 3분 시작법

1. 아래 표에서 primary module 하나를 고른다.
2. 해당 README의 `30초 요약`과 `집중해서 읽을 범위`를 따른다.
3. Allowed dependency 안에서 owner leaf module의 public API를 사용하고 underscore private 구현은 피한다.
4. External API/wire, persisted format, policy, lifecycle, ownership, 운영 절차 의미나 그 의미에 닿는
   interface가 바뀌면 구현을 멈추고 [승인 절차](#승인-대상-변경-절차)를 따른다.

| 바꾸려는 것 | Primary module |
|---|---|
| Source YAML, reader, budget, semantic overlay | Source Catalog |
| DB 구조 수집, context 선택, metadata revision | Metadata |
| SQL 허용 범위, 실행 제한, 결과 encoding, cancel | Guarded Query |
| HTTP/MCP 요청·응답, 인증·인가 | Delivery |
| Process 설정, 조립, readiness, `qm` operator CLI | Runtime |
| 품질 평가, verified result, offline 검증 | Assurance |

## 모듈 목록

| Module | 한 문장 책임 | 개요 | 작업별 읽기 |
|---|---|---|---|
| Source Catalog | Git에서 검토한 source/budget YAML을 strict validation해 immutable runtime profile로 만든다. | [source-catalog](source-catalog/README.md) | [필요한 code·test](source-catalog/README.md#집중해서-읽을-범위) |
| Metadata | PostgreSQL catalog를 검증된 revision과 질문별 context로 만든다. | [metadata](metadata/README.md) | [필요한 code·test](metadata/README.md#집중해서-읽을-범위) |
| Guarded Query | SQL을 검증하고 read-only resource limit 안에서 실행·취소·rollback한다. | [guarded-query](guarded-query/README.md) | [필요한 code·test](guarded-query/README.md#집중해서-읽을-범위) |
| Delivery | Caller를 인증·인가하고 같은 application service를 HTTP와 MCP로 제공한다. | [delivery](delivery/README.md) | [필요한 code·test](delivery/README.md#집중해서-읽을-범위) |
| Runtime | 구현을 조립하고 configuration, lifecycle, health와 operator CLI를 관리한다. | [runtime](runtime/README.md) | [필요한 code·test](runtime/README.md#집중해서-읽을-범위) |
| Assurance | Metadata 품질과 verified query 결과를 offline/runtime acceptance로 검증한다. | [assurance](assurance/README.md) | [필요한 code·test](assurance/README.md#집중해서-읽을-범위) |

## 현재 공통 기준선

[ADR 0025](../decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A`는 current serving
범위를, [ADR 0030](../decisions/0030-git-reviewed-yaml-source-authority.md)은 source authority를
정한다.

- `config/sources/*.yaml`, `config/budget-profiles.yaml`, `config/verified-queries.yaml`의 Git-reviewed
  version이 source inventory와 관련 설정의 유일한 authority다.
- Runtime은 `development-issues`, `market-voc` 두 non-RLS source와 단일 replica를 조립한다.
- Runtime hot reload와 관리 API는 없다. 변경은 pull request review, 검증, 배포/재시작을 거쳐 반영한다.
- `qm source list|show|validate`는 local repository YAML을 사람이 읽을 수 있게 확인하는 read-only CLI다.
- Source Catalog는 RLS manifest를 거부하며 PostgreSQL 18/UTF-8 reader 정책과 manifest v3의 명시적
  `disable`/`require`/`verify-full` TLS mode를 제공한다.
- Metadata와 Guarded Query는 connection/session 정책을 fail-closed로 확인한다.
- Query Man은 개인정보(PII)를 탐지·분류·마스킹하지 않는다. DB owner가 개인정보를 제거했다고
  확인한 reviewed curated view만 Source Catalog에 등록한다.
- Guarded Query는 SQL policy v3와 exact seven result OID를 적용한다.
- DSN password 같은 secret은 YAML/Git에 저장하지 않고 environment로 resolve한다.

RLS attestation, broader lossless encoding, COST와 TRACE는 parked 주제다. 정확한 재개 조건은
[Active TODO](../development-todo.md#현재-일정에-없는-일)에만 둡니다.

## 허용 의존 방향

아래 `->`는 왼쪽 module이 오른쪽 module이 소유한 capability를 소비한다는 뜻이다. 중요한 동작과
entrypoint는 owner 문서에 설명하지만 모든 public Python symbol을 열거하지 않는다.

```text
Delivery -> Source Catalog(read), Metadata, Guarded Query, Runtime operations interface
Metadata -> Source Catalog(read), reader connection/session policy,
            Guarded Query immutable SQL-policy descriptor, Runtime operations interface
Guarded Query -> Source Catalog(read and reader policy), Metadata published revision,
                 Runtime operations interface
Assurance -> Source Catalog(read), Metadata, Guarded Query
```

Runtime composition과 operations sink, Metadata/Guarded Query의 revision-policy 연결은 허용된 reciprocal
dependency다. Leaf Python import는 비순환으로 유지하지만 package 자체의 독립 추출이나 모든 provider의
대체 가능성을 보장하지 않는다.

Concrete implementation 조립은 다음 composition root만 소유한다.

```text
Runtime --compose--> production server implementation and lifecycle
Assurance offline CLI --compose--> offline acceptance implementation
```

Source onboarding Skill은 Source Catalog가 소유하는 plan-only workflow다. Repository 문서를 읽어
YAML pull request와 DBA 작업 handoff를 만들 수 있지만 credential 접근, 파일 변경, DB DDL, API 호출,
배포 승인 또는 safety validation을 대신하지 않는다.

다음 의존은 금지한다.

- Delivery가 catalog 또는 PostgreSQL query adapter를 직접 호출하는 것
- Metadata나 Guarded Query가 source authority 파일을 직접 다시 해석하는 것
- Runtime과 Assurance CLI 이외의 위치에서 concrete implementation을 조립하는 것
- Source별 차이를 `source_id` Python branch로 구현하는 것
- Prompt, Skill, comment 또는 caller 관례를 safety enforcement policy로 사용하는 것
- 삭제된 Control DB, managed package, admin API 또는 runtime fallback을 되살리는 것

## 현재 코드·설정 ownership map

| 현재 파일 또는 영역 | Owner | 주의점 |
|---|---|---|
| `source_catalog/models.py`, `source_catalog/registry.py`, `source_catalog/reader_policy.py` | Source Catalog | Immutable source DTO, strict YAML parser, `SourceReader`, reader connection/session policy |
| `metadata/models.py`, `metadata/catalog.py`, `metadata/service.py`, `metadata/relevance.py`, `metadata/revision.py`, `metadata/quality_level.py` | Metadata | Catalog/context/revision/quality; persisted metadata store는 없음 |
| `guarded_query/query.py`, `guarded_query/sql_validation.py`, `guarded_query/diagnostics.py`, `guarded_query/result_encoding.py` | Guarded Query | SQL validation, admission, execution/cancel/rollback와 canonical result encoding |
| `delivery/access.py`, `delivery/authentication.py`, `delivery/diagnostics.py`, `delivery/gateway.py`, `delivery/mcp_server.py`, `delivery/http_validation.py`, `delivery/app.py` | Delivery | Caller/authentication, application facade와 HTTP/MCP wire |
| `runtime/config.py`, `runtime/composition.py`, `runtime/server.py`, `runtime/operations.py`, `runtime/diagnostic_capture.py`, `runtime/operator_shell.py`, `runtime/operator_backend.py` | Runtime | Environment, production composition/lifecycle, safe operations와 local YAML CLI/backend |
| `assurance/quality.py`, `assurance/verified.py`, `assurance/cli.py` | Assurance | Offline quality/verified artifact와 concrete verification composition |
| `errors.py` | Shared interface; file은 shared single-writer | `AppError` base는 shared, domain error 발생 의미는 provider, external envelope는 Delivery가 소유 |
| `config/sources/`, `config/budget-profiles.yaml` | Source Catalog | Git-reviewed YAML source authority와 versioned budget |
| `config/access-policies*.yaml` | Delivery | Caller/source/scope policy |
| `config/quality-evaluation.yaml`, `config/verified-queries.yaml`, `config/security-evaluation.yaml` | Assurance | Versioned acceptance data; source membership은 Git YAML과 일치해야 함 |
| `Dockerfile`, `compose.yaml`, `.env.example` | Runtime | Serving artifact와 environment contract |
| `scripts/verify-container.sh` | Assurance; Runtime consumer | Container acceptance |
| `.github/workflows/ci.yml`, `.github/workflows/mcp-soak.yml` | Assurance | Repository gate와 soak; shared transition artifact |
| `skills/query-man-text-to-sql/` | Delivery workflow | Query Man MCP만 사용하고 DB에 직접 접속하지 않음 |
| `skills/query-man-source-onboarding/` | Source Catalog workflow | YAML-only plan handoff; mutation 권한 없음 |
| `pyproject.toml` package/dependency/entrypoint sections | Runtime; shared single-writer | Package와 CLI entrypoint 계약 |
| `uv.lock` | Shared dependency lock | `pyproject.toml` dependency와 함께 변경 |

Python file별 primary owner는 다음과 같다. 경로는 `src/query_man/` 기준이다.

- Shared: `__init__.py`, `errors.py`
- Source Catalog: `source_catalog/__init__.py`, `source_catalog/models.py`,
  `source_catalog/reader_policy.py`, `source_catalog/registry.py`
- Metadata: `metadata/__init__.py`, `metadata/catalog.py`, `metadata/models.py`,
  `metadata/quality_level.py`, `metadata/relevance.py`, `metadata/revision.py`, `metadata/service.py`
- Guarded Query: `guarded_query/__init__.py`, `guarded_query/diagnostics.py`,
  `guarded_query/query.py`, `guarded_query/result_encoding.py`, `guarded_query/sql_validation.py`
- Delivery: `delivery/__init__.py`, `delivery/access.py`, `delivery/app.py`,
  `delivery/authentication.py`, `delivery/diagnostics.py`, `delivery/gateway.py`,
  `delivery/http_validation.py`, `delivery/mcp_server.py`
- Runtime: `runtime/__init__.py`, `runtime/composition.py`, `runtime/config.py`,
  `runtime/diagnostic_capture.py`, `runtime/operations.py`, `runtime/operator_backend.py`,
  `runtime/operator_shell.py`, `runtime/server.py`
- Assurance: `assurance/__init__.py`, `assurance/cli.py`, `assurance/quality.py`,
  `assurance/verified.py`

Root `tests/`는 구현 owner가 검증한다. `AGENTS.md`, `tests/helpers.py`,
`tests/test_documentation.py`, `docs/development-todo.md`, `docs/decisions/README.md`,
`docs/verification/README.md`처럼 여러 owner가 함께 사용하는 artifact는 coordinating agent가
single-writer로 편집한다.

## 제공 인터페이스와 소유 경계

각 module README의 `제공 인터페이스와 소유 경계`는 provider가 보장하는 중요한 동작과 안정된
entrypoint를 설명한다. Public Python symbol 전체를 inventory처럼 등록하지 않으며, consumer는 allowed
dependency 안에서 package marker가 아닌 owner leaf module에서 직접 import한다. Underscore private
symbol과 provider의 concrete storage에는 의존하지 않는다.

Root `errors.py`는 interface-only shared artifact다. Domain error의 발생 조건은 provider module이,
HTTP/MCP status/code/message/details rendering은 Delivery가 소유한다. Metadata와 Guarded Query 사이의
published-revision/SQL-policy 연결, 여러 module이 쓰는 Runtime operations sink는 허용된 cross-cutting
dependency이며 package 독립 추출이나 다른 private implementation 접근 권한을 만들지 않는다.

Error symbol ownership은 다음과 같다.

- Shared: `AppError`
- Delivery: `OperatorRequiredError`, `InsufficientScopeError`
- Source Catalog: `SourceNotFoundError`
- Metadata: `MetadataUnavailableError`, `MetadataRevisionMismatchError`
- Guarded Query: `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`,
  `QueryTimeoutError`, `QueryUnavailableError`, `QueryNotFoundError`

## 새 데이터베이스 추가 시 영향

새 PostgreSQL source는 DB 연결 정보가 있다는 이유만으로 자동 등록하지 않는다. Source onboarding
Skill이 plan을 만들고 사람이 승인한 다음 다음 artifact를 한 Git change set으로 review한다.

1. DBA가 개인정보를 제거한 curated view, 최소 권한 reader와 PostgreSQL `COMMENT ON` 기반
   table/column 설명을 준비하고 exact view 공개 범위를 확인한다. Comment에는 credential이나 실제
   개인정보 값을 넣지 않는다.
2. `config/sources/<source-id>.yaml`에 source, connection environment key, allowed schema/relation kind,
   budget, semantic overlay와 provenance를 추가한다. Password 값은 외부 secret store/environment에 둔다.
3. 필요하면 `config/budget-profiles.yaml`, `config/verified-queries.yaml`과 quality/security case를 같은
   review에 갱신한다.
4. `qm source validate`와 focused/full acceptance를 통과하고 배포/재시작한다. Runtime은 시작 시 YAML과
   DB catalog를 다시 읽으며 잘못된 설정, drift, RLS 또는 reader policy 불일치를 fail-closed한다.

Source YAML schema/revision, metadata revision material, verified result identity, SQL/reader policy나
배포 절차를 바꾸면 아래 승인 분류도 함께 검토한다.

## 승인 대상 변경 절차

Allowed dependency map 안의 내부 Python shape/signature와 public leaf symbol은 provider와 직접 consumer를
같은 change set에서 수정·검증할 수 있다. Module interface는 중요한 entrypoint의 호출 단위
input/output/domain-error semantics를 설명하며, 모든 symbol을 문서 목록에 등록하거나 실제 교체 요구가
없는 구현에 Protocol을 만들 필요는 없다. 다음 의미 변경은 일반 구현 요청으로 승인된 것으로 보지 않는다.

- `External API/wire format`: HTTP/MCP/CLI wire, authentication과 public error
- `Persisted/versioned format`: YAML/config schema와 version
- `Policy/compatibility identity`: metadata revision, result hash, allowlist와 canonical encoding
- `Safety/lifecycle invariant`: authorize, validate, admit, cancel, rollback, shutdown과 fail-closed outcome
- `Ownership/composition boundary`: allowed dependency와 composition ownership
- `Protected operational procedure`: protected inventory, DDL, secret 설치, 배포, cutover와 rollback

변경이 필요하면 현재/제안 의미, 이유, provider와 consumer, compatibility/migration/rollback,
보안·데이터 손실 영향, 검증 계획을 제시하고 사용자의 명시적 승인을 받은 뒤 code·문서·테스트를 같은
change set에서 갱신한다. Protected environment의 실제 action은 repository 승인과 별도의 실행 승인이
필요하다. Protected evidence는 승인된 환경 기록 시스템에 append하고, repository history는
[Git 기록 안내](../verification/README.md)의 archive policy를 따른다.

위 의미를 보존하는 additive helper, internal/public Python symbol 정리, file move와 provider/consumer
동시 refactor는 별도 사용자 승인을 요구하지 않는다. 중요한 behavior/entrypoint와 runnable test만
owner 문서에 유지한다.

## 구조 변경 판단

Module은 사람과 agent가 독립적으로 이해·변경하는 단위이지 독립 배포·교체 단위가 아니다. Folder나
package 수를 늘리는 것이 목표가 아니다. 같은 transaction·cleanup·실패 경로에 속하면 함께 두고,
실제로 다른 release, owner, 접근 권한이나 lifecycle이 확인될 때만 새 경계를 검토한다. 이름만 전달하는
facade, unused extension point, re-export용 `__init__.py`는 만들지 않는다.

## 검증

Module focused test는 빠른 feedback이며 전체 gate를 대체하지 않는다.

```bash
uv run ruff check .
uv run ruff check src/query_man/runtime --select C901 --config "lint.mccabe.max-complexity=19"
uv run mypy src
uv run pytest
```

DB catalog/query 경계를 바꾸면 관련 integration lane도 실행한다. Source authority 변경은 최소한
registry, revision, assurance CLI, operator CLI, Runtime configuration과 documentation test를 함께
확인한다.
