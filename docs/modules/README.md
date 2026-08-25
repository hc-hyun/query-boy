# Query Man Module Boundaries

Status: Active development contract

## 목적

Query Man은 하나의 repository와 하나의 deployable process를 유지하는 modular monolith다.
이 문서는 기능을 독립적으로 개발할 수 있도록 논리적 module의 소유권, 허용 의존 방향과
module 사이의 계약 변경 절차를 정의한다.

현재 Python 코드는 `src/query_man`의 평면 구조다. 아래 module directory는 개발 소유권과
계약을 먼저 고정하기 위한 문서 경계이며, 코드가 이미 물리적으로 package 분리되었다는 뜻이
아니다. 실제 파일 이동은 동작과 계약을 고정한 별도 refactoring으로 수행한다.

AI agent는 repository 전체를 기본적으로 읽을 필요가 없다. 작업 module의 문서, 해당 문서가
가리키는 현재 코드와 테스트, 소비하는 계약 및 관련 ADR만 읽는다. 변경하는 완전한 실행 흐름과
trust boundary가 다른 module로 넘어가면 필요한 계약·코드·테스트까지 읽기 범위를 확장한다.

## 모듈 목록

| Module | 한 문장 책임 | 계약 문서 |
|---|---|---|
| Source Catalog | Source 정의, budget, semantic 설정과 runtime source projection을 관리한다. | [source-catalog](source-catalog/README.md) |
| Metadata | PostgreSQL catalog를 검증된 revision과 질문별 context로 만든다. | [metadata](metadata/README.md) |
| Guarded Query | SQL을 검증하고 read-only resource limit 안에서 실행·취소·rollback한다. | [guarded-query](guarded-query/README.md) |
| Control Plane | Control DB와 source/metadata/verified lifecycle 상태 전이를 원자적으로 관리한다. | [control-plane](control-plane/README.md) |
| Delivery | Caller를 인증·인가하고 동일한 application contract를 HTTP와 MCP로 제공한다. | [delivery](delivery/README.md) |
| Runtime | 구현을 조립하고 configuration, lifecycle, health와 process 운영을 관리한다. | [runtime](runtime/README.md) |
| Assurance | Metadata 품질과 verified query 결과를 offline/runtime acceptance로 검증한다. | [assurance](assurance/README.md) |

## 승인 대기 중인 공통 계약

[Proposed ADR 0024](../decisions/0024-rls-policy-drift-attestation.md)의 `RLS-01-A`는 Metadata가
recursive RLS identity, exact policy normal/shared dependency, deterministic validation marker,
history-decode/current-serving gate와 snapshot/revision v2를 제공하고 Source
Catalog가 RLS pool startup client UTF8 및 no-SQL PostgreSQL-18/server/client UTF8 connection policy를
제공한다. Guarded Query가 두 provider를 lock-first transaction에서 소비하며 Control Plane, Delivery,
Runtime과 Assurance가 cutover/error/acceptance를 담당하는 정확한 제안이다. 이 encoding restriction은
client-encoding별 same Python identifier가 다른
relation을 resolve하는 것을 막는 RLS-only identity 경계다. Non-RLS pool의 startup encoding/session/result
의미와 broader public result/source-semantics encoding은 ADR 0020에 남긴다. 다만 아래 exact-profile
lifecycle은 모든 managed source transition에 적용한다. 아직 사용자에게 exact 범위가 승인되지 않았고 현재
module contract나 제품 구현이 아니다. 일반적인 plan/진행 승인은 이 경계를 열지 않는다.

같은 RLS target은 기존 immutable `SourceProfile` exact equality를 별도 token 없이 runtime execution
identity로 재사용한다. Control invalidator가 Query를 먼저 fence/drain하고 Catalog active
load/observation lease까지 drain한 뒤 Metadata cache, registry 순으로 전환한다. Managed routed profile은
registry에서만 얻고 provider exact-profile fence를 통과해야 하므로 retired profile의 pool 재생성과
partial-failure route가 닫힌다. 각 최대 1초인 fixed three-phase cleanup도 이 lifecycle의 일부이며 ADR 0024 exact
승인 범위이지 current invalidate 계약이 아니다. 이 공용 managed lifecycle은 non-RLS transition에도
적용하지만 PostgreSQL-18/UTF8, graph와 snapshot v2 조건은 RLS-only다.
Registry upsert/remove의 successful return이 단일 transition commit point다. Applied-generation/status
bookkeeping은 post-commit reconciliation이다. Pre-fence candidate failure는 current route를 유지하고,
Query fence 뒤 commit 전 failure만 old/new route를 닫는다. Commit 뒤 bookkeeping/probe failure는
only-new/removed projection과 unavailable/failed status를 유지하고 external probe cancellation은 fabricated
failure 없이 재전파한다.

[Proposed ADR 0020](../decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 `ENC-01-A`는 Guarded
Query result loader/encoder와 SQL/result policy, Source Catalog reader settings, Metadata
source-semantics fingerprint 및 cumulative snapshot/revision v3를 하나의 계약으로 묶는다. Delivery의
public row/byte와 Assurance/Control verified result hash, Runtime cutover가 직접 소비한다. RLS source의
v3 attestation은 ADR 0024 shape/semantics를 current policy/live graph에서 fresh 계산한다. 이 계약도 exact
사용자 승인 전에는 current module contract나 구현이 아니다.

## 허용 의존 방향

아래 화살표는 왼쪽 module이 오른쪽 module의 공개 계약을 소비한다는 뜻이다.

```text
Delivery -> Source Catalog(read), Metadata, Guarded Query,
            Control Plane administration use cases, Runtime operations contract
Metadata -> Source Catalog(read), reader-session safety contract,
            Guarded Query immutable SQL-policy descriptor, Runtime operations contract
Guarded Query -> Source Catalog(read), Metadata published-revision contract,
                 Runtime operations contract
Control Plane -> Source Catalog(write), Metadata public use cases,
                 Guarded Query public use case, Assurance verified contract,
                 Runtime operations contract
Assurance -> Source Catalog(read), Metadata, Guarded Query
Runtime -> every production implementation, for server composition and lifecycle only
```

이 graph는 production Python/runtime dependency다. Source Catalog 소유의 plan-only onboarding
workflow에는 다음 문서 소비 방향을 별도로 허용한다.

```text
Source Catalog onboarding workflow (plan-only)
  -> Control Plane public administration contract,
     Delivery public admin transport contract,
     Assurance onboarding acceptance
```

이 방향은 공개 문서를 읽어 human handoff를 작성하는 데만 쓰며 Python import, API 호출, concrete
composition 또는 production mutation을 허용하지 않는다. 따라서 production Source Catalog code가
Control Plane/Delivery implementation에 의존할 수 있다는 뜻이 아니다.

다음 의존은 금지한다.

- Delivery가 catalog 또는 PostgreSQL query adapter를 직접 호출하는 것
- Delivery가 Control Plane persistence implementation/private symbol이나 Assurance verified DTO를
  직접 import하는 것
- Metadata나 Guarded Query가 Control DB implementation을 import하는 것
- Control Plane 이외의 module이 `control` schema table을 직접 변경하는 것
- Runtime production server, Control Plane candidate staging과 Assurance offline CLI 이외의 위치에서
  다른 module의 concrete implementation을 조립하는 것
- Source별 차이를 `source_id` Python branch로 구현하는 것
- Prompt, Skill 또는 caller 관례를 안전 enforcement 계약으로 사용하는 것

현재 평면 구조에서 같은 파일이 두 module 책임을 포함할 수 있다. 그런 파일은 아래의
transition map과 두 module 문서를 모두 읽고, 수정 범위를 symbol 단위로 제한한다.

Metadata와 Guarded Query 사이의 published-revision/SQL-policy contract 및 여러 module이 쓰는
Runtime operations sink는 현재 공개 계약 수준의 양방향 transition debt다. 이를 다른 module의
private implementation 접근 허가로 확대하지 않는다. 물리 분리 때 dependency를 뒤집거나 새
contract package를 만드는 결정은 실제 중복/필요와 사용자 승인을 확인한 별도 작업이다.

## 현재 코드 전환 맵

`논리적 owner`는 기본적으로 해당 artifact의 단일 primary owner다. 같은 file 안에서 symbol이나
configuration section별 owner가 다른 경우에는 별도 row로 나눈다. Consumer나 verification
owner는 주의점에 기록하며 primary owner와 같은 뜻으로 해석하지 않는다.

| 현재 파일 또는 영역 | 논리적 owner | 전환상 주의점 |
|---|---|---|
| `models.py` source/budget/semantic/provenance/observation-definition types | Source Catalog | Published graph의 sequence는 tuple, nested mapping은 alias를 복사한 read-only mapping이다. Optional observability target은 metadata revision이나 public relation allowlist가 아니다. Catalog/metadata types도 같은 파일에 있으므로 type 변경은 Metadata 계약도 확인한다. Delivery admin validation은 `SourceEnvironment`를 소비한다. |
| `models.py` catalog/prepared metadata/provider/resource-observation types | Metadata | Catalog column/key/index/relation/snapshot/prepared graph는 recursively immutable하다. 작은 `CatalogProvider`와 resource/invalidate lifecycle을 포함하는 `RuntimeCatalogProvider`를 제공한다. `SourceProfile`을 소비하므로 Source Catalog 계약을 변경하지 않는다. |
| `registry.py` | Source Catalog | `SourceReader`와 이를 확장하는 `SourceProjectionWriter`를 제공한다. Control Plane은 validator/writer를, ordinary consumer는 reader를 소비한다. Delivery admin validation은 공개 `Identifier`와 `StableSlug` type을 소비하므로 validation 의미 변경 시 Delivery도 확인한다. |
| `catalog.py`, `metadata.py`, `relevance.py`, `revision.py`, `quality_level.py` | Metadata | Catalog는 private mutable builder를 public boundary 전에 freeze한다. `MetadataService`는 immutable graph와 `SourceReader`를 소비하고 wire projection은 list/dict를 유지한다. `revision.py`는 Guarded Query의 immutable canonical-time material을 metadata digest에 포함한다. `reader_policy.py`와 SQL capability는 cross-module 계약이다. |
| `query.py`, `sql_validation.py`, `result_encoding.py` | Guarded Query | `QueryService`는 `SourceReader`와 작은 `QueryExecutor`를 소비하고 Runtime에는 이를 확장하는 `RuntimeQueryExecutor`를 제공한다. `result_encoding.py`가 canonical-time material과 aware datetime UTC encoding을 소유하고 `sql_validation.py`가 같은 material을 policy v2 digest에 포함한다. Trusted terminal outcome은 Runtime usage recorder에만 전달하며 result dictionary 의미는 별도 application contract다. |
| `reader_policy.py` | Source Catalog | Metadata와 Guarded Query가 소비하며 두 DB 경로가 BEGIN 직후 transaction-local UTC를 먼저 설정하고 공통 probe로 검증해야 한다. Role/database default는 바꾸지 않는다. |
| `source_admin.py`, `source_store.py`, `secrets.py` | Control Plane | `SourceReloader`는 `SourceProjectionWriter`와 작은 `SourcePoolInvalidator`, isolated staging은 `SourceReader`/`RuntimeCatalogProvider`를 소비한다. `source_admin.py`는 public administration input/sequence, replica/resource/gateway observation writer, usage projection과 use case를 제공하고 `source_store.py`는 persistence-private type/transaction을 소유한다. Source projection, management catalog, mutation receipt, attempt/success freshness/fencing와 logical retention 의미를 함께 보존한다. |
| `metadata_store.py` Protocol/codec | Metadata contract | Immutable Python graph와 기존 persisted JSON array/object를 상호 변환한다. PostgreSQL store와 Control DB transaction ownership은 Control Plane이다. |
| `metadata_store.py` PostgreSQL implementation | Control Plane | Metadata가 implementation을 알지 않도록 한다. |
| `gateway.py`, `access.py`, `mcp_server.py`, `http_validation.py` | Delivery | `GatewayService`는 `SourceReader`를 소비한다. Public caller/application/transport와 bounded validation contract다. |
| `source_admin_routes.py` | Delivery | Control Plane의 public `CONTROL_SEQUENCE_MAX`, verified-publish input/use case와 source-usage projection, Source Catalog의 `SourceEnvironment`, `Identifier`, `StableSlug` validation type을 소비하는 public admin HTTP/validation contract다. Control persistence와 Assurance DTO를 import하지 않는다. |
| `errors.py`의 `AppError` carrier field | Delivery | Delivery가 HTTP/MCP public envelope와 `status_code/code/message/details` rendering compatibility를 소유한다. Domain subclass를 생성하는 module은 자신의 오류 발생 조건과 의미를 소유하며 file은 coordinating agent가 single-writer로 편집한다. |
| `errors.py`의 `SourceNotFoundError` | Source Catalog | Source 존재 의미는 Source Catalog 계약이다. Metadata, Guarded Query와 Delivery가 생산·소비하고 public status/code/message rendering은 Delivery가 소유한다. |
| `errors.py`의 `MetadataUnavailableError`, `MetadataRevisionMismatchError` | Metadata | Metadata availability와 published revision 의미를 Metadata가 소유한다. Guarded Query와 Control Plane이 소비하고 public envelope는 Delivery가 소유한다. |
| `errors.py`의 `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`, `QueryTimeoutError`, `QueryUnavailableError` 및 query public message/rejected-construct mapping | Guarded Query | SQL policy, execution과 bounded reason 의미를 Guarded Query가 소유한다. Control Plane/Delivery가 소비하고 public envelope는 Delivery가 소유한다. |
| `errors.py`의 `OperatorRequiredError`, `QueryNotFoundError` | Delivery | Delivery가 operator authorization을 강제하고 Guarded Query의 `cancel(query_id) -> found boolean`을 public cancel-not-found 오류로 매핑한다. Guarded Query는 active query ID 존재 의미를 소유한다. |
| `errors.py`의 `SourceValidationError`, `SourceGenerationConflictError`, `SourceControlUnavailableError`, `MutationNotFoundError`, `MutationIdempotencyConflictError` | Control Plane | Source administration state/error 의미는 Control Plane이 소유하고 Delivery는 status/code/message envelope를 rendering한다. 같은 이름의 persistence-private exception과 혼동하지 않는다. |
| `app.py` request/middleware/routes/MCP mount | Delivery | `build_app`와 lifespan 부분은 Runtime 책임이다. |
| `app.py` composition/lifespan/probe/reporters | Runtime | Concrete `SourceRegistry`를 capability별로 주입하고 query/catalog Runtime composite의 required callable을 검증한 뒤 invalidator, direct drain, 24시간 resource와 60초 gateway reporter를 조립한다. Observation Control write는 고정 source pool 안에서 직렬화하며 Delivery route 동작을 함께 바꾸지 않는다. |
| `server.py`, `runtime_config.py`, `operations.py` | Runtime | Domain module은 operations reporting 계약만 소비하며 lifecycle/redaction/health, managed replica identity와 bounded gateway usage accumulator 의미는 Runtime이 소유한다. Existing public operations snapshot은 internal observation을 위해 확장하지 않는다. |
| `__init__.py` | Runtime | Package identity/version만 소유하며 domain contract export를 모으지 않는다. |
| `verified.py`, `quality.py` | Assurance | Quality/verified DTO, configuration, comparison, verification과 hash core다. Concrete registry/catalog/query adapter를 조립하지 않는다. Verified DTO/hash는 Control Plane만 직접 소비하고 hash는 Guarded Query encoding에 의존한다. Delivery는 Control Plane public input을 사용한다. |
| `assurance_cli.py` | Assurance | `query-man-evaluate`/`query-man-verify`의 유일한 offline concrete composition root다. Bootstrap filesystem config만 읽고 registry application reference는 `SourceReader`로 좁히며 production/Control staging wiring을 소유하지 않는다. |
| `tests/helpers.py` | Assurance test infrastructure | Source Catalog registry와 Metadata catalog fixture를 여러 module test가 공유한다. Fixture shape 변경은 해당 provider와 직접 consumer를 확인하고 coordinating agent가 single-writer로 편집한다. |
| `tests/conftest.py` | Assurance test infrastructure | Repository-wide pytest fixture composition이다. Control DB fixture 의미는 Control Plane이 제공하며 coordinating agent가 consumer 실행 순서를 확인하고 single-writer로 편집한다. |
| `tests/control_database.py` | Control Plane | Disposable Control DB, migration apply, 6개 core authority 및 13-table fingerprint, 격리 cross-service archive restore와 leak-free cleanup test contract다. 여러 integration test가 소비하므로 Control Plane owner와 coordinating agent가 한 writer를 지정한다. |
| `tests/test_documentation.py` | Assurance | 모든 module의 문서 link, owner mapping, immutable ledger, governance와 금지된 Delivery hidden import guard를 조립한다. 각 module이 자신의 사실을 검토하되 coordinating agent만 이 shared gate를 편집한다. |
| `tests/test_registry.py` | Source Catalog contract; cross-module capability verification | Registry behavior, source/semantic graph의 deep immutability·alias 차단과 exact `SourceReader`/`SourceProjectionWriter` shape 및 직접 consumer annotation을 함께 검증한다. Capability 계약 변경 때 coordinating agent가 single-writer를 맡는다. |
| `tests/test_catalog.py`, `tests/test_query.py` | Metadata/Guarded Query provider contract | Catalog graph의 deep immutability와 작은 application Protocol/Runtime composite의 exact method set, 상속 및 sync/async signature를 각 provider가 검증한다. UTC setter→공통 verifier→catalog/planning/query 순서와 stale revision의 executor-before 거부도 여기서 고정한다. Composite를 함께 바꾸면 coordinating agent가 consumer 순서를 정한다. |
| `tests/test_http.py` | Delivery | HTTP/MCP parent surface가 primary 범위이며 Runtime lifespan과 Control Plane public admin input/use case도 검증한다. Symbol별 owner review 뒤 coordinating agent가 file single-writer를 맡는다. |
| `tests/test_runtime_startup_cleanup.py` | Runtime | MCP child `enter` 실패 시 Runtime parent cleanup 순서·exactly-once 시도·최초 오류 보존과 failed child `exit` 비호출을 검증한다. Runtime owner가 primary writer이고 Delivery는 child partial-enter 책임 경계만 검토한다. |
| `tests/test_managed_mode.py`, `tests/test_control_startup.py` | Runtime | Runtime composite annotation/누락 capability fail-closed와 managed composition/startup가 primary 범위이며 Delivery access, Control Plane authority와 Assurance verified membership을 함께 검증한다. Coordinating agent가 provider/consumer 변경 순서를 정한다. |
| `tests/test_runtime_config.py` | Runtime | Environment/source authority 조립이 primary 범위이며 Source Catalog의 source directory와 budget configuration 입력을 함께 검증한다. 두 owner가 같은 fixture/config assertion을 병렬 편집하지 않도록 coordinating agent가 single-writer를 지정한다. |
| `tests/test_source_admin.py` | Control Plane | Source Catalog validation, Metadata publish, Guarded Query execution, public verified input에서 Assurance DTO로의 exact mapping을 함께 검증한다. Control Plane owner가 primary writer이고 cross-module contract 변경 시 coordinating single-writer로 전환한다. |
| `tests/test_metadata_store.py` | Metadata contract; Control Plane implementation | Persisted metadata port/codec, legacy array/object round-trip와 immutable decode graph 및 Control DB implementation을 함께 검증하는 transition test다. 두 owner가 병렬 편집하지 않고 coordinating agent가 test-case 단위 변경 순서를 지정한다. |
| `tests/test_quality_level.py` | Metadata | Publish quality 판정이 primary 범위이고 Assurance verified membership을 소비한다. 두 계약을 함께 바꾸면 coordinating agent가 single-writer를 지정한다. |
| `tests/test_result_encoding.py` | Guarded Query | Canonical result scalar encoding과 immutable canonical-time material이 primary 범위이며 Assurance verified result hash와 Metadata revision이 직접 소비한다. Encoding/hash/revision 경계를 함께 바꿀 때 coordinating agent가 single-writer를 맡는다. |
| `tests/test_assurance_cli.py` | Assurance contract; Runtime entrypoint verification | Offline concrete construction 허용 위치, console-script target, bootstrap path, help/output/exit와 cleanup 순서를 검증한다. `pyproject.toml`과 함께 coordinating agent가 single-writer로 편집한다. |
| `tests/test_mcp.py`, `tests/test_mcp_server*.py` | Delivery contract; Assurance acceptance | Delivery의 MCP wire/workflow 의미를 Assurance가 실제 SDK/load/soak로 검증한다. Protocol fixture와 공통 helper는 coordinating single-writer로 다룬다. |
| `tests/test_integration.py`, `tests/test_load.py`, `tests/test_security_evaluation.py` | Assurance | Source Catalog, Metadata, Guarded Query, Delivery와 Runtime의 end-to-end acceptance를 조립한다. Provider 의미는 각 module이 검토하고 coordinating agent만 cross-module test file을 편집한다. |
| `tests/test_source_database_corners.py` | Assurance; Source Catalog/Metadata/Guarded Query integration verification | UUID별 disposable PostgreSQL 18 UTF8/SQL_ASCII source에서 wide/untrusted metadata, timezone/direct·hidden-view·domain-type collation/custom-function·custom-operator/encoding·극단 scalar/result OID·planner-order 경계를 검증한다. 대부분은 실제 public service 흐름을 통과하고 SQL_ASCII identity, domain/enum OID와 domain/operator `pg_depend`는 raw driver/catalog probe로 특성화하며 필요한 경우 public companion을 둔다. RLS base-policy drift는 누출을 current behavior로 승인하지 않는 strict xfail security sentinel이다. Provider 계약 변경은 각 owner가 먼저 검토하고 test fixture는 Assurance가 쓴다. |
| `tests/test_control_recovery.py` | Assurance; Control Plane/Runtime recovery verification | PostgreSQL 18.4→18.6 custom archive, 13-table fingerprint, 별도 key/LOGIN, logical retention, zero-bootstrap와 두 managed replica/query 복구를 하나의 acceptance로 조립한다. Coordinating agent가 provider/consumer 순서와 격리 service ownership을 확인한다. |
| `tests/test_onboarding_skill.py` | Assurance; Source Catalog workflow verification | Plan-only Skill metadata/reference, 8-section handoff, secret/mutation 금지와 owner/admin 경계를 검증한다. Behavioral forward evaluation과 zero-mutation evidence는 별도 acceptance가 보완한다. |
| `tests/test_text_to_sql_skill.py` | Assurance; Delivery workflow verification | Query Man 세 MCP tool이 없을 때 server/HTTP/DB/fixture fallback과 추정 결과를 금지하는 fail-closed consumer workflow를 검증한다. |
| `docker/postgres/init/05-control-plane.sh`, `docker/postgres/init/control-migrations/` | Control Plane | Migration ledger/checksum, 번호, FK, lock, CAS와 privilege는 하나의 owner가 관리한다. |
| `config/sources/`, `config/budget-profiles.yaml` | Source Catalog | Bootstrap/fixture definition과 versioned resource tier다. Managed production authority로 해석하지 않는다. |
| `config/access-policies*.yaml` | Delivery | Caller identity/capability 입력이며 source visibility와 tier 의미는 ADR 0017을 함께 따른다. |
| `config/quality-evaluation.yaml`, `config/verified-queries.yaml`, `config/security-evaluation.yaml` | Assurance | Metadata/Guarded Query acceptance data다. Version, case와 expected result 변경은 관련 provider 계약도 확인한다. |
| `config/onboarding/<source>.yaml`, `config/onboarding/<source>-l2.yaml` | Source Catalog | Control Plane candidate staging이 소비하는 fixture source/semantic input이다. |
| `config/onboarding/<source>-verified-query.yaml` | Assurance | Control Plane candidate staging이 소비하는 verified expectation이다. |
| `Dockerfile`, `compose.yaml`, `.env.example` | Runtime | Image, process, network, secret/config와 lifecycle 조립 계약이다. `recovery` profile의 PostgreSQL 18.4 tmpfs service는 Assurance의 minor-version test fixture이며 production topology가 아니다. HTTP/MCP probe 변경은 Delivery도 확인한다. |
| `scripts/verify-container.sh` | Assurance | Runtime container와 Delivery HTTP/MCP 계약을 소비하는 shared transition acceptance script다. |
| `scripts/apply-control-schema.sh`, `scripts/control-plane-drill.sh` | Control Plane | Schema apply/recovery 절차다. Drill의 acceptance 결과는 Assurance evidence로 남긴다. |
| `scripts/apply-db.sh` | Assurance | Source fixture와 Control Plane migration을 함께 호출하는 shared transition composition script다. 각 provider-owned script의 의미를 바꾸지 않는다. |
| `docker/postgres/init/00-bootstrap.sql`, `01-source-bootstrap.sh`, source fixture SQL `10`~`90` | Assurance | Production source schema authority가 아닌 fixture infrastructure다. `05-control-plane.sh`와 `control-migrations/`는 포함하지 않는다. |
| `.github/workflows/ci.yml`, `.github/workflows/mcp-soak.yml` | Assurance | 모든 provider의 repository gate와 실행 증거를 조립하는 shared transition artifact다. |
| `skills/query-man-text-to-sql/` | Delivery | Metadata/Guarded Query 계약을 소비하는 workflow이며 module contract나 enforcement boundary가 아니다. |
| `skills/query-man-source-onboarding/` | Source Catalog | Onboarding/runbook, Control Plane public administration, Delivery public admin transport와 Assurance acceptance 계약을 읽어 plan-only owner/admin handoff를 만든다. 공개 문서 소비일 뿐 Python/runtime dependency가 아니며 credential, mutation, authorization 또는 validation boundary가 아니다. |
| `pyproject.toml` package/dependency/entrypoint sections | Runtime | 모든 module이 소비하는 shared transition toolchain이다. Offline command 이름은 유지하고 내부 target은 Assurance의 `assurance_cli.py`를 가리킨다. |
| `pyproject.toml` Ruff/mypy/pytest sections | Assurance | Runtime-owned package section과 같은 file이므로 coordinating agent가 single-writer로 편집한다. |
| `uv.lock` | Runtime | 모든 module이 소비하는 shared transition lockfile이며 dependency owner 변경과 함께 갱신한다. |
| `.python-version`, `.dockerignore` | Runtime | Python/container build와 build-context secret boundary다. Assurance가 supply-chain gate를 검증한다. |
| `.gitleaksignore`, `.trivyignore.yaml` | Assurance | Secret/vulnerability scan의 bounded exception이다. 변경 시 근거·scope를 검토하고 vulnerability exception의 expiry를 유지한다. |
| `.github/dependabot.yml` | Runtime | Dependency update automation이다. Assurance gate와 lockfile single-writer 절차를 따른다. |
| `.gitignore` | Coordinating agent | Repository hygiene artifact다. Secret/runtime file 포함 여부를 바꾸면 Runtime과 Assurance 경계를 확인한다. |
| Root `README.md`, `docs/architecture.md`, `docs/mvp.md` | Coordinating agent | 전체 system navigation과 current/target 범위를 여러 module에 handoff한다. 각 사실의 module owner가 검토하고 coordinating agent가 single-writer로 편집한다. |
| `docs/development-todo.md` | Coordinating agent | Repository 전체 priority, approval/start gate와 single-writer handoff를 소유한다. TODO 추가는 계약 승인이 아니며 병렬 agent가 직접 priority를 재배열하지 않는다. |
| `docs/implementation-roadmap.md` | Coordinating agent | 완료 ID와 evidence를 보존하는 immutable completion ledger다. Primary module 결과와 Assurance evidence를 확인한 뒤 한 writer가 갱신한다. |
| `docs/module-contract-decision-guide.md` | Coordinating agent | 승인된 D0-A~D5-A의 exact 범위, 구현 순서와 ID별 완료/미완료 current/target 차이를 전달하는 공통 handoff 문서다. 한 writer만 갱신한다. |
| `docs/verification/`의 cross-module evidence | Assurance | 실행 시점의 provider/consumer evidence를 보존한다. 새 evidence는 coordinating writer가 작성하고 과거 evidence를 현재 보장처럼 소급 수정하지 않는다. |
| Root `AGENTS.md`, 이 module index와 cross-module accepted ADR | Coordinating agent | Repository governance와 공통 contract authority다. 영향 module owner review 뒤 coordinating agent만 편집한다. |

명시적 shared transition artifact는 `models.py`, `reader_policy.py`, `metadata_store.py`,
`errors.py`, `app.py`, `scripts/verify-container.sh`, `scripts/apply-db.sh`, CI workflow,
`pyproject.toml`, `uv.lock`이다. Test 영역에서는 `tests/helpers.py`, `tests/conftest.py`,
`tests/control_database.py`, `tests/test_documentation.py`와 위 표의 cross-module focused/acceptance
test가 shared transition artifact다. 문서 영역에서는 root `README.md`, `AGENTS.md`, 이 index,
`docs/architecture.md`, `docs/mvp.md`, `docs/development-todo.md`,
`docs/implementation-roadmap.md`, `docs/module-contract-decision-guide.md`, cross-module accepted ADR과
`docs/verification/` evidence가 공통 handoff artifact다.

이 목록은 coordinating agent가 single-writer로 직렬화한다. 나머지는 primary owner가 쓰고
소비자는 계약 영향만 검토한다. Test code는 계속 root `tests/`에 두되 해당 test가 검증하는
provider와 직접 consumer module을 owner로 판단한다. 하나의 focused test file이 여러 module의
symbol을 함께 검증하면 test function이 다르다는 이유로 병렬 편집하지 않고 coordinating agent가
primary writer와 변경 순서를 지정한다.

## 새 데이터베이스 추가 시 영향

Production managed mode에서 기존 계약 안으로 PostgreSQL database를 추가하는 일은 module contract
변경이 아니다. 정상적인 onboarding은 application code, repository source file, process restart나
package 수정 없이 다음 data/configuration 흐름으로 끝나야 한다. Bootstrap mode는 local/CI fixture
authority이므로 source YAML 변경과 process restart가 필요할 수 있으며 production onboarding
경로로 사용하지 않는다.

| 영향 영역 | 하는 일 | 보통 코드 변경 여부 |
|---|---|---|
| Source database | 최소 권한 reader, grant/RLS와 필요한 curated view를 준비한다. | Query Man 코드 변경 없음 |
| Source Catalog | 기존 manifest v2/provenance/budget/semantic schema로 definition을 검증한다. | 새 data만 추가 |
| Control Plane | Expected state와 idempotency 계약 아래 credential, source generation과 metadata를 publish한다. | 기존 admin use case 실행 |
| Metadata | Physical catalog를 읽고 revision/context/quality gate를 계산한다. | 자동 실행 |
| Assurance | 필요하면 onboarding quality/verified case를 추가해 acceptance를 남긴다. | Versioned case data만 추가 |
| Delivery, Guarded Query, Runtime | 같은 public contract로 새 `source_id`를 처리한다. | 변경 없음 |

새 database 때문에 Python `source_id` 분기, manifest field, SQL capability, result type, reader 정책
또는 public API를 추가해야 한다면 단순 onboarding이 아니다. 관련 module contract 변경으로
간주하고 사용자 승인을 먼저 받는다.

## 모듈 계약의 의미

다음 중 하나라도 바뀌면 implementation detail이 아니라 module contract 변경이다.

- 다른 module이 import하거나 호출하는 type, Protocol, function 또는 method의 의미와 shape
- HTTP/MCP input, output, error code, status, detail, tool schema 또는 protocol version
- Source manifest, budget profile, access policy, verified query 또는 persisted JSON shape/version
- `metadata_revision`, `sql_policy_revision`, query fingerprint 또는 verified result hash의 재료
- Canonical result encoding과 row/byte accounting
- Query authorize/validate/admit/transaction/limit/cancel/rollback 순서와 실패 의미
- Reader role, schema/relation/function/operator allowlist와 tenant context
- Control DB table, constraint, role/grant, transaction, advisory lock, generation/state CAS와 pin 의미
- Runtime startup/shutdown, reload/invalidate 순서, readiness 또는 public redaction 의미

파일 이동, private helper 변경, 같은 결과를 만드는 내부 algorithm 개선처럼 위 항목을 바꾸지
않는 작업은 module 내부 변경이다. 여러 module의 implementation을 함께 수정하더라도 이미 합의된
계약을 그대로 소비한다면 계약 변경은 아니다.

## 계약 변경 승인 절차

Module contract는 사용자 명시적 승인 없이 변경하지 않는다. 일반적인 “구현해줘”, “정리해줘”,
“refactor해줘”는 계약 변경 승인으로 간주하지 않는다.

계약 변경 필요성을 발견한 agent는 구현을 멈추고 다음 내용을 사용자에게 제시한다.

1. 현재 계약과 변경이 필요한 이유
2. 제안하는 새 계약의 정확한 input, output, 상태 전이 또는 invariant
3. 영향을 받는 owner와 consumer module
4. 하위 호환, rolling replica, persisted data와 migration 영향
5. 보안·데이터 손실 위험과 fail-closed 동작
6. 함께 갱신할 ADR, module 문서와 contract/integration test

사용자가 해당 계약과 영향 범위를 명시적으로 승인한 뒤에만 code, schema, configuration과 계약
문서를 같은 변경에서 갱신한다. 승인 범위를 넘어서는 추가 계약 변경은 다시 승인을 받는다.

## 승인된 계약 강화와 구현 상태

현재 조사된 startup cleanup, hidden dependency, read/write capability, deep immutability,
lifecycle Protocol과 offline composition 선택지는
[module contract decision guide](../module-contract-decision-guide.md)에 설명한다. 사용자는
2026-08-24 `D0-A`~`D5-A`와 공통 불변조건을 승인했다. `D0-A`/`RTSAFE-01`,
`D1-A`/`MOD-04`, `D2-A`/`MOD-05`, `D4-A`/`MOD-06`, `D3-A`/`MOD-07`과
`D5-A`/`MOD-08`은 모두 구현 완료됐다. `CTRL-07A` observation과 2026-08-25 승인된 `CTRL-08`
latest-attempt/usage projection도 provider와 consumer 계약을 고정해 구현했다. 기존 계약을 재현한
`CTRL-09` isolated cross-service Control recovery fixture acceptance까지 완료했으며 이후 작업은
확정된 provider contract를
기준으로 서로 다른 module implementation을 병렬화한다.

## Agent 작업 절차

1. 요청을 담당하는 primary module을 고른다.
2. Root `AGENTS.md`와 primary module의 `README.md`를 읽는다.
3. `집중해서 읽을 범위`의 코드·테스트와 소비 계약, 관련 ADR만 읽는다.
4. 다른 module 구현을 직접 수정하기 전에 공개 계약으로 해결할 수 있는지 확인한다.
5. 계약을 보존하는 최소 end-to-end slice를 구현하고 module별 검증을 실행한다.
6. 계약 변경이 필요하면 위 승인 절차에서 멈춘다.
7. 완료 전에 repository 전체 static/unit gate를 실행하고 DB 경계 변경에는 integration gate를
   추가한다.

병렬 agent는 같은 계약 baseline을 기준으로 서로 다른 module을 맡을 수 있다. Shared file을
동시에 편집하거나 공통 contract 문서/초안을 각자 다르게 만들지 않는다. 이 영역은 coordinating
agent가 single-writer와 순서를 정한다. Contract owner가 계약 변경을 먼저 합의·merge한 후
consumer module 구현을 병렬로 진행한다.

## 문서 우선순위

1. Root `AGENTS.md`의 safety와 작업 절차
2. Accepted ADR과 외부/persisted schema 및 wire contract
3. Public model/Protocol, 실제 schema와 이를 고정하는 runnable contract test
4. 이 module index와 각 module `README.md`의 owner/dependency/집중 읽기 범위
5. 전체 그림을 설명하는 architecture, 사용·운영 문서와 과거 verification evidence

상위 문서와 module 문서가 충돌하거나 문서와 runtime 사실이 다르면 임의로 하나를 선택하지
않고 작업을 멈춰 차이와 필요한 결정을 사용자에게 보고한다.

전체 system architecture와 현재/목표 상태 구분은 [architecture](../architecture.md), 완료된
baseline과 active work는 [implementation roadmap](../implementation-roadmap.md) 및
[development TODO](../development-todo.md)를 따른다. 이 module governance의 승인된 결정은
[ADR 0018](../decisions/0018-module-ownership-and-contract-governance.md)에 기록한다.
