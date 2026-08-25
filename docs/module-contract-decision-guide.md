# 모듈 계약 강화 결정 가이드

Status: Accepted choices — implementation complete

Last reviewed: 2026-08-25

Approved: 2026-08-24 — `D0-A`, `D1-A`, `D2-A`, `D3-A`, `D4-A`, `D5-A`와
[모든 권장 선택에 공통으로 유지할 불변조건](#모든-권장-선택에-공통으로-유지할-불변조건)

## 이 문서의 목적

Query Man은 하나의 process로 배포되지만, 개발할 때는 일곱 개 논리 module이 각자 공개한
창구만 사용하려 한다. 현재 문서 경계는 생겼지만 일부 관계는 Python type이나 조립 구조로
강제되지 않는다. 이 문서는 그 간격에 대한 여섯 가지 선택을 쉬운 말로 설명하고, 승인 범위와
ID별 구현 상태를 함께 기록한다.

1. Startup 도중 실패했을 때 어디까지 정리할 것인가
2. Delivery의 숨은 import를 어느 공개 계약으로 돌릴 것인가
3. Source 조회 권한과 수정 권한을 type으로 나눌 것인가
4. 공유 snapshot을 어느 수준까지 변경 불가능하게 만들 것인가
5. Runtime lifecycle capability를 어떻게 명시할 것인가
6. Offline 품질 CLI의 예외적인 조립 권한을 어디에 둘 것인가

이 문서는 선택지의 상세 범위와 구현 순서를 설명하고 ADR 0018의 승인 기록을 보조한다. 위
Approved 조합은 명시적으로 승인됐고 `RTSAFE-01`, `MOD-04`~`MOD-08`의 구현·검증이 끝나 완료
ledger로 이동했다. 여섯 선택은 모두 현재 구현 계약이다. 승인 범위를 넘어서는 code, schema,
configuration 또는 module contract 변경은 다시 승인받는다.

## 한눈에 보는 현재 상태

```text
현재

Runtime ──> child 진입 전 parent resource를 추적하고 실패 시 역순 cleanup

Delivery ──> Control Plane 공개 sequence/verified-publish input
Control  ──> Assurance verified DTO

일반 consumer ──> SourceReader
Control writer ──> SourceProjectionWriter

Source/Metadata ──> 외부 JSON은 유지하면서 published graph를 deep immutable로 제공

Runtime ──> 필수 RuntimeQueryExecutor/RuntimeCatalogProvider를 검증하고 직접 호출

Assurance core
    └── 전용 offline CLI composition root
```

승인된 전체 조합의 현재 구조는 다음과 같다. Runtime cleanup 행은 `RTSAFE-01`, Delivery/Control
행은 `MOD-04`, Source capability 두 행은 `MOD-05`, lifecycle 행은 `MOD-06`, published graph의
deep immutability는 `MOD-07`, Assurance CLI 분리는 `MOD-08`로 반영됐다.

```text
승인된 목표(현재)

Runtime ──> child 진입 전 parent resource를 추적하고 실패 시 역순 cleanup

Delivery ──> Control Plane 공개 관리 입력
Control  ──> Assurance verified 계약

일반 consumer ──> SourceReader
Control writer ──> SourceProjectionWriter

Source/Metadata ──> 외부 JSON은 유지하면서 published graph를 deep immutable로 제공

Runtime ──> 명시된 필수 lifecycle Protocol

Assurance core
    └── 전용 offline CLI composition root
```

이 목표는 microservice 전환이나 물리 package 전면 분리가 아니다. 같은 process 안에서 다른
module의 서랍을 직접 열지 않고 공식 창구만 사용하게 만드는 작업이다.

## 용어사전

| 용어 | 쉬운 설명 |
|---|---|
| Module | 한 가지 책임을 맡은 논리적인 부서 |
| 계약 | 다른 부서가 사용해도 된다고 합의한 입력, 출력과 동작 |
| Provider | 계약을 제공하는 module |
| Consumer | 그 계약을 사용하는 module |
| Public contract | 다른 module이 사용해도 되는 공식 창구 |
| Private implementation | 담당 module 안에서만 쓰는 table, helper, 상수와 저장 방식 |
| 의존성 | 한 module이 다른 module의 type이나 동작을 알아야 하는 관계 |
| 숨은 의존성 | 문서에는 없지만 실제 import나 호출로 생긴 관계 |
| Capability | 객체가 할 수 있는 작업. 열쇠나 출입카드와 비슷하다 |
| Read capability | 조회만 할 수 있는 권한 |
| Write capability | 등록, 교체와 삭제까지 할 수 있는 권한 |
| Protocol | Python에서 “이 객체는 이 method들을 제공해야 한다”고 명시하는 interface. HTTP protocol과는 다른 말이다. |
| DTO | Module 사이에 전달하는 정해진 모양의 data object |
| Command/Input | “이 상태 변경을 수행해 달라”는 application 입력 |
| Immutable | 생성된 뒤 내용을 바꿀 수 없는 상태 |
| Shallow immutable | 겉 객체는 잠겼지만 안쪽 list/dict는 바꿀 수 있는 상태 |
| Deep immutable | 안쪽 collection까지 모두 바꿀 수 없는 상태 |
| Snapshot | 특정 시점의 data를 고정해 둔 사본 |
| Lifecycle | 시작, reload, 신규 작업 중단, drain과 종료 순서 |
| Drain | 진행 중인 작업을 일정 시간 기다린 뒤 남은 작업을 취소하는 종료 절차 |
| Invalidate | Source 교체 뒤 이전 cache나 connection pool을 폐기하는 작업 |
| Lifespan | ASGI application이 startup과 shutdown 때 resource를 열고 닫는 구간 |
| Composition root | 여러 module의 실제 구현을 조립하는 제한된 장소 |
| Offline quality gate | Production 요청 밖에서 metadata와 검증 SQL을 실행해 품질을 판정하는 명령 |
| Fail-closed | 필요한 검증이나 capability가 없으면 추측해 계속하지 않고 안전하게 실패하는 방식 |
| Wire contract | HTTP/MCP처럼 process 밖으로 보이는 요청·응답 계약 |
| Persisted contract | DB나 file에 저장되어 이전 version과도 호환돼야 하는 계약 |
| Contract test | Provider와 consumer가 같은 계약을 이해하는지 실행해서 확인하는 test |
| Rolling replica | 서로 다른 code version의 replica가 잠시 함께 실행되는 배포 상태 |

## 결정 요약

| 결정 | 질문 | 권장 | 가장 큰 trade-off |
|---|---|---|---|
| D0 | Startup 실패 cleanup을 보장할 것인가 | D0-A | 실패 경로 구현과 test가 늘어남 |
| D1 | Delivery의 숨은 import를 어디로 돌릴 것인가 | D1-A | Control Plane public Python input이 추가됨 |
| D2 | Source read/write capability를 나눌 것인가 | D2-A | Type annotation과 composition 수정이 필요함 |
| D3 | 공유 snapshot을 deep immutable로 만들 것인가 | D3-A | 가장 넓은 consumer/test 수정이 필요함 |
| D4 | Lifecycle method를 필수 Protocol로 만들 것인가 | D4-A | Custom adapter와 test fake가 새 capability를 구현해야 함 |
| D5 | Offline CLI 조립 예외를 어디에 둘 것인가 | D5-A | CLI 전용 file/entrypoint 분리가 필요함 |

각 결정은 독립적으로 선택하거나 보류할 수 있다. 이 문서에서 `A` 선택지는 아래 “정확한
범위”까지 승인할 수 있는 implementation-ready 제안이다. `B`와 새 architecture를 만드는
`D1-C`/`D5-C`는 방향과 trade-off를 비교하기 위한 대안이며, 선택하면 별도의 정확한 contract
초안을 만든 뒤 다시 승인받는다. `D0-C`/`D2-C`/`D3-C`/`D4-C`는 현재 상태를 유지하고 debt를
보류하는 선택이라 구현 승인이 필요하지 않다.

## Wave 0: 승인 전 read-only prework (완료)

현재 가능한 Wave 0는 계약을 구현하는 단계가 아니다. 다음 read-only prework만
더 낮은 priority의 Start gate 전에도 병렬로 할 수 있다.

- 현재 code, type, import, test, schema와 운영 근거 조사
- Provider/direct consumer 범위와 compatibility, rollback, safety 영향 확인
- 이 문서의 기존 선택지 비교와 구현·검증 계획 초안 작성
- 추가 승인이 필요한 범위를 찾았을 때 의미상 변경을 멈추고 보고

Wave 0는 아래 행위를 허용하지 않는다.

- Active TODO item을 공식적으로 시작·완료했다고 표시하는 것
- Accepted ADR/module contract baseline의 의미를 바꾸는 것
- Code, schema, configuration, public type/Protocol 또는 contract 문서의 의미를 변경하는 것
- 권장안이나 TODO 순서를 사용자의 contract 선택으로 간주하는 것

계약 선택과 실행 순서는 Active TODO와 completion ledger에서 다음 ID로 추적한다.

| 계약 결정 | 추적 ID | 상태/공식 시작 gate |
|---|---|---|
| D0 startup cleanup | `RTSAFE-01` | 구현·검증 완료; roadmap ledger로 이동 |
| D1 hidden dependency | `MOD-04` | 구현·검증 완료; roadmap ledger로 이동 |
| D2 read/write capability | `MOD-05` | 구현·검증 완료; roadmap ledger로 이동 |
| D4 lifecycle Protocol | `MOD-06` | 구현·검증 완료; roadmap ledger로 이동 |
| D3 deep immutability | `MOD-07` | 구현·검증 완료; roadmap ledger로 이동 |
| D5 offline composition | `MOD-08` | 구현·검증 완료; roadmap ledger로 이동 |

이 plan, Wave 0 또는 Active TODO 순서만 승인하는 것은 `D0-A`~`D5-A` 선택 승인이 아니었다.
2026-08-24 사용자가 [승인 회신 방법](#승인-회신-방법)의 권장 조합과 공통 불변조건을 명시적으로
승인했고 모든 ID를 정해진 순서로 구현했다.

`D1`/`D5`의 B/C와 `D2`/`D3`/`D4`의 B는 implementation-ready A choice가 아니므로
구현 전 exact follow-up contract를 다시 승인받는다. 반면 `D2-C`/`D3-C`/`D4-C`는
현재 상태와 debt를 유지해 구현할 내용이 없다. 이 C choice를 선택해도 Active TODO의
해당 P0.5 ID가 자동으로 완료되거나 P1 gate가 자동으로 열리지 않는다. 사용자가
해당 ID를 bypass/defer할지와 남은 debt를 받아들이고 P1을 시작할지를 별도로 재결정한다.

## D0. Startup 진입 실패 cleanup

### 현재 사실

Managed Runtime은 Control sync, metadata probe와 reload task 생성 뒤 MCP child lifespan에 진입한다.
`RTSAFE-01` 구현 뒤 child의 `__aenter__`가 실패하면 parent가 먼저 연 reload task와
query/catalog/metadata/source-store 최상위 resource를 승인된 고정 순서로 정리한다. 각 resource
identity에는 close/cancel을 한 번만 시도하고 한 단계가 실패해도 나머지를 계속 정리하며 최초
startup exception을 보존한다. [Runtime contract](modules/runtime/README.md#startup-contract)과
[`test_runtime_startup_cleanup.py`](../tests/test_runtime_startup_cleanup.py)가 이 보장을 고정한다.

중요한 구분이 있다. 진입에 실패한 child context에 parent가 `__aexit__`를 호출하면 안 된다.
Child가 `__aenter__` 안에서 일부 resource를 열었다면 그 partial-enter cleanup은 child
lifespan 구현의 책임이다. Runtime이 책임질 범위는 **child 진입을 시도하기 전에 parent가 이미
만든 resource**다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D0-A 권장** | 현재 startup 순서를 유지한다. Parent resource를 추적하고 child enter 실패 시 parent resource만 정확히 한 번 역순 cleanup한 뒤 원래 exception을 다시 발생시킨다. | 현재 정상 경로를 유지하면서 leak gap을 닫는다. 책임 경계가 명확하다. | 실패 단계별 cleanup test와 조심스러운 exception handling이 필요하다. |
| D0-B | MCP child lifespan을 reload task/pool 같은 장기 parent resource보다 먼저 진입시킨다. 이후 단계 실패 시 정상 진입한 child를 종료한다. | Child enter 실패 때 정리할 parent resource가 줄어든다. | 현재 startup 순서가 바뀌며 child background 동작과 control sync/probe의 관계를 다시 검증해야 한다. |
| D0-C | 현재 동작을 유지하고 process restart와 운영 감시로 대응한다. | Code 변경이 없다. | Startup 반복 실패 때 resource가 남을 수 있고 현재 P0 debt가 계속 열린다. |

### D0-A 승인 범위와 구현 결과

- 정상 startup/shutdown 순서, readiness와 public response는 바꾸지 않는다.
- Child enter를 시도하기 전에 parent가 만든 reload task, query/catalog/metadata/store resource를
  cleanup 대상에 포함한다.
- 진입하지 못한 child의 `__aexit__`는 호출하지 않는다.
- Cleanup 순서는 `reload task cancel/await -> query executor immediate close -> catalog close ->
  metadata close(소유한 metadata store 포함) -> source store close`로 고정한다.
- Startup이 아직 ready가 아니므로 configured graceful drain을 기다리지 않는다. Production query
  executor의 immediate close는 `stop_accepting`과 `drain(0)` 의미를 포함한다.
- 한 cleanup이 실패해도 나머지 resource 정리를 계속한다. Cleanup failure는 secret-free bounded
  logging만 허용하고 최초 startup exception을 primary error로 다시 발생시킨다.
- 같은 resource의 close/cancel은 정확히 한 번 시도한다.
- Runtime owner가 `app.py` lifespan symbol과 failure-path test를 single-writer로 변경한다.

DB migration, wire/persisted schema와 data loss 영향은 없다. Rollback은 code rollback이다.

## D1. Delivery의 숨은 의존성

### 현재 사실

`MOD-04` 구현 뒤 Delivery 소유 `source_admin_routes.py`는 기존 Control Plane public
`MutationContext`/`SourceAdminService`와 새 `CONTROL_SEQUENCE_MAX`,
`PublishVerifiedQueryInput`, `VerifiedExpectedInput`을 소비한다. Control Plane persistence
implementation의 `POSTGRES_BIGINT_MAX`와 Assurance 소유 `VerifiedQuery`/`ExpectedResult`를 직접
import하지 않는다. `SourceAdminService`가 public input을 Assurance DTO로 exact mapping한다.
[`test_documentation.py`](../tests/test_documentation.py)의 import guard와
[`test_source_admin.py`](../tests/test_source_admin.py)의 mapping test가 이 경계를 고정한다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D1-A 권장** | Control Plane public contract에 sequence 상한과 verified publish input을 둔다. Delivery는 이 input만 만들고 Control Plane이 Assurance DTO로 변환한다. | 현재 허용 dependency graph를 지키며 Delivery와 Assurance를 분리한다. | Public Python input type과 mapping test가 추가된다. |
| D1-B | `Delivery -> Assurance`를 공식 허용하고 sequence 상한만 Control Plane public contract로 옮긴다. | 변경량이 가장 작다. | Assurance DTO 변경이 Delivery route 변경으로 전파되고 dependency graph가 더 촘촘해진다. |
| D1-C | 전역 `contracts` package를 새로 만들어 DTO와 상한을 모두 옮긴다. | Import 위치는 한곳이 된다. | 현재 규모에서 owner 없는 공용 창고가 될 위험이 크고 물리 이동 범위가 넓다. |

### D1-A 승인 범위와 구현 결과

Control Plane 공개 계약에 다음 exact public symbol을 추가한다.

```text
CONTROL_SEQUENCE_MAX: Final[int] = 9_223_372_036_854_775_807

@dataclass(frozen=True)
VerifiedExpectedInput:
  columns: tuple[str, ...]
  row_count: int
  result_hash: str

@dataclass(frozen=True)
PublishVerifiedQueryInput:
  query_id: str
  source_id: str
  question: str
  sql: str
  metadata_revision: str
  relations: tuple[str, ...]
  expected: VerifiedExpectedInput
```

- Delivery는 `source_store.py`와 `verified.py`를 직접 import하지 않는다.
- `SourceAdminService.publish_verified_query`의 첫 argument는 `PublishVerifiedQueryInput`이 되고,
  현재 `tenant_id: str`와 `MutationContext | None` argument 의미는 유지한다.
- Control Plane만 이 input을 Assurance `VerifiedQuery`/`ExpectedResult`로 변환한다.
- 현재 HTTP field, validation 상한, status/error, receipt와 Control DB schema는 바꾸지 않는다.
- Managed verified identity `(source_id, query_id, metadata_revision)`, result hash와 membership
  의미를 바꾸지 않는다.

DB migration은 없다. External contract가 같아 mixed-version replica가 함께 있어도 wire/storage
호환성은 유지된다. Rollback은 code rollback이다.

## D2. Source 조회와 수정 capability

### 현재 사실

`SourceRegistry` concrete 하나는 기존 조회와 수정 method를 모두 제공한다.

```text
조회: list, get, source_ids
수정: upsert, remove
```

`MOD-05` 구현 뒤 Source Catalog는 structural `SourceReader`와 이를 확장하는
`SourceProjectionWriter` Protocol을 제공한다. Delivery `GatewayService`, Metadata
`MetadataService`, Guarded Query `QueryService`, Runtime probe와 Assurance application reference는
`SourceReader`로 좁혀지고 Control Plane `SourceReloader`만 `SourceProjectionWriter`를 받는다.
Concrete registry의 method나 identity를 숨기는 runtime sandbox는 아니며 type checker와 review가
일반 consumer의 accidental mutation을 찾는 경계다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D2-A 권장** | Source Catalog가 read-only `SourceReader`와 read/write `SourceProjectionWriter` Protocol을 제공한다. 같은 `SourceRegistry`가 둘 다 구현하고 consumer에는 필요한 capability만 전달한다. | 새 wrapper 없이 mypy와 review가 잘못된 mutation을 잡는다. | Constructor/type annotation과 test fake를 갱신해야 한다. |
| D2-B | 별도의 read-only wrapper를 만들어 일반 consumer에 전달하고 concrete registry는 Runtime/Control Plane만 가진다. | Runtime에서도 mutation method에 접근하기 더 어렵다. | Wrapper와 전달 계층, identity/cache 고려가 늘어난다. |
| D2-C | Concrete `SourceRegistry`를 계속 전달하고 문서로만 mutation을 금지한다. | Code 변경이 없다. | 실수를 자동으로 찾지 못하며 병렬 agent 격리 목표가 약해진다. |

### 승인·구현된 D2-A의 정확한 범위

```text
SourceReader:
  list() -> list[dict[str, str]]
  get(source_id) -> SourceProfile | None
  source_ids() -> frozenset[str]

SourceProjectionWriter extends SourceReader:
  upsert(source) -> None
  remove(source_id) -> None
```

- Delivery, Metadata, Guarded Query와 Assurance application code는 `SourceReader`만 소비한다.
- Control Plane runtime projector/reloader는 `SourceProjectionWriter`를 소비한다.
- Runtime은 concrete `SourceRegistry`를 생성할 수 있지만 업무 consumer에는 read capability로
  전달한다.
- `SourceRegistry.load`, manifest parsing과 현재 return value는 바꾸지 않는다.
- Source authority, projection content, reload 순서와 public `GET /sources` 응답은 바꾸지 않는다.

이는 악성 in-process code를 막는 sandbox가 아니라 개발 실수를 type 수준에서 차단하는 계약이다.
DB/schema/data migration은 없다.

## D3. 공유 data의 deep immutability

### 구현 전 사실

`SourceProfile`은 `frozen=True`지만 안쪽 semantic list/dict는 수정할 수 있다. `PreparedMetadata`도
frozen이지만 안쪽 `CatalogSnapshot`, relation과 column object는 mutable하다. 겉표지에는
자물쇠가 있지만 안쪽 종이는 지우개로 고칠 수 있는 상태다.

Registry와 metadata cache는 같은 객체를 여러 consumer에 반환한다. 한 consumer의 우발적
mutation이 다른 query나 revision 계산에 영향을 줄 수 있다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D3-A 권장** | Module 밖으로 나가는 `SourceProfile`과 published metadata graph를 재귀적으로 immutable하게 만든다. Mutable builder는 provider 내부에만 둔다. | 공유 객체 오염을 가장 강하게 막고 반복 조회 때 복사 비용이 없다. | List→tuple/Mapping type 변경으로 가장 많은 consumer와 fixture 수정이 필요하다. |
| D3-B | Public shape는 유지하고 `get`/`get_published` 경계에서 defensive deep copy를 반환한다. | Provider 원본은 보호하면서 consumer의 list API는 유지한다. | Metadata가 클 때 조회마다 CPU와 memory 복사 비용이 생긴다. |
| D3-C | 현재 shallow immutability와 문서상 mutation 금지를 유지한다. | Code 변경이 없다. | Cache/registry 공유 객체 오염 위험과 agent 실수 가능성이 남는다. |

### D3-A를 승인하면 바뀌는 정확한 범위

- `SourceProfile`과 semantic overlay의 public sequence는 실제 tuple이 된다. Nested mapping은
  `MappingProxyType` 또는 동등한 runtime-immutable representation을 사용하고, provider는
  mutable 원본의 alias를 published graph 밖에도 남기지 않는다. Public annotation만 `Mapping`으로
  바꾸고 내부 dict를 공유하는 구현은 D3-A를 만족하지 않는다.
- `CatalogColumn`, `CatalogRelation`, `CatalogSnapshot`과 `PreparedMetadata`의 published graph는
  재귀적으로 immutable하다.
- Catalog introspection, YAML decoder와 persistence decoder 내부 builder는 mutable일 수 있지만
  public boundary를 넘기 전에 immutable snapshot으로 변환한다.
- `VerifiedQuery`처럼 이미 frozen scalar/tuple graph인 type은 바꾸지 않는다.
- YAML, HTTP/MCP JSON과 Control DB persisted JSON은 현재처럼 array/object로 직렬화한다.
- Metadata revision, canonical ordering, result hash와 byte accounting은 byte-for-byte 동일해야
  한다. Golden test가 달라지면 fail-closed하고 변경을 중단한다.
- 현재 revision canonicalizer가 특별 처리하는 list/dict와 새 tuple/immutable mapping을 같은
  canonical list/object로 정규화하는 compatibility code와 golden test를 같은 변경에 포함한다.

의도한 DB migration은 없다. Python public type 변화가 넓으므로 coordinating workstream 안에서
provider를 먼저 편집하되, 모든 직접 consumer·문서·test까지 통과하는 하나의 atomic commit으로
확정한다. Mixed-version replica의 external contract는 동일하게 유지한다.

### D3-A 구현 결과 (`MOD-07` 완료)

Source Catalog provider와 public dataclass는 sequence를 실제 tuple로, nested semantic mapping을
원본 alias를 복사한 read-only mapping으로 freeze한다. Metadata catalog는 private mutable builder를
사용한 뒤 frozen column/key/index/relation/snapshot graph를 반환하고 persistence decoder도 같은
경계에서 freeze한다. Context와 snapshot codec은 tuple/read-only mapping을 기존 list/dict로
명시적으로 projection한다.

List/dict와 tuple/immutable mapping을 같은 canonical array/object로 처리하는 revision
canonicalizer, nested mutation/alias 거부, legacy persisted JSON round-trip와 변경 전 exact revision
및 snapshot JSON golden을 runnable test로 고정했다. Golden이 유지돼 Control DB migration은 없고
HTTP/MCP, metadata revision, canonical result encoding과 verified hash 계약은 그대로다.

## D4. Runtime lifecycle Protocol

### 현재 사실

Production concrete는 기존 lifecycle method를 유지하고 provider가 작은 application Protocol과
Runtime 전용 composite Protocol을 나눠 제공한다.

```text
QueryExecutor Protocol: execute, cancel, close
RuntimeQueryExecutor: QueryExecutor + stop_accepting, drain, invalidate
CatalogProvider Protocol: load, close
RuntimeCatalogProvider: CatalogProvider + invalidate
```

`MOD-06` 구현 뒤 Runtime은 주입/default adapter를 falsey 여부가 아니라 `None` 여부로 선택하고
composite의 모든 required method가 callable인지 app composition에서 검증한다. 누락 adapter는
sync/probe/background task나 ready 전 `TypeError`로 실패한다. 정상 shutdown은 configured grace로
`drain`을 직접 호출하며 managed reloader에는 catalog와 query invalidator가 모두 주입된다. Runtime
검사는 callable 존재를, provider Protocol/mypy/contract test는 sync/async signature를 고정한다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D4-A 권장** | 작은 application Protocol은 유지하고 provider module이 Runtime 전용 composite lifecycle Protocol을 추가한다. Runtime은 필수 capability가 모두 있는 객체만 조립한다. | 일반 consumer는 작은 계약을 유지하고 Runtime은 drain/invalidate를 확실히 요구한다. | Runtime injection과 test/custom adapter가 새 Protocol을 만족해야 한다. |
| D4-B | 기존 `QueryExecutor`와 `CatalogProvider`에 lifecycle method를 모두 직접 추가한다. | Protocol 수가 적고 눈에 잘 보인다. | Lifecycle을 쓰지 않는 QueryService와 test fake까지 운영 method를 구현해야 한다. |
| D4-C | 현재 optional `getattr` 호출을 유지한다. | Code 변경이 없다. | Custom adapter에서 shutdown drain이나 source invalidation이 조용히 빠질 수 있다. |

### 승인·구현된 D4-A의 정확한 범위

```text
RuntimeQueryExecutor extends QueryExecutor:
  stop_accepting() -> None
  async drain(grace_ms: int) -> None
  async invalidate(source_id: str) -> None
  async close() -> None  # inherited

RuntimeCatalogProvider extends CatalogProvider:
  async invalidate(source_id: str) -> None
  async close() -> None  # inherited
```

기존 `close()`는 상위 Protocol을 그대로 사용한다.

- Guarded Query가 query lifecycle Protocol을, Metadata가 catalog lifecycle Protocol을 소유한다.
- Runtime은 capability를 optional하게 추측하지 않는다. Startup sync/probe와 background task를
  시작하기 전에 required method가 callable인지 composition-time validation하고, mypy와 contract
  test로 sync/async signature를 고정한다.
- Control Plane reloader는 현재 `SourcePoolInvalidator` consumer port로 두 provider를 소비한다.
- Production/test/custom adapter가 필수 capability를 제공하지 않으면 composition이 ready 전에
  fail-closed한다.
- 현재 drain grace, reload invalidation과 정상 startup/shutdown 순서는 바꾸지 않는다.
- D0의 startup enter-failure cleanup은 별도 결정으로 `RTSAFE-01`에서 완료됐으며 D4 범위에
  포함하지 않는다.

Wire/DB contract와 data migration은 없다. Python adapter compatibility만 바뀐다.

## D5. Assurance offline composition root

### 현재 사실

Production concrete adapter 조립은 Runtime만 수행한다. 예외적으로 Assurance의
`query-man-evaluate`는 실제 Metadata를, `query-man-verify`는 Metadata와 Guarded Query safety
path를 실행해야 하므로 제한된 offline composition root로 허용되어 있다.

`quality.py`와 `verified.py`에는 core case/DTO/comparison/hash만 남고, `assurance_cli.py`가 두
console entrypoint와 concrete wiring을 전담한다. File 경계만 읽어도 offline composition 권한의
owner가 드러난다.

### 선택지

| 선택 | 내용 | 장점 | 비용·위험 |
|---|---|---|---|
| **D5-A 권장** | Assurance가 offline composition 권한을 유지하되 전용 CLI module/entrypoint로 격리한다. Core quality/verified code는 concrete adapter를 조립하지 않는다. | 현재 workflow를 유지하면서 예외 위치와 owner가 명확해진다. | File과 console-script target의 내부 이동이 필요하다. |
| D5-B | Offline CLI concrete wiring도 Runtime으로 옮기고 Assurance는 rule/case/hash만 제공한다. | Composition root라는 이름의 위치는 하나가 된다. | Runtime이 production 운영과 offline 품질 workflow를 모두 알아야 한다. |
| D5-C | Runtime과 Assurance가 함께 쓰는 범용 composition package를 새로 만든다. | 조립 code를 공유할 여지가 있다. | 현재 실제 두 사용 사례보다 abstraction이 크고 owner가 흐려질 수 있다. |

### 승인·구현된 D5-A의 정확한 범위

- `query-man-evaluate`, `query-man-verify` command 이름, argument, output와 exit 의미는 바꾸지
  않는다.
- Assurance 전용 CLI composition file만 `SourceRegistry`, Metadata, Catalog와 Query concrete
  adapter를 조립할 수 있다.
- Quality/verified core type, comparison과 hash function은 concrete implementation을 조립하지
  않는다.
- `query-man-verify`의 verification SQL은 계속 Guarded Query safety path를 통과한다.
- 두 offline CLI는 현재처럼 bootstrap-only로 filesystem `config/sources`와 quality/verified
  config를 읽는다. Managed Runtime의 authority selector나 fallback 경로로 사용하지 않는다.
- `query-man-verify`가 tenant ID를 공급하지 않아 RLS source를 fail-closed하는 현재 범위를 유지한다.
- Production HTTP/MCP wiring과 domain policy를 Assurance CLI로 옮기지 않는다.
- Control Plane candidate staging의 별도 bounded composition 권한은 바꾸지 않는다.

DB/wire/persisted data migration은 없다. Console script의 외부 이름은 같아서 rollback은 code
rollback으로 끝난다.

### D5-A 구현 결과 (`MOD-08` 완료)

`query-man-evaluate`와 `query-man-verify`의 `pyproject.toml` command 이름은 유지하고 내부 target만
`assurance_cli.py`의 두 entrypoint로 옮겼다. 이 파일만 `SourceRegistry`, `PostgresCatalog`,
`PostgresQueryExecutor`, `MetadataService`와 `QueryService`를 offline workflow에 조립한다.
Quality/verified core와 Control Plane의 DTO/hash consumer import는 그대로다.

Contract test는 console target, help와 `--root` resolve, bootstrap-only config path, evaluate의 exact
success/failure JSON·exit, verify의 success JSON·실패 예외 전파, 각 cleanup과 concrete construction
허용 위치를 고정한다. Live evaluate/verify 결과와 help는 변경 전후 byte-for-byte 같고 verification SQL은
계속 tenant ID 없이 `QueryService`를 지나므로 RLS fail-closed 범위도 유지된다. Dependency, lockfile,
DB/wire/persisted migration은 없다.

## 모든 권장 선택에 공통으로 유지할 불변조건

`D0-A`, `D1-A`, `D2-A`, `D3-A`, `D4-A`, `D5-A` 조합은 다음을 바꾸지 않는다.

- HTTP/MCP request, response, error, status와 protocol version
- Source manifest, budget profile, access policy와 onboarding 절차
- Control DB schema, migration, receipt, advisory lock와 generation/state CAS
- Metadata revision, SQL policy revision, fingerprint와 canonical result encoding
- Query authorize/validate/admit/transaction/timeout/cancel/rollback 순서
- Verified result hash, row-count 비교와 verified membership identity
- Reader role, allowlist, tenant/RLS와 credential trust boundary
- 정상 Runtime startup/shutdown/reload 순서, readiness와 grace 의미
- Quality threshold와 CLI 성공/실패 의미
- 하나의 deployable process와 현재 물리 package layout
- Dependency set과 lockfile 내용

`D0-B`를 선택하면 child lifespan의 내부 startup 위치만 명시적으로 예외가 된다. 이 불변조건을
지킬 수 없는 추가 필요가 발견되면 구현을 멈추고 새 영향 범위를 다시 승인받는다.

## Compatibility, migration과 rollback 요약

| 결정 | Wire/DB migration | Rolling replica | 주요 안전 확인 | Rollback |
|---|---|---|---|---|
| D0-A | 없음 | External contract 동일 | 최초 error 보존, exactly-once parent cleanup | Code rollback |
| D1-A | 없음 | HTTP/storage 동일 | Private import 제거, verified identity/hash 불변 | Code rollback |
| D2-A | 없음 | Runtime output 동일 | Read consumer type contract가 writer capability를 요구·노출하지 않음 | Code rollback |
| D3-A | 없음; golden 호환 검증 완료 | External JSON/revision/hash 동일 | Nested mutation 거부, golden digest/hash 불변 | Code rollback |
| D4-A | 없음 | External contract 동일 | Capability 누락 시 ready 전 fail-closed | Code rollback |
| D5-A | 없음 | Production runtime와 무관 | CLI safety path와 exit/output 불변 | Code rollback |

D3 golden encoding이나 digest가 하나라도 달라지면 “migration 없음” 전제가 깨진다. 그 경우 승인
범위를 넘으므로 구현을 중단하고 별도 compatibility/migration 결정을 요청한다.

## 권장 조합과 이유

```text
D0-A  정상 startup 순서를 보존하면서 parent resource leak gap을 닫음
D1-A  기존 dependency graph를 유지하며 숨은 import를 public use case로 회수
D2-A  Wrapper 없이 read/write 권한을 type으로 분리
D3-A  공유 cache/registry object의 우발적 오염을 원천 차단
D4-A  일반 application 계약은 작게, Runtime 필수 lifecycle은 명시적으로 유지
D5-A  기존 offline workflow는 유지하면서 composition 예외 위치를 격리
```

변경량을 줄이는 것이 최우선이면 D3만 `D3-B`를 선택할 수 있었다. 다만 large metadata를
조회할 때마다 복사하는 CPU/memory 비용을 먼저 측정해야 했다. 승인된 D0-A~D5-A는 각각
`RTSAFE-01`, `MOD-04`~`MOD-08`에서 완료되어 startup leak, Delivery hidden-import, 혼합 consumer
capability, optional lifecycle, shallow shared-graph와 mixed core/CLI composition debt는 더 이상 현재
상태가 아니다.

## 승인 회신 방법

권장 조합 전체를 승인하려면 다음처럼 회신한다.

```text
D0-A, D1-A, D2-A, D3-A, D4-A, D5-A를
“모든 권장 선택에 공통으로 유지할 불변조건” 범위 안에서 승인한다.
```

일부만 승인하거나 보류할 수도 있다.

```text
D0-A, D1-A, D2-A, D4-A, D5-A 승인.
D3은 보류하고 현재 계약(D3-C)을 유지한다.
```

대안 방향을 고르되 아직 구현을 승인하지 않으려면 다음처럼 회신한다.

```text
D3-B 방향을 선택한다. 구현하지 말고 defensive-copy의 정확한
type, 성능 상한, consumer와 rollback 범위를 후속 승인안으로 작성해줘.
```

선택하지 않은 항목은 현재 계약과 debt 상태를 그대로 유지한다. 일반적인 “이어서 구현”,
“정리” 또는 “권장안대로 잘 해줘”는 이 문서의 계약 변경 승인으로 해석하지 않는다. 이 문서로
구현을 승인할 때는 반드시 implementation-ready `A` 선택 ID와 공통 불변조건 범위가 포함되어야
한다. `B` 또는 새 architecture인 `D1-C`/`D5-C` 선택은 후속 정확한 승인안을 요청하는 방향
결정으로만 기록한다.

## 승인 후 작업과 커밋 순서

계약 변경은 coordinating workstream 하나가 다음 순서로 직렬화한다.

1. D0 startup failure cleanup (`RTSAFE-01`) — 완료
2. D1 숨은 dependency 제거 (`MOD-04`) — 완료
3. D2 read/write capability 분리 (`MOD-05`) — 완료
4. D4 lifecycle Protocol 명시 (`MOD-06`) — 완료
5. D3 immutable snapshot 전환 (`MOD-07`) — 완료
6. D5 offline CLI composition 격리 (`MOD-08`) — 완료
7. 전체 dependency/contract audit — 완료 (2026-08-25)

최종 audit에서 당시 contract 위반이나 migration 필요성은 발견되지 않았다. 이후 `CTRL-06A`의
Control DB schema, replica observation, freshness와 admin response 계약은 2026-08-25 별도 사용자
승인을 받아 구현됐다. `CTRL-07A` resource/gateway observation도 2026-08-25 별도 승인을 받아
구현됐다. `CTRL-08` latest resource attempt/last-success와 public usage projection도 2026-08-25
별도 승인을 받아 구현됐고, 기존 계약을 재현한 `CTRL-09` isolated cross-service Control recovery
fixture acceptance까지 완료됐다. 이후 plan-only Source Onboarding Skill `SKILL-01`~`SKILL-06`도
독립 forward evaluation과 mutation 0 증거로 완료됐다. Disposable source DB assurance
`DBEDGE-01`~`DBEDGE-03`도 완료했다. Reader TimeZone/canonical hash 변경은 별도 승인된
`TIME-01`과 `TIME-02`에서
결정·구현했고 완료 이력을 roadmap ledger로 옮겼다. 후속 database corner에서 발견한
lossless scalar, semantic GUC, result OID와 array identity gap은
[proposed ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 `ENC-01` 승인 전
구현을 동결했다. Repository fixture와 local cutover까지는 검증했지만 final encoding baseline의 실제
managed production inventory·재발행·rollback change record인 `TIME-03`은 열려 있다. 이를 완료하거나
사용자가 명시적으로 defer한 뒤에만 `COST-01`을 시작한다. COST/TRACE의
[ADR 0021](decisions/0021-database-native-cost-attribution.md)과
[ADR 0022](decisions/0022-w3c-workflow-trace-context.md)는 lower-track read-only 선택지 초안일 뿐
start/contract 승인이 아니다.

각 단계는 provider contract, 직접 consumer, module 문서와 runnable contract test가 함께
통과하는 독립 커밋으로 끝낸다. Shared contract file은 single-writer로 편집한다. Provider
baseline이 확정된 뒤에만 서로 다른 consumer implementation을 병렬화한다.

최소 repository gate는 다음과 같다.

```text
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -m integration
```

추가 contract test는 다음을 고정한다.

- Child enter 실패 때 진입하지 못한 child exit를 호출하지 않고 parent resource만 역순 정리
- Delivery가 Control persistence와 Assurance DTO를 직접 import하지 않음
- Read consumer type contract가 writer capability를 요구하거나 노출하지 않음
- Nested snapshot mutation 거부와 metadata revision/result hash 불변
- Lifecycle capability 누락 시 composition이 ready 전에 fail-closed
- Assurance core와 offline CLI concrete wiring 분리
- Provider와 모든 직접 consumer의 focused regression

## 현재 근거 위치

| 관찰 | 현재 위치 |
|---|---|
| Startup enter-failure cleanup 보장 | `src/query_man/app.py` lifespan, [`test_runtime_startup_cleanup.py`](../tests/test_runtime_startup_cleanup.py), [Runtime startup contract](modules/runtime/README.md#startup-contract) |
| Delivery의 public Control administration input 경계 | `src/query_man/source_admin_routes.py`, `src/query_man/source_admin.py`, [`test_documentation.py`](../tests/test_documentation.py), [`test_source_admin.py`](../tests/test_source_admin.py) |
| 분리된 Source capability | `src/query_man/registry.py`의 `SourceReader`/`SourceProjectionWriter`, [`test_registry.py`](../tests/test_registry.py) |
| Deep immutable source/metadata graph | `src/query_man/models.py`, `src/query_man/registry.py`, `src/query_man/catalog.py`, [`test_registry.py`](../tests/test_registry.py), [`test_catalog.py`](../tests/test_catalog.py) |
| D3 serialization/revision compatibility | `src/query_man/revision.py`, `src/query_man/metadata_store.py`, `src/query_man/metadata.py`, [`test_revision.py`](../tests/test_revision.py), [`test_metadata_store.py`](../tests/test_metadata_store.py) |
| 명시된 Runtime lifecycle Protocol | `src/query_man/query.py`의 `RuntimeQueryExecutor`, `src/query_man/models.py`의 `RuntimeCatalogProvider`, [`test_query.py`](../tests/test_query.py), [`test_catalog.py`](../tests/test_catalog.py) |
| Runtime required capability validation/direct lifecycle | `src/query_man/app.py`, [`test_managed_mode.py`](../tests/test_managed_mode.py), [`test_http.py`](../tests/test_http.py) |
| Offline composition 예외 | `src/query_man/assurance_cli.py`, [`test_assurance_cli.py`](../tests/test_assurance_cli.py), [Assurance module](modules/assurance/README.md#offline-cli-composition-contract) |
| 허용 dependency graph | [Module index](modules/README.md#허용-의존-방향) |
