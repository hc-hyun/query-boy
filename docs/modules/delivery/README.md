# Delivery Module

Status: Physical package boundary active

## 목적

### 30초 요약

Delivery는 Query Man의 **현관**이다. 외부 요청에서 caller를 확인하고, 허용된 application 기능을
호출한 뒤 결과를 HTTP 또는 MCP 형식으로 돌려준다. Metadata를 만드는 방법이나 SQL을 안전하게
실행하는 방법은 각각의 provider에 맡기며 transport마다 다시 구현하지 않는다.

```text
HTTP/MCP 요청 -> 인증·인가 -> GatewayService -> Metadata 또는 Guarded Query -> 안전한 응답
```

HTTP와 MCP가 같다는 말은 endpoint가 모두 같다는 뜻이 아니다. 공통 data operation의 caller 권한,
성공 결과, 오류 의미와 disconnect cancellation이 같다는 뜻이다.

| 구분 | 현재 상태 |
|---|---|
| Static launch data surface | `development-issues`, `market-voc`를 단일 replica의 HTTP와 MCP로 제공 |
| Operator cancel | HTTP에만 있으며 explicit operator만 사용 가능 |
| Managed administration | `query_man.managed` package에 보존되며 managed authority에서만 route를 등록함 |

현재 기준은 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)다.

## 소유 책임

- Server가 결정하는 `CallerContext`, bearer/access policy와 operator capability
- `GatewayService` application facade와 authorization-before-work 순서
- HTTP route, request/response, status와 public error envelope
- MCP protocol/version, 세 tool, strict argument와 result envelope
- HTTP/MCP 공통 operation의 성공·오류·cancel 의미 일치
- Header, body, Host/Origin과 admin path/query의 bounded validation
- Client disconnect를 query cancellation으로 전달하는 transport lifecycle
- Request/caller/source 정보 중 log나 metric에 기록하면 안 되는 범위

## 소유하지 않는 책임

- Source definition, registry mutation과 Control DB state transition
- Physical catalog, metadata revision과 context 선택 algorithm
- SQL AST policy, admission, transaction, cancel/rollback 구현과 result encoding
- Concrete adapter 조립, process lifecycle와 health 상태 계산
- PostgreSQL schema, reader role와 credential 저장
- MCP source administration 또는 MCP operator-cancel tool

## 현재 코드 위치

| 위치 | 역할 |
|---|---|
| [`delivery/access.py`](../../../src/query_man/delivery/access.py) | `CallerContext`, `AccessPolicy`와 token 확인 |
| [`delivery/diagnostics.py`](../../../src/query_man/delivery/diagnostics.py) | 동의된 question/SQL capture를 Runtime sink에 전달하는 port |
| [`delivery/gateway.py`](../../../src/query_man/delivery/gateway.py) | Transport-independent `GatewayService` |
| [`delivery/mcp_server.py`](../../../src/query_man/delivery/mcp_server.py) | MCP server, schema, error와 disconnect |
| [`delivery/http_validation.py`](../../../src/query_man/delivery/http_validation.py) | HTTP/MCP/admin 공통 JSON Content-Type 검사 |
| [`managed/source_admin_routes.py`](../../../src/query_man/managed/source_admin_routes.py) | Managed-only admin HTTP validation과 Control Plane use-case 호출; static composition은 import·등록하지 않음 |
| [`delivery/app.py`](../../../src/query_man/delivery/app.py) | HTTP DTO, middleware, route, handler와 transport child 조립 | Production provider/lifespan은 `runtime/composition.py` 또는 `managed/runtime.py`가 주입 |
| [`errors.py`](../../../src/query_man/errors.py) | `AppError` public carrier와 external rendering; domain 오류 발생 의미는 producer 소유 |
| `config/access-policies*.yaml` | Caller identity와 capability 입력 |
| [`query-man-text-to-sql`](../../../skills/query-man-text-to-sql) | MCP consumer workflow; safety enforcement가 아님 |
| [`test_http.py`](../../../tests/test_http.py), [`test_mcp.py`](../../../tests/test_mcp.py) | Static HTTP/MCP surface, source-admin route 부재와 common transport behavior |
| [`test_managed_http.py`](../../../tests/test_managed_http.py), [`test_managed_runtime_startup_cleanup.py`](../../../tests/test_managed_runtime_startup_cleanup.py) | Managed admin wire/use-case mapping과 managed parent/child lifespan boundary |

`GetContextSuccessOutput`은 현재 `delivery/mcp_server.py`에 있지만 HTTP `/meta`도 사용하는 Delivery 공통 wire
format이다. Managed admin adapter는 물리적으로 managed package에 있지만 external wire owner는 계속
Delivery다. `delivery/app.py`나 `errors.py`를 바꾸면 [Runtime](../runtime/README.md)과 오류 producer도 확인한다.

## 제공 인터페이스와 소유 경계

Python application interface, HTTP/MCP external wire, 인증 정책과 lifecycle은 서로 다른 변경 범주다.
아래 Python shape와 호출 단위 input/output/domain-error 의미만 official module interface다.

### GatewayService application interface

다음은 conceptual 표기가 아니라 현재 Python signature다.

```python
@dataclass(frozen=True)
class DiagnosticConsent:
    version: Literal[1]
    receipt_id: str
    expires_at: datetime

@dataclass(frozen=True)
class CallerContext:
    caller_id: str
    tenant_id: str
    operator: bool = False
    diagnostic_consent: DiagnosticConsent | None = None
    subject_id: str | None = None

class DiagnosticCapture(Protocol):
    def capture_question(self, caller: CallerContext,
        source_id: str, question: str) -> None: ...
    def capture_sql(self, caller: CallerContext,
        source_id: str, sql: str, query_id: str) -> None: ...

def caller_audit_fields(caller: CallerContext) -> dict[str, str]: ...

class GatewayService:
    def __init__(
        self,
        registry: SourceReader,
        metadata: MetadataService,
        queries: QueryService,
        diagnostic_capture: DiagnosticCapture | None = None,
    ) -> None: ...

    def list_sources(self, _caller: CallerContext) -> dict[str, object]: ...

    async def get_context(
        self,
        caller: CallerContext,
        source_id: str,
        question: str,
        max_objects: int,
    ) -> dict[str, object]: ...

    async def query(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        metadata_revision: str,
        sql_policy_revision: str,
    ) -> dict[str, object]: ...

    async def cancel_query(
        self,
        caller: CallerContext,
        query_id: str,
    ) -> dict[str, str]: ...
```

`GatewayService`는 provider의 public application result와 safe domain error만 전달한다. `CallerContext`는
server가 만들며 client request field가 아니다. `diagnostic_consent`와 `subject_id`도 access policy와
Runtime key가 만든 trusted server state이고 HTTP/MCP request field가 아니다. Capture port는 submit/storage
실패를 data operation에서 격리하며 concrete encryption/persistence는 Runtime이 소유한다.
`AccessPolicy.with_subject_identifier(callable) -> AccessPolicy`는 authenticated caller graph를 그대로
복사하면서 audit `subject_id`만 채우며 token digest, consent, operator와 tenant 의미를 바꾸지 않는다.

### 현재 data API

| 기능 | HTTP | MCP |
|---|---|---|
| Source 목록 | `GET /sources` | `list_sources()` |
| 질문별 context | `POST /meta` | `get_context(...)` |
| Guarded query | `POST /query` | `query(...)` |
| Query cancel | `DELETE /queries/{query_id}` | 없음 |
| Liveness/readiness | `GET /health`, `GET /ready` | 없음 |
| Operator runtime 상태 | `GET /admin/health`, `GET /admin/metrics` | 없음 |

두 transport는 같은 active source inventory, metadata/SQL-policy revision, application result,
`AppError` code/message/safe detail과 disconnect-to-cancel 의미를 사용한다. Aware datetime도 Guarded
Query가 만든 UTC `+00:00` 값을 그대로 직렬화한다. Delivery가 timezone이나 old revision을 다시
해석하지 않는다.

HTTP status와 MCP `isError`, validation issue 형식, discovery, health/admin/cancel surface는 transport에
따라 다를 수 있다.

### Caller identity and authorization policy

- `/health`와 `/ready`를 제외한 요청은 parent access policy를 통과한다. Authorization header는 최대
  하나이며 token 원문을 저장하지 않는다.
- Caller ID, tenant ID와 operator 여부는 server가 결정한다.
- Source 존재 확인은 metadata load, SQL validation과 admission보다 먼저다.
- 모든 인증 query identity와 operator는 같은 active source 목록을 본다. Caller별 source scope가 없고
  operator 여부도 visibility를 바꾸지 않는다.
- Operator만 admin endpoint와 query cancel을 사용한다. 이는 query capability에 admin/cancel을 더한
  boolean superset이다.
- Bootstrap anonymous `local-development`와 legacy `legacy-api-token`은 query-only다. Bootstrap admin은
  version 2 policy의 explicit operator가 필요하다.
- Managed mode는 version 2 policy, non-admin query identity와 explicit operator를 모두 요구한다.
  Anonymous, single API token, version 1과 source-scope field는 startup에서 거부한다.
- Version 2 caller의 optional `diagnostic_consent`는 exact version 1, receipt ID와 timezone-aware expiry를
  요구한다. Expiry equality부터 capture하지 않으며 client가 header/body/tool argument로 consent를 추가할
  수 없다. Consent 제거는 access-policy 교체와 Runtime protected purge 절차를 함께 따른다.

### Consent-gated diagnostic capture

- 일반 application/audit/MCP log에는 계속 question, SQL와 body를 넣지 않는다.
- Capture가 configured된 process는 일반 audit의 raw caller/tenant를 HMAC `subject_id`로 바꾼다. Subject는
  authorization, source visibility, rate limit, metric label이나 workflow correlation key가 아니다.
- Gateway가 caller 인증과 source 존재 확인을 끝낸 뒤 consent가 active인 `get_context` question과 `query`
  SQL만 Runtime sink에 submit한다. Unknown source, 인증/인가 실패와 list/cancel/admin은 capture하지 않는다.
- Question 원문은 encrypted payload에만 들어간다. SQL은 Guarded Query의 diagnostic renderer가 exact single
  SELECT의 모든 constant를 `NULL`로 바꾼 결과만 들어가며 invalid/multi/non-SELECT raw text는 저장하지 않는다.
- HTTP와 MCP wire는 바뀌지 않고 둘 다 같은 Gateway capture path를 쓴다. 서로 다른 request의
  question→SQL workflow 연결은 제공하지 않으며 ADR 0022 trace를 암묵적으로 활성화하지 않는다.

정확한 persisted envelope, TTL, key/budget와 purge는
[ADR 0027](../../decisions/0027-consent-gated-diagnostic-capture.md)과 Runtime/Operations가 소유한다.

### Public error and validation boundary

HTTP 오류는 `{error: {code, message, details?}}`, MCP는 같은 업무 의미의 structured tool result를
사용한다. 예상하지 못한 오류는 고정 internal error로 줄이며 raw PostgreSQL error, SQL literal,
credential, token과 5xx detail을 반환하지 않는다.

- RLS source는 인증·인가와 source 존재 확인 뒤, tenant/revision/metadata/SQL/queue/DB 작업 전에
  차단한다. HTTP는 detail 없는 `503 QUERY_UNAVAILABLE`, MCP는 같은 code/message의 detail 없는
  `isError` result이며
  `TENANT_CONTEXT_REQUIRED`에는 도달하지 않는다.
- Guarded Query가 final result OID를 거부해도 detail 없는 같은 `503 QUERY_UNAVAILABLE`을 전달한다.
  허용 타입은 `int8`, `int2`, `int4`, `text`, `date`, `timestamptz`, `numeric`이며 검사는 duplicate
  column 거부 뒤 첫 fetch 전에 수행된다.
- Reader compatibility 실패는 provider의 safe domain error 그대로 rendering하고 connection detail이나
  lower-level exception을 재분류하지 않는다.

MCP argument 오류는 `INVALID_REQUEST`와 아래 bounded detail을 반환한다.

```text
action: CALL_GET_CONTEXT | CORRECT_ARGUMENTS
retryable: true
issues: [{path, reason_code, message}]  # 최대 8개
truncated: boolean
```

Revision 누락·형식 오류의 action은 `CALL_GET_CONTEXT`, 나머지는 `CORRECT_ARGUMENTS`다. Reason code는
`ARGUMENT_REQUIRED`, `ARGUMENT_FORMAT_INVALID`, `ARGUMENT_LENGTH_INVALID`, `ARGUMENT_TYPE_INVALID`,
`ARGUMENT_OUT_OF_RANGE`, `ARGUMENT_NOT_ALLOWED`, `ARGUMENT_INVALID`만 사용한다. 입력값, private field와
Pydantic detail을 반사하지 않으며 output validation 실패는 generic `INTERNAL_ERROR`다.

HTTP request validation은 최대 32개의 bounded path/code와 고정 message만 공개한다. Public data
route에는 전역 body-size limit이 없으며 이를 MCP/admin의 1 MiB 제한으로 오해하지 않는다.

### MCP protocol surface

- `/mcp`에서 stateless Streamable HTTP JSON mode와 protocol `2026-07-28`만 제공한다.
- Tool은 `list_sources`, `get_context`, `query` 세 개뿐이고 extra argument/type coercion을 거부한다.
- `query`는 `get_context`가 반환한 exact `metadata_revision`과 `sql_policy_revision`을 모두 요구한다.
  Revision mismatch는 context와 SQL을 새로 만든 뒤 최대 한 번 재시도하도록 안내한다.
- POST body는 1 MiB 이하이며 single JSON Content-Type/protocol-version header, Host/Origin allowlist와
  DNS rebinding protection을 적용한다.
- 한 POST의 server-generated request ID, call ID와 query ID만 연결한다. 여러 POST용 client trace는
  [ADR 0022](../../decisions/0022-w3c-workflow-trace-context.md)의 parked research다.

### 구현됐지만 현재 launch에서 비활성인 admin HTTP

Managed Control Plane을 별도로 조립했을 때만 source inventory/history, receipt, replica, usage와 여섯
mutation HTTP route를 사용한다. 전체 route와 mutation header/receipt 의미는 [current management
operations](../../source-management-plane.md#current-management-operations)과 [mutation wire
format](../../source-management-plane.md#mutation-request-and-receipt-wire-format-and-semantics)에 정리돼
있다. Static composition은 이 13개 route를 등록하지 않으므로 OpenAPI에 없고 요청은 route-not-found다.
MCP admin tool은 없다.

Delivery가 계속 소유하는 wire 불변조건은 다음과 같다.

- 모든 admin route는 operator 확인을 path/query/body validation보다 먼저 한다.
- Mutation은 exact JSON Content-Type, 1 MiB body, 최대 1,024 object member, duplicate key/non-finite
  number 거부와 idempotency/expected-state header를 사용한다.
- Replica 조회는 `limit` 기본 50, 범위 1~100과 1~80자 exclusive stable-slug cursor를 사용한다. Known
  source는 replica가 stale/unavailable이어도 200이다.
- Usage 조회는 query parameter를 받지 않고 한 DB snapshot의 inclusive 31일 window, 최대 1,000
  gateway row와 `lower_bound=true`를 반환한다. Missing/failed 값을 0으로 만들지 않는다.
- Monetary cost는 status `not_configured`, reason `PROVIDER_NOT_CONFIGURED`, last attempt `null`이고
  amount/currency/provider field가 없다.
- Replica/usage response와 audit에 credential, connection, caller/tenant, question/SQL, query ID와 raw
  error를 넣지 않는다.

Replica/usage result의 source semantics는 [Control Plane observability](../control-plane/observability.md),
HTTP projection과 validation은 Delivery가 소유한다. Unknown source는 404, Control read/projection
failure는 bounded `SOURCE_CONTROL_UNAVAILABLE` 503이다.

### Readiness wire and launch acceptance

`GET /ready`는 `{"status": <state>}`를 반환하고 `ready`/`degraded`는 HTTP 200, 나머지는 503이다.
Runtime의 Compose healthcheck만 body가 정확히 `{"status":"ready"}`일 때 healthy로 판정한다. 이는 새
Delivery status나 field가 아니다.

### Child lifespan ownership and cleanup rule

- MCP child는 자신의 `enter` 도중 만든 partial resource를 스스로 정리한다.
- Child `enter` 실패 시 Runtime parent는 진입하지 못한 child의 `exit`를 호출하지 않는다.
- Runtime은 child 진입 전 parent composition이 만든 최상위 resource만 startup-failure rule에 따라
  정리한다. 이 경계는 정상 shutdown 순서나 HTTP/MCP wire를 바꾸지 않는다.

## 소비 인터페이스와 전제

| Provider | 직접 소비하는 공개 interface | Delivery의 의무 |
|---|---|---|
| [Source Catalog](../source-catalog/README.md) | `SourceReader`; admin validation용 `SourceEnvironment`, `Identifier`, `StableSlug` | Sanitized source만 공개하고 pattern/range를 재정의하지 않음 |
| [Metadata](../metadata/README.md) | Context의 plain list/dict application result | Internal immutable graph를 직접 serialize하지 않음 |
| [Guarded Query](../guarded-query/README.md) | Query/cancel result와 safe domain errors | OID/revision/reader policy를 재정의하지 않음 |
| [Control Plane](../control-plane/README.md) | Managed projection/mutation use case, `CONTROL_SEQUENCE_MAX`, `PublishVerifiedQueryInput`, `VerifiedExpectedInput` | Preserved admin surface에만 사용; persistence/Assurance DTO를 import하지 않음 |
| [Runtime](../runtime/README.md) | Aggregate health/operations state와 lifecycle context | Health 계산이나 production composition을 Delivery로 옮기지 않음 |

`GatewayService`는 concrete registry 대신 `SourceReader`를 받아 `list/get`만 사용한다. Admin route는
Control Plane public input만 만들고 `managed/source_store.py`나 Assurance `assurance/verified.py`를 import하지 않는다.
Metadata/Source Catalog가 내부 tuple/read-only mapping을 사용해도 `/meta`와 `get_context`의 기존
array/object projection은 유지한다.

Control Plane 소비는 managed admin용이며 current static launch에는 참여하지 않는다. Source Catalog
validation type의 pattern/range를 바꾸면 admin wire compatibility도 함께 검토한다.

## 불변조건

- HTTP와 MCP의 공통 operation은 하나의 `GatewayService` path를 사용한다.
- 인증·인가 뒤에만 source 작업을 하며 RLS와 unsupported final OID는 정해진 순서에서 detail 없는
  `QUERY_UNAVAILABLE`로 끝난다.
- Caller는 DSN, credential, DB role이나 tenant context를 선택할 수 없다.
- Unknown/unauthorized source, SQL, question, token, credential과 내부 오류를 일반 log/metric에 노출하지
  않는다. Active consent 뒤 authorized content만 ADR 0027의 별도 encrypted capture에 저장한다.
- Disconnect는 task cancellation을 통해 database cancel/rollback으로 전달된다.
- MCP SDK workaround가 protocol rule이나 domain policy를 대신하지 않는다.
- Query-facing source 목록에는 connection과 internal control state가 없다.
- Admin payload는 Control Plane public input만 사용한다.
- Static first launch는 두 source와 단일 replica이며 managed admin adapter를 import하거나 13개 route와
  hot onboarding을 활성화하지 않는다.

## 모듈 내부 변경

Official interface, external wire, policy와 lifecycle 의미가 같다면 middleware/route helper, token lookup,
serialization, MCP SDK adapter와 audit implementation 내부는 정리할 수 있다.

## 사용자 승인이 필요한 경계 변경

다음 의미 변경은 실제 범주를 구분해 먼저 승인받는다.

- HTTP path/method, request/response field, status, size/range/default
- MCP tool/schema/description, protocol version, transport, Host/Origin/header/body policy
- `GatewayService`, `CallerContext` input/output/domain-error 의미
- 인증·인가/source-existence 순서, shared visibility, tenant trust와 operator capability
- Public error/validation envelope와 unknown-vs-unauthorized 처리
- Source summary, admin/cancel/replica/usage path와 projection 의미
- Admin idempotency/expected-state, receipt, pagination, freshness/lower-bound와 cost placeholder 의미
- Audit 허용 field, disconnect/cancel과 child cleanup lifecycle
- RLS quarantine, unsupported-result mapping과 detail-free 5xx invariant
- `/ready` wire. Container health, inventory, replica 수와 managed activation은 Runtime/operational 범주다.

Delivery는 audit event 의미와 기록 금지 대상을 소유하고 Runtime은 formatter/redaction 구현을 소유한다.
승인안에는 관련 provider와 기존 HTTP/MCP client compatibility를 포함한다. Protected environment 실행은
별도 operational authorization이다.

## 검증

```text
uv run pytest tests/test_registry.py tests/test_access.py tests/test_diagnostic_capture.py \
  tests/test_http.py tests/test_mcp.py \
  tests/test_text_to_sql_skill.py
uv run pytest tests/test_managed_http.py tests/test_managed_runtime_startup_cleanup.py
```

실제 MCP server는 `uv run pytest -m 'mcp_server and not soak' -s tests/test_mcp_server.py`로 별도
검증한다. Protocol/socket 변경은 disconnect integration을, managed admin 변경은
`test_managed_http.py`와 source-admin/control-store/hidden-import test를, managed child lifespan 변경은
`test_managed_runtime_startup_cleanup.py`를, concurrency/session 변경은 MCP load/soak를 추가한다. 완료 전
root 전체 gate를 실행한다.

## 집중해서 읽을 범위

1. 이 문서와 [module index](../README.md)
2. 변경하는 access/gateway/HTTP/MCP/admin/error code와 focused test
3. 호출하는 provider의 input/output/error interface
4. 직접 관련된 [guarded query](../../decisions/0002-guarded-query-contract.md),
   [authorization](../../decisions/0004-caller-source-authorization.md),
   [MCP](../../decisions/0006-mcp-transport-and-workflow.md),
   [container](../../decisions/0015-containerized-local-runtime.md),
   [shared access](../../decisions/0017-shared-source-access-and-resource-tier.md) ADR
5. Current launch 변경이면 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
6. `delivery/app.py` child lifecycle 변경이면 Runtime composition/lifecycle rule

Catalog SQL, Control persistence transaction과 query executor 내부는 위 interface나 경계 의미가 바뀔
때만 추가로 읽는다.
