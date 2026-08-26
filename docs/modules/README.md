# Query Man Module Boundaries

Status: Active development governance

## 목적

Query Man은 하나의 repository와 하나의 deployable process를 유지하는 modular monolith다.
이 문서는 기능을 독립적으로 개발할 수 있도록 논리적 module의 소유권, 허용 의존 방향과
module interface 및 별도 변경 경계의 승인 절차를 정의한다.

현재 Python 코드는 `src/query_man`의 평면 구조다. 아래 module directory는 개발 소유권과
interface와 소유 경계를 먼저 고정하기 위한 문서 경계이며, 코드가 이미 물리적으로 package
분리되었다는 뜻이 아니다. 실제 파일 이동은 외부 의미를 보존한 별도 refactoring으로 수행한다.

AI agent는 repository 전체를 기본적으로 읽을 필요가 없다. 작업 module의 문서, 해당 문서가
가리키는 현재 코드와 테스트, 소비하는 interface 및 관련 ADR만 읽는다. 변경하는 완전한 실행 흐름과
trust boundary가 다른 module로 넘어가면 필요한 interface·정책·코드·테스트까지 읽기 범위를 확장한다.

## 3분 시작법

1. 아래 표에서 primary module 하나를 고릅니다.
2. 그 모듈 README의 `집중해서 읽을 범위`에서 필요한 code·test만 읽습니다.
3. 다른 모듈의 private 구현 대신 `제공 인터페이스`를 사용합니다. 의미 변경이 필요하면 구현을
   멈추고 [승인 절차](#승인-대상-변경-절차)를 따릅니다.

일반적인 작업의 시작점은 다음과 같습니다.

| 바꾸려는 것 | Primary module |
|---|---|
| Source manifest, reader, budget, semantic 설정 | Source Catalog |
| DB 구조 수집, context 선택, metadata revision | Metadata |
| SQL 허용 범위, 실행 제한, 결과 encoding, cancel | Guarded Query |
| HTTP/MCP 요청·응답, 인증·인가 | Delivery |
| Process 설정, 조립, readiness, 시작·종료 | Runtime |
| 품질 평가, verified result, offline 검증 | Assurance |
| Managed source 관리와 Control DB | Control Plane — 현재 launch에서는 비활성 |

`Protocol`, `DTO`, `projection`, `composition root` 같은 말은
[용어 사전](../glossary.md)에 쉽게 설명돼 있습니다.

## 모듈 목록

| Module | 한 문장 책임 | 개요 | 작업별 읽기 |
|---|---|---|---|
| Source Catalog | Source 정의, budget, semantic 설정과 runtime source projection을 관리한다. | [source-catalog](source-catalog/README.md) | [필요한 code·test](source-catalog/README.md#집중해서-읽을-범위) |
| Metadata | PostgreSQL catalog를 검증된 revision과 질문별 context로 만든다. | [metadata](metadata/README.md) | [필요한 code·test](metadata/README.md#집중해서-읽을-범위) |
| Guarded Query | SQL을 검증하고 read-only resource limit 안에서 실행·취소·rollback한다. | [guarded-query](guarded-query/README.md) | [필요한 code·test](guarded-query/README.md#집중해서-읽을-범위) |
| Control Plane | Control DB와 source/metadata/verified lifecycle 상태 전이를 원자적으로 관리한다. | [control-plane](control-plane/README.md) | [필요한 code·test](control-plane/README.md#집중해서-읽을-범위) |
| Delivery | Caller를 인증·인가하고 동일한 application service를 HTTP와 MCP로 제공한다. | [delivery](delivery/README.md) | [필요한 code·test](delivery/README.md#집중해서-읽을-범위) |
| Runtime | 구현을 조립하고 configuration, lifecycle, health와 process 운영을 관리한다. | [runtime](runtime/README.md) | [필요한 code·test](runtime/README.md#집중해서-읽을-범위) |
| Assurance | Metadata 품질과 verified query 결과를 offline/runtime acceptance로 검증한다. | [assurance](assurance/README.md) | [필요한 code·test](assurance/README.md#집중해서-읽을-범위) |

## 현재 공통 기준선

[ADR 0025](../decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A`가 current launch
authority다.

- Source Catalog는 모든 RLS manifest를 거부하고 PostgreSQL 18/UTF-8 no-SQL connection verifier를
  제공한다.
- Metadata와 Guarded Query는 pool checkout 직후 verifier를 호출한다.
- Guarded Query는 final result를 exact 7개 OID로 제한하고 SQL policy v3로 식별한다.
- Runtime은 static two-source bootstrap, single replica와 exact-ready container를 조립한다.
- Metadata revision, verified result hash, HTTP/MCP field와 Control DB schema는 바뀌지 않는다.

RLS attestation, broader lossless encoding, COST와 TRACE 문서는 parked research다. 각 module README에는
현재 interface와 invariant만 기록하고 미래 설계를 복제하지 않는다.

## 허용 의존 방향

아래 `->`는 왼쪽 module이 오른쪽 module의 official module interface를 소비한다는 뜻이다.

```text
Delivery -> Source Catalog(read), Metadata, Guarded Query,
            Control Plane administration use cases, Runtime operations interface
Metadata -> Source Catalog(read), reader-connection/session verifier interface,
            Guarded Query immutable SQL-policy descriptor, Runtime operations interface
Guarded Query -> Source Catalog(read and reader verifier), Metadata published-revision interface,
                 Runtime operations interface
Control Plane -> Source Catalog(write), Metadata public use cases,
                 Guarded Query public use case, Assurance verified interface,
                 Runtime operations interface
Assurance -> Source Catalog(read), Metadata, Guarded Query
```

Concrete implementation 조립은 module interface 소비와 다른 ownership/composition 예외다. 아래
`--compose-->`는 조립 권한만 뜻하며 provider private API를 일반 dependency로 공개하지 않는다.

```text
Runtime --compose--> every production implementation, for server composition and lifecycle only
Control Plane candidate staging --compose--> candidate validation에 필요한 implementation only
Assurance offline CLI --compose--> offline acceptance에 필요한 implementation only
```

이 graph는 production Python/runtime dependency다. Source Catalog 소유의 plan-only onboarding
workflow에는 다음 문서 소비 방향을 별도로 허용한다.

```text
Source Catalog onboarding workflow (plan-only)
  -> Control Plane public administration interface,
     Delivery public admin API,
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
- Prompt, Skill 또는 caller 관례를 safety enforcement policy로 사용하는 것

현재 평면 구조에서 같은 파일이 두 module 책임을 포함할 수 있다. 그런 파일은 아래의
transition map과 두 module 문서를 모두 읽고, 수정 범위를 symbol 단위로 제한한다.

Metadata와 Guarded Query 사이의 published-revision/SQL-policy interface 및 여러 module이 쓰는
Runtime operations sink는 현재 official interface 수준의 양방향 transition debt다. 이를 다른 module의
private implementation 접근 허가로 확대하지 않는다. 물리 분리 때 dependency를 뒤집거나 새
interface package를 만드는 결정은 실제 중복/필요와 사용자 승인을 확인한 별도 작업이다.

## 현재 코드 전환 맵

`논리적 owner`는 기본적으로 해당 artifact의 단일 primary owner다. 같은 file 안에서 symbol이나
configuration section별 owner가 다른 경우에는 별도 row로 나눈다. Consumer나 verification
owner는 주의점에 기록하며 primary owner와 같은 뜻으로 해석하지 않는다.

일반 작업에서는 먼저 해당 module README의 짧은 코드 지도를 사용하세요. 아래 전체 표는 shared
file이나 owner가 애매한 artifact를 확인할 때만 펼쳐 봅니다.

<details>
<summary>전체 file·configuration·test ownership map 펼치기</summary>


| 현재 파일 또는 영역 | 논리적 owner | 전환상 주의점 |
|---|---|---|
| `models.py` source/budget/semantic/provenance/observation-definition types | Source Catalog | Published graph의 sequence는 tuple, nested mapping은 alias를 복사한 read-only mapping이다. Optional observability target은 metadata revision이나 public relation allowlist가 아니다. Catalog/metadata types도 같은 파일에 있으므로 type 변경은 Metadata interface도 확인한다. Delivery admin validation은 `SourceEnvironment`를 소비한다. |
| `models.py` catalog/prepared metadata/provider/resource-observation types | Metadata | Catalog column/key/index/relation/snapshot/prepared graph는 recursively immutable하다. 작은 `CatalogProvider`와 resource/invalidate lifecycle을 포함하는 `RuntimeCatalogProvider`를 제공한다. `SourceProfile`을 소비하므로 Source Catalog interface를 변경하지 않는다. |
| `registry.py` | Source Catalog | `SourceReader`와 이를 확장하는 `SourceProjectionWriter`를 제공한다. Control Plane은 validator/writer를, ordinary consumer는 reader를 소비한다. Delivery admin validation은 공개 `Identifier`와 `StableSlug` type을 소비하므로 validation 의미 변경 시 Delivery도 확인한다. |
| `catalog.py`, `metadata.py`, `relevance.py`, `revision.py`, `quality_level.py` | Metadata | Catalog는 private mutable builder를 public boundary 전에 freeze한다. `MetadataService`는 immutable graph와 `SourceReader`를 소비하고 wire projection은 list/dict를 유지한다. `revision.py`는 Guarded Query의 immutable canonical-time material을 metadata digest에 포함한다. Catalog connection은 BEGIN 전에 Source Catalog의 launch verifier를 통과한다. |
| `query.py`, `sql_validation.py`, `result_encoding.py` | Guarded Query | `QueryService`는 `SourceReader`와 작은 `QueryExecutor`를 소비하고 Runtime에는 이를 확장하는 `RuntimeQueryExecutor`를 제공한다. `result_encoding.py`가 canonical-time material과 exact seven-OID policy를 소유하고 `sql_validation.py`가 이를 SQL policy v3 digest에 포함한다. Trusted terminal outcome은 Runtime usage recorder에만 전달하며 result dictionary는 별도 application interface다. |
| `reader_policy.py` | Source Catalog | `READER_CLIENT_ENCODING`과 no-SQL PostgreSQL 18/UTF-8 connection verifier, transaction-local session verifier를 제공한다. Metadata와 Guarded Query가 connection verifier→BEGIN→UTC/session 순서로 소비하며 role/database default는 바꾸지 않는다. |
| `source_admin.py`, `source_store.py`, `secrets.py` | Control Plane | `SourceReloader`는 `SourceProjectionWriter`와 작은 `SourcePoolInvalidator`, isolated staging은 `SourceReader`/`RuntimeCatalogProvider`를 소비한다. `source_admin.py`는 public administration input/sequence, replica/resource/gateway observation writer, usage projection과 use case를 제공하고 `source_store.py`는 persistence-private type/transaction을 소유한다. Source projection, management catalog, mutation receipt, attempt/success freshness/fencing와 logical retention 의미를 함께 보존한다. |
| `metadata_store.py` Protocol/codec | Metadata interface/format | Store port는 module interface이고 codec은 immutable Python graph와 기존 persisted JSON array/object를 상호 변환하는 persisted format이다. PostgreSQL store와 Control DB transaction ownership은 Control Plane이다. |
| `metadata_store.py` PostgreSQL implementation | Control Plane | Metadata가 implementation을 알지 않도록 한다. |
| `gateway.py`, `access.py`, `mcp_server.py`, `http_validation.py` | Delivery | `GatewayService`는 `SourceReader`를 소비한다. Public caller/application interface, external transport와 bounded validation policy를 소유한다. |
| `source_admin_routes.py` | Delivery | Control Plane의 public `CONTROL_SEQUENCE_MAX`, verified-publish input/use case와 source-usage projection, Source Catalog의 `SourceEnvironment`, `Identifier`, `StableSlug` validation type을 소비하는 public admin HTTP API/validation boundary다. Control persistence와 Assurance DTO를 import하지 않는다. |
| `errors.py`의 `AppError` carrier field | Delivery | Delivery가 HTTP/MCP public envelope와 `status_code/code/message/details` rendering compatibility를 소유한다. Domain subclass를 생성하는 module은 자신의 오류 발생 조건과 의미를 소유하며 file은 coordinating agent가 single-writer로 편집한다. |
| `errors.py`의 `SourceNotFoundError` | Source Catalog | Source 존재 의미는 Source Catalog module interface다. Metadata, Guarded Query와 Delivery가 생산·소비하고 public status/code/message rendering은 Delivery가 소유한다. |
| `errors.py`의 `MetadataUnavailableError`, `MetadataRevisionMismatchError` | Metadata | Metadata availability와 published revision 의미를 Metadata가 소유한다. Guarded Query와 Control Plane이 소비하고 public envelope는 Delivery가 소유한다. |
| `errors.py`의 `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`, `QueryTimeoutError`, `QueryUnavailableError` 및 query public message/rejected-construct mapping | Guarded Query | SQL policy, execution과 bounded reason 의미를 Guarded Query가 소유한다. Control Plane/Delivery가 소비하고 public envelope는 Delivery가 소유한다. |
| `errors.py`의 `OperatorRequiredError`, `QueryNotFoundError` | Delivery | Delivery가 operator authorization을 강제하고 Guarded Query의 `cancel(query_id) -> found boolean`을 public cancel-not-found 오류로 매핑한다. Guarded Query는 active query ID 존재 의미를 소유한다. |
| `errors.py`의 `SourceValidationError`, `SourceGenerationConflictError`, `SourceControlUnavailableError`, `MutationNotFoundError`, `MutationIdempotencyConflictError` | Control Plane | Source administration state/error 의미는 Control Plane이 소유하고 Delivery는 status/code/message envelope를 rendering한다. 같은 이름의 persistence-private exception과 혼동하지 않는다. |
| `app.py` request/middleware/routes/MCP mount | Delivery | `build_app`와 lifespan 부분은 Runtime 책임이다. |
| `app.py` composition/lifespan/probe/reporters | Runtime | Concrete `SourceRegistry`를 capability별로 주입하고 query/catalog Runtime composite의 required callable을 검증한 뒤 invalidator, direct drain, 24시간 resource와 60초 gateway reporter를 조립한다. Observation Control write는 고정 source pool 안에서 직렬화하며 Delivery route 동작을 함께 바꾸지 않는다. |
| `server.py`, `runtime_config.py`, `operations.py` | Runtime | Domain module은 operations reporting interface만 소비하며 lifecycle/redaction/health, managed replica identity와 bounded gateway usage accumulator 의미는 Runtime이 소유한다. Existing public operations snapshot은 internal observation을 위해 확장하지 않는다. |
| `__init__.py` | Runtime | Package identity/version만 소유하며 domain interface export를 모으지 않는다. |
| `verified.py`, `quality.py` | Assurance | Quality/verified DTO, configuration, comparison, verification과 hash core다. Concrete registry/catalog/query adapter를 조립하지 않는다. Verified DTO/hash는 Control Plane만 직접 소비하고 hash는 Guarded Query encoding에 의존한다. Delivery는 Control Plane public input을 사용한다. |
| `assurance_cli.py` | Assurance | `query-man-evaluate`/`query-man-verify`의 유일한 offline concrete composition root다. Bootstrap filesystem config만 읽고 registry application reference는 `SourceReader`로 좁히며 production/Control staging wiring을 소유하지 않는다. |
| `tests/helpers.py` | Assurance test infrastructure | Source Catalog registry와 Metadata catalog fixture를 여러 module test가 공유한다. Fixture shape 변경은 해당 provider와 직접 consumer를 확인하고 coordinating agent가 single-writer로 편집한다. |
| `tests/conftest.py` | Assurance test infrastructure | Repository-wide pytest fixture composition이다. Control DB fixture 의미는 Control Plane이 제공하며 coordinating agent가 consumer 실행 순서를 확인하고 single-writer로 편집한다. |
| `tests/control_database.py` | Control Plane | Disposable Control DB, migration apply, 6개 core authority 및 13-table fingerprint, 격리 cross-service archive restore와 leak-free cleanup test fixture다. 여러 integration test가 소비하므로 Control Plane owner와 coordinating agent가 한 writer를 지정한다. |
| `tests/test_documentation.py` | Assurance | 모든 module의 문서 link, owner mapping, immutable ledger, governance와 금지된 Delivery hidden import guard를 조립한다. 각 module이 자신의 사실을 검토하되 coordinating agent만 이 shared gate를 편집한다. |
| `tests/test_registry.py` | Source Catalog interface verification | Registry behavior, source/semantic graph의 deep immutability·alias 차단과 exact `SourceReader`/`SourceProjectionWriter` shape 및 직접 consumer annotation을 함께 검증한다. Capability interface 변경 때 coordinating agent가 single-writer를 맡는다. |
| `tests/test_catalog.py`, `tests/test_query.py` | Metadata/Guarded Query provider-interface verification | Catalog graph의 deep immutability와 작은 application Protocol/Runtime composite의 exact method set, 상속 및 sync/async signature를 각 provider가 검증한다. UTC setter→공통 verifier→catalog/planning/query 순서와 stale revision의 executor-before 거부도 여기서 고정한다. Composite를 함께 바꾸면 coordinating agent가 consumer 순서를 정한다. |
| `tests/test_http.py` | Delivery | HTTP/MCP parent surface가 primary 범위이며 Runtime lifespan과 Control Plane public admin input/use case도 검증한다. Symbol별 owner review 뒤 coordinating agent가 file single-writer를 맡는다. |
| `tests/test_runtime_startup_cleanup.py` | Runtime | MCP child `enter` 실패 시 Runtime parent cleanup 순서·exactly-once 시도·최초 오류 보존과 failed child `exit` 비호출을 검증한다. Runtime owner가 primary writer이고 Delivery는 child partial-enter 책임 경계만 검토한다. |
| `tests/test_managed_mode.py`, `tests/test_control_startup.py` | Runtime | Runtime composite annotation/누락 capability fail-closed와 managed composition/startup가 primary 범위이며 Delivery access, Control Plane authority와 Assurance verified membership을 함께 검증한다. Coordinating agent가 provider/consumer 변경 순서를 정한다. |
| `tests/test_runtime_config.py` | Runtime | Environment/source authority 조립이 primary 범위이며 Source Catalog의 source directory와 budget configuration 입력을 함께 검증한다. 두 owner가 같은 fixture/config assertion을 병렬 편집하지 않도록 coordinating agent가 single-writer를 지정한다. |
| `tests/test_source_admin.py` | Control Plane | Source Catalog validation, Metadata publish, Guarded Query execution, public verified input에서 Assurance DTO로의 exact mapping을 함께 검증한다. Control Plane owner가 primary writer이고 cross-module interface 변경 시 coordinating single-writer로 전환한다. |
| `tests/test_metadata_store.py` | Metadata interface/format; Control Plane implementation | Persisted metadata port/codec, legacy array/object round-trip와 immutable decode graph 및 Control DB implementation을 함께 검증하는 transition test다. 두 owner가 병렬 편집하지 않고 coordinating agent가 test-case 단위 변경 순서를 지정한다. |
| `tests/test_quality_level.py` | Metadata | Publish quality 판정이 primary 범위이고 Assurance verified membership을 소비한다. 두 interface를 함께 바꾸면 coordinating agent가 single-writer를 지정한다. |
| `tests/test_result_encoding.py` | Guarded Query | Canonical result scalar encoding과 immutable canonical-time material이 primary 범위이며 Assurance verified result hash와 Metadata revision이 직접 소비한다. Encoding/hash/revision 경계를 함께 바꿀 때 coordinating agent가 single-writer를 맡는다. |
| `tests/test_assurance_cli.py` | Assurance CLI/composition; Runtime entrypoint verification | Offline concrete construction 허용 위치, console-script target, bootstrap path, help/output/exit와 cleanup 순서를 검증한다. `pyproject.toml`과 함께 coordinating agent가 single-writer로 편집한다. |
| `tests/test_mcp.py`, `tests/test_mcp_server*.py` | Delivery MCP API; Assurance acceptance | Delivery의 MCP wire/workflow 의미를 Assurance가 실제 SDK/load/soak로 검증한다. Protocol fixture와 공통 helper는 coordinating single-writer로 다룬다. |
| `tests/test_integration.py`, `tests/test_load.py`, `tests/test_security_evaluation.py` | Assurance | Source Catalog, Metadata, Guarded Query, Delivery와 Runtime의 end-to-end acceptance를 조립한다. Provider 의미는 각 module이 검토하고 coordinating agent만 cross-module test file을 편집한다. |
| `tests/test_source_database_corners.py` | Assurance; Source Catalog/Metadata/Guarded Query integration verification | UUID별 disposable PostgreSQL 18 UTF8/SQL_ASCII source에서 wide/untrusted metadata, timezone/direct·hidden-view·domain-type collation/custom-function·custom-operator/encoding·극단 scalar/result OID·planner-order 경계를 검증한다. SQL_ASCII identity, domain/enum OID와 domain/operator `pg_depend`는 raw driver/catalog probe로 보존하고, public path는 exact seven OID와 RLS quarantine를 회귀 검증한다. RLS base-policy drift 사실은 과거 evidence에 남지만 current serving에서는 tenant 유무와 무관하게 query/database 이전에 차단한다. Provider interface/policy 변경은 각 owner가 먼저 검토하고 test fixture는 Assurance가 쓴다. |
| `tests/test_control_recovery.py` | Assurance; Control Plane/Runtime recovery verification | PostgreSQL 18.4→18.6 custom archive, 13-table fingerprint, 별도 key/LOGIN, logical retention, zero-bootstrap와 두 managed replica/query 복구를 하나의 acceptance로 조립한다. Coordinating agent가 provider/consumer 순서와 격리 service ownership을 확인한다. |
| `tests/test_onboarding_skill.py` | Assurance; Source Catalog workflow verification | Plan-only Skill metadata/reference, 8-section handoff, secret/mutation 금지와 owner/admin 경계를 검증한다. Behavioral forward evaluation과 zero-mutation evidence는 별도 acceptance가 보완한다. |
| `tests/test_text_to_sql_skill.py` | Assurance; Delivery workflow verification | Query Man 세 MCP tool이 없을 때 server/HTTP/DB/fixture fallback과 추정 결과를 금지하는 fail-closed consumer workflow를 검증한다. |
| `docker/postgres/init/05-control-plane.sh`, `docker/postgres/init/control-migrations/` | Control Plane | Migration ledger/checksum, 번호, FK, lock, CAS와 privilege는 하나의 owner가 관리한다. |
| `config/sources/`, `config/budget-profiles.yaml` | Source Catalog | Fixture이자 ADR 0025 static first-launch의 reviewed two-source definition과 versioned resource tier다. Dynamic managed authority와 섞지 않는다. |
| `config/access-policies*.yaml` | Delivery | Caller identity/capability 입력이며 source visibility와 tier 의미는 ADR 0017을 함께 따른다. |
| `config/quality-evaluation.yaml`, `config/verified-queries.yaml`, `config/security-evaluation.yaml` | Assurance | Metadata/Guarded Query acceptance data다. Version, case와 expected result 변경은 관련 provider interface/policy도 확인한다. |
| `config/onboarding/<source>.yaml`, `config/onboarding/<source>-l2.yaml` | Source Catalog | Control Plane candidate staging이 소비하는 fixture source/semantic input이다. |
| `config/onboarding/<source>-verified-query.yaml` | Assurance | Control Plane candidate staging이 소비하는 verified expectation이다. |
| `Dockerfile`, `compose.yaml`, `.env.example` | Runtime | Image, process, network, secret/config와 lifecycle composition boundary다. `recovery` profile의 PostgreSQL 18.4 tmpfs service는 Assurance의 minor-version test fixture이며 production topology가 아니다. HTTP/MCP probe 변경은 Delivery도 확인한다. |
| `scripts/verify-container.sh` | Assurance | Runtime container surface와 Delivery HTTP/MCP API를 소비하는 shared transition acceptance script다. |
| `scripts/apply-control-schema.sh`, `scripts/control-plane-drill.sh` | Control Plane | Schema apply/recovery 절차다. Drill의 acceptance 결과는 Assurance evidence로 남긴다. |
| `scripts/apply-db.sh` | Assurance | Source fixture와 Control Plane migration을 함께 호출하는 shared transition composition script다. 각 provider-owned script의 의미를 바꾸지 않는다. |
| `docker/postgres/init/00-bootstrap.sql`, `01-source-bootstrap.sh`, source fixture SQL `10`~`90` | Assurance | Production source schema authority가 아닌 fixture infrastructure다. `05-control-plane.sh`와 `control-migrations/`는 포함하지 않는다. |
| `.github/workflows/ci.yml`, `.github/workflows/mcp-soak.yml` | Assurance | 모든 provider의 repository gate와 실행 증거를 조립하는 shared transition artifact다. |
| `skills/query-man-text-to-sql/` | Delivery | Delivery의 external MCP API와 workflow를 사용하는 client-side plan이며 Metadata/Guarded Query Python interface나 enforcement boundary 자체는 아니다. |
| `skills/query-man-source-onboarding/` | Source Catalog | Onboarding/runbook, Control Plane public administration, Delivery public admin transport와 Assurance acceptance scope를 읽어 plan-only owner/admin handoff를 만든다. 공개 문서 소비일 뿐 Python/runtime dependency가 아니며 credential, mutation, authorization 또는 validation boundary가 아니다. |
| `pyproject.toml` package/dependency/entrypoint sections | Runtime | 모든 module이 소비하는 shared transition toolchain이다. Offline command 이름은 유지하고 내부 target은 Assurance의 `assurance_cli.py`를 가리킨다. |
| `pyproject.toml` Ruff/mypy/pytest sections | Assurance | Runtime-owned package section과 같은 file이므로 coordinating agent가 single-writer로 편집한다. |
| `uv.lock` | Runtime | 모든 module이 소비하는 shared transition lockfile이며 dependency owner 변경과 함께 갱신한다. |
| `.python-version`, `.dockerignore` | Runtime | Python/container build와 build-context secret boundary다. Assurance가 supply-chain gate를 검증한다. |
| `.gitleaksignore`, `.trivyignore.yaml` | Assurance | Secret/vulnerability scan의 bounded exception이다. 변경 시 근거·scope를 검토하고 vulnerability exception의 expiry를 유지한다. |
| `.github/dependabot.yml` | Runtime | Dependency update automation이다. Assurance gate와 lockfile single-writer 절차를 따른다. |
| `.gitignore` | Coordinating agent | Repository hygiene artifact다. Secret/runtime file 포함 여부를 바꾸면 Runtime과 Assurance 경계를 확인한다. |
| Root `README.md`, `docs/README.md`, `docs/glossary.md`, `docs/architecture.md`, `docs/mvp.md`, `docs/decisions/README.md` | Coordinating agent | 전체 system navigation, 공통 용어와 current/target/record 구분을 여러 module에 handoff한다. 각 사실의 module owner가 검토하고 coordinating agent가 single-writer로 편집한다. |
| `docs/development-todo.md`, `docs/future-work.md` | Coordinating agent | Repository 전체 active priority와 parked start gate를 분리해 소유한다. TODO나 future ID 추가는 interface 또는 경계 변경 승인이 아니며 병렬 agent가 직접 priority를 재배열하지 않는다. |
| `docs/source-onboarding.md`, `docs/managed-source-onboarding.md`, `docs/source-extension-checklist.md` | Source Catalog | 현재 static inventory 경로와 별도 승인 뒤의 managed onboarding 절차를 분리한다. Managed 절차를 current launch로 확대하지 않는다. |
| `docs/implementation-roadmap.md` | Coordinating agent | 완료 ID와 evidence를 보존하는 immutable completion ledger다. Primary module 결과와 Assurance evidence를 확인한 뒤 한 writer가 갱신한다. |
| `docs/module-boundary-decision-guide.md` | Coordinating agent | 승인·완료된 D0-A~D5-A를 보존하는 frozen decision record다. Current 개발 시작점은 이 index와 ADR 0018이다. |
| `docs/verification/`의 cross-module evidence | Assurance | 실행 시점의 provider/consumer evidence를 보존한다. 새 evidence는 coordinating writer가 작성하고 과거 evidence를 현재 보장처럼 소급 수정하지 않는다. |
| Root `AGENTS.md`, 이 module index와 cross-module accepted ADR | Coordinating agent | Repository governance와 공통 interface/boundary authority다. 영향 module owner review 뒤 coordinating agent만 편집한다. |

명시적 shared transition artifact는 `models.py`, `reader_policy.py`, `metadata_store.py`,
`errors.py`, `app.py`, `scripts/verify-container.sh`, `scripts/apply-db.sh`, CI workflow,
`pyproject.toml`, `uv.lock`이다. Test 영역에서는 `tests/helpers.py`, `tests/conftest.py`,
`tests/control_database.py`, `tests/test_documentation.py`와 위 표의 cross-module focused/acceptance
test가 shared transition artifact다. 문서 영역에서는 root `README.md`, `AGENTS.md`, 이 index,
`docs/README.md`, `docs/glossary.md`, `docs/architecture.md`, `docs/mvp.md`,
`docs/development-todo.md`, `docs/future-work.md`, `docs/decisions/README.md`,
`docs/implementation-roadmap.md`, `docs/module-boundary-decision-guide.md`, cross-module accepted ADR과
`docs/verification/` evidence가 공통 handoff artifact다.

이 목록은 coordinating agent가 single-writer로 직렬화한다. 나머지는 primary owner가 쓰고
소비자는 interface 또는 별도 경계 영향만 검토한다. Test code는 계속 root `tests/`에 두되 해당 test가 검증하는
provider와 직접 consumer module을 owner로 판단한다. 하나의 focused test file이 여러 module의
symbol을 함께 검증하면 test function이 다르다는 이유로 병렬 편집하지 않고 coordinating agent가
primary writer와 변경 순서를 지정한다.

</details>

## 새 데이터베이스 추가 시 영향

Static first launch의 reviewed inventory는 `development-issues`, `market-voc`뿐이다. 새 database나
source ID는 정상 운영 중 추가 대상이 아니며 manifest review, result OID/reader policy 확인과 별도
배포 승인이 필요하다. 동적 추가가 실제 요구가 되면 이미 구현된 managed onboarding을 별도 운영
결정으로 활성화한다.

| 영향 영역 | 하는 일 | 보통 코드 변경 여부 |
|---|---|---|
| Source database | 최소 권한 reader, non-RLS curated view, PG18/UTF-8과 DDL freeze 범위를 준비한다. | 사전 환경 작업 |
| Source Catalog | Manifest, budget, semantic과 static inventory 변경을 검토한다. | 설정·배포 변경 |
| Metadata/Guarded Query | Catalog와 실제/광고 result OID가 launch policy 안인지 검증한다. | 정책 밖이면 별도 승인 |
| Assurance | Quality, verified query와 positive/negative result corpus를 실행한다. | Versioned acceptance 변경 |
| Runtime/Delivery | 단일-replica route, auth, exact ready와 rollback을 검증한다. | 배포 변경 |

새 database 때문에 Python `source_id` 분기, manifest field, SQL capability, result type, reader 정책
또는 public API를 추가해야 한다면 단순 onboarding이 아니다. Module interface, external API/wire,
persisted/versioned format, policy/compatibility identity, safety/lifecycle invariant,
ownership/composition boundary와 protected operational procedure 중 해당되는 모든 변경 범주를
명시하고 사용자 승인을 먼저 받는다.

## 용어와 변경 분류

Official `module interface`는 provider가 allowed dependency map과 자기 module 문서에서 다른 logical
module이 사용하도록 명시적으로 공개한 Python constant/enum/type/DTO/domain error, Protocol,
function, method, use case와 lifecycle capability다. 그 의미는 Python shape/signature와 호출 단위
input/output/domain-error semantics로 한정한다. 단순 public symbol, shared file 또는 중요한 system
behavior라는 이유만으로 module interface가 되지는 않는다. Policy, ordering, limit와 lifecycle
outcome은 아래 별도 범주로 분류한다.

나머지 의미는 다음 이름으로 구분한다.

- `External API/wire format`: HTTP/MCP/CLI shape, status, error, auth와 protocol
- `Persisted/versioned format`: DB/file/config schema, codec, version과 migration/rollback
- `Policy/compatibility identity`: revision, fingerprint, canonical encoding/hash, allowlist와 resource policy
- `Safety/lifecycle invariant`: 실행·실패·cleanup의 externally required outcome
- `Ownership/composition boundary`: dependency와 concrete implementation 조립 권한
- `Protected operational procedure`: protected inventory, freeze, DDL, cutover, rollback plan과
  stop/rollback condition
- `Operational execution authorization`: 실제 protected action의 access, scope, target, stop condition과
  change-record 책임
- `Evidence/change record`: 실행된 사실을 보존하는 append-only/immutable evidence와 provenance
- `Shared transition artifact`: 여러 owner가 검토하고 single-writer가 편집할 file, 문서와 test

예를 들어 SQL-policy descriptor의 Python shape는 module interface지만 digest 재료는 policy identity다.
Control DB table은 다른 module이 직접 소비하지 않으므로 persisted format이지 module interface가
아니다. Runtime이 소비하는 `SourceProjectionWriter`는 module interface지만 그 구현 table은 private다.

파일 이동, private helper/algorithm/lock 구현 변경처럼 interface와 별도 승인 대상 의미를 보존하는
작업은 module 내부 변경이다. 여러 module implementation을 함께 수정하더라도 합의된 interface,
format, policy와 invariant를 그대로 지키면 새 의미 선택이 아니다.

## 승인 대상 변경 절차

Module interface의 의미 변경은 additive change를 포함해 사용자 명시적 승인 없이 진행하지 않는다.
External API/wire, persisted/versioned format, policy/compatibility identity, safety/lifecycle invariant,
ownership/composition boundary와 protected operational procedure의 의미 변경도 실제 범주를 명시해
별도로 승인받는다. 일반적인
“구현해줘”, “정리해줘”, “refactor해줘”는 어느 범주의 의미 변경 승인으로도 간주하지 않는다.

변경 필요성을 발견한 agent는 구현을 멈추고 다음 내용을 사용자에게 제시한다.

1. 현재 의미와 변경이 필요한 이유 및 정확한 변경 범주
2. 제안하는 새 interface input/output/error 또는 format/policy/invariant
3. 영향을 받는 provider/consumer, external client, persisted data 또는 operator
4. 하위 호환, rolling replica, migration과 rollback 영향
5. 보안·데이터 손실 위험과 fail-closed 동작
6. 함께 갱신할 ADR, module 문서, 해당되는 procedure/evidence와 interface/integration/acceptance test

서로 다른 범주를 한 기능과 관련 있다는 이유만으로 하나의 승인안에 자동 포함하지 않는다.
사용자가 해당 변경과 영향 범위를 명시적으로 승인한 뒤에만 code, schema, configuration과 의미
문서를 같은 변경에서 갱신한다. Protected environment의 실제 operation은 repository나 procedure
승인과 별도로 access, scope, target, stop condition 및 change-record 책임을 확인한 실행 승인이
필요하다. 실행 evidence는 과거 기록을 조용히 수정·삭제하지 않고 정정 provenance를 남긴다.

## 승인된 module-boundary 강화와 구현 상태

Startup cleanup, hidden dependency, read/write capability, deep immutability,
lifecycle Protocol과 offline composition의 완료 선택은 frozen
[module boundary decision record](../module-boundary-decision-guide.md)에 보존한다. 사용자는
2026-08-24 `D0-A`~`D5-A`와 공통 불변조건을 승인했다. `D0-A`/`RTSAFE-01`,
`D1-A`/`MOD-04`, `D2-A`/`MOD-05`, `D4-A`/`MOD-06`, `D3-A`/`MOD-07`과
`D5-A`/`MOD-08`은 모두 구현 완료됐다. `CTRL-07A` observation과 2026-08-25 승인된 `CTRL-08`
latest-attempt/usage projection도 승인된 change-set 의미를 고정해 구현했다. 기존 상태를 재현한
`CTRL-09` isolated cross-service Control recovery fixture acceptance까지 완료했으며 이후 작업은
확정된 change-set baseline을
기준으로 서로 다른 module implementation을 병렬화한다.

## Agent 작업 절차

1. 요청을 담당하는 primary module을 고른다.
2. Root `AGENTS.md`와 primary module의 `README.md`를 읽는다.
3. `집중해서 읽을 범위`의 코드·테스트와 소비 interface, 관련 ADR만 읽는다.
4. 다른 module 구현을 직접 수정하기 전에 official interface로 해결할 수 있는지 확인한다.
5. Interface와 별도 경계를 보존하는 최소 end-to-end slice를 구현하고 module별 검증을 실행한다.
6. 승인 대상 의미 변경이 필요하면 위 절차에서 멈춘다.
7. 완료 전에 repository 전체 static/unit gate를 실행하고 DB 경계 변경에는 integration gate를
   추가한다.

병렬 agent는 같은 승인 change-set baseline을 기준으로 서로 다른 module을 맡을 수 있다. Shared file을
동시에 편집하거나 공통 governance 문서/초안을 각자 다르게 만들지 않는다. 이 영역은 coordinating
agent가 single-writer와 순서를 정한다. Change-set owner가 승인된 baseline을 먼저 merge한 후 독립
module 구현을 병렬로 진행한다.

## 문서 우선순위

1. Root `AGENTS.md`의 safety와 작업 절차
2. Accepted ADR, external API/wire, persisted/versioned format, policy/compatibility identity,
   safety/lifecycle invariant와 protected operational procedure
3. Official module interface, 실제 schema, runnable interface/integration/acceptance test와
   provenance가 보존된 evidence/change record
4. 이 module index와 각 module `README.md`의 owner/dependency/집중 읽기 범위
5. 전체 그림을 설명하는 architecture, 사용·운영 문서와 과거 verification evidence

상위 문서와 module 문서가 충돌하거나 문서와 runtime 사실이 다르면 임의로 하나를 선택하지
않고 작업을 멈춰 차이와 필요한 결정을 사용자에게 보고한다.

전체 system architecture와 현재/목표 상태 구분은 [architecture](../architecture.md), 현재 작업은
[development TODO](../development-todo.md), 과거 완료 ID는
[implementation ledger](../implementation-roadmap.md)를 따른다. 전체 문서 지도는
[docs 안내](../README.md)에 있습니다. 이 module governance의 승인된 결정은
[ADR 0018](../decisions/0018-module-ownership-and-contract-governance.md)에 기록한다.
