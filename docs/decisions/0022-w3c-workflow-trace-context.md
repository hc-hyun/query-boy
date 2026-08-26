# ADR 0022: W3C Workflow Trace Context

Status: Proposed read-only prework — priority gate and user approval required before implementation

Date: 2026-08-26

## Priority Boundary

이 문서는 `TRACE-01` 선택지를 미리 검토한 초안이다. 현재 TODO상 `ENC`, `TIME-03`과 `COST`가
우선이므로 사용자가 P4 결정과 `TRACE-02`~`TRACE-04` 구현·검증 전체를 명시적으로 reprioritize하거나
앞 priority를 완료하기 전에는 `TRACE-01`을 공식 시작하지 않고 header, audit field, metric과 module
interface/API/policy를 바꾸지 않는다. `TRACE-01` 결정만 먼저 승인하는 경우 구현은 기존 priority gate 뒤에 남는다.

이 제안은 하나의 module interface가 아니다. Runtime trace scope는 module interface, Delivery
`traceparent` header 의미는 external API, audit/redaction/counter는 observability policy다. 각 범주는
같은 승인안에서 조율하되 실제 분류와 owner를 따로 기록한다.

## Context

현재 MCP HTTP POST마다 server-generated UUID4 `mcp_http_request_id`, tool call마다 UUID4
`mcp_call_id`, query마다 UUID4 `query_id`가 있다. Request → call → query lifecycle은 한 POST 안에서
연결되지만 client가 여러 POST에 걸쳐 수행하는 workflow와 revision retry는 연결하지 못한다.
`query_id`는 active cancel key와 PostgreSQL `application_name`에도 쓰이므로 workflow ID로 대체할 수
없다. Metrics와 Control usage에는 trace/caller/tenant dimension이 없다.

[W3C Trace Context Recommendation](https://www.w3.org/TR/trace-context/)의 `traceparent`는 lowercase
hex의 version, 128-bit trace ID, 64-bit parent ID와 flags를 정의하고 zero ID를 금지한다. Invalid
context는 새 trace로 restart할 수 있고, public ingress의 header 길이/content 검증과 random trace ID를
권고하며 trace data를 인증/신뢰 정보로 취급하지 않는다. 이 결정은 그 header의 inbound trace ID만
상관관계 힌트로 쓰는 부분 구현이다. `tracestate`, outbound propagation과 response header를 구현하지
않으므로 완전한 distributed-tracing propagation 구현이라고 주장하지 않는다.

## Options

### `TRACE-01-A` — policy-admitted fail-soft `traceparent` (recommended)

1. 적용 route는 configured authentication policy가 `CallerContext`를 성공적으로 만든 exact `POST /mcp`,
   `POST /query`와, 그 caller의 `operator=true`가 확인된 뒤 decoded path가 정확히
   `/queries/{query_id}` 한 segment인 `DELETE`뿐이다. Bootstrap local policy의 accepted anonymous
   `local-development`도 성공한 caller context이므로 in-scope이고 managed mode는 bearer가 필수다. Auth와
   operator 판정을 먼저 끝내고 그 뒤에만 case-insensitive ASGI raw `traceparent` occurrence를 읽는다.
   `/mcp`에서는 content-type와 MCP protocol validation보다 먼저 parse하므로 성공·protocol/tool error의
   outer completion이 모두 같은 trace를 가진다. `GET /sources`, `POST /meta`, admin source route, MCP GET,
   trailing-slash redirect, policy-admitted 404/405, `/health`, `/ready`, shutdown rejection/pre-auth path,
   인증 실패와 non-operator cancel은 header를 읽거나 parse/audit/counter하지 않는다. In-scope지만
   `/query` request-model 또는 cancel UUID validation 전에 실패한 요청은 disposition counter만 있고 새
   generic HTTP audit은 만들지 않는다. Gateway의 active-source check가 통과해 `query_started`가 생긴
   경우부터 existing query audit에 trace가 붙는다. Unknown source의 existing `authorization_denied`에는
   trace를 붙이지 않고 MCP tool completion만 correlation을 보존하며 HTTP는 counter만 남긴다.
   Response/status는 바꾸지 않는다.
2. 판정 순서는 case-insensitive ASGI raw header occurrence 0개면 absent → 2개 이상이거나 single raw value에
   comma byte가 있으면 duplicate → single value의 ASCII/length/format 오류면 invalid → accepted다.
   Header는 정확히 하나, ASGI에서 관측되는 value 기준 1~512 bytes이고 core와 future suffix 모두
   visible US-ASCII `0x21..0x7e`만
   허용한다. Mixed-case duplicate도 duplicate이며 duplicate payload가 invalid여도
   `restarted_duplicate`가 우선한다. Empty value는 absent가 아니라 `restarted_invalid`다. Locked
   Uvicorn/h11 transport가 leading/trailing HTTP OWS를 ASGI 전에 제거할 수 있으므로 wire OWS rejection을
   보장하지 않는다. 정규화 뒤 valid core이면 accepted이며 direct-ASGI visible whitespace/control만
   invalid다. Transport를 바꾸면 이 normalization parity를 acceptance로 다시 검증한다.
3. Core offset은 version `0:2`, delimiter `2`, trace ID `3:35`, delimiter `35`, parent ID `36:52`,
   delimiter `52`, flags `53:55`다. Version `00`은 정확히 55자
   `00-<32 lower hex>-<16 lower hex>-<2 lower hex>`이고 trace/parent all-zero, uppercase, whitespace,
   suffix와 delimiter 오류를 거부한다. Version과 flags도 two lower hex이고 version `ff`는 invalid다.
   Version `01`~`fe`는 55자 core 또는 56~512자에서 같은 fixed core offset/lowercase/nonzero를 검증하고
   index 55가 `-`여야 한다. Lone trailing `-`도 future-version opaque suffix로 허용하며 그 뒤 visible
   ASCII content는 해석하거나 보존하지 않는다. Comma는 앞 단계에서 duplicate이고 whitespace/control은
   invalid다. Flags는 sampled/reserved bit를 log level, recording, 비용이나 admission 결정에 쓰지 않는다.
4. Disposition은 다음 네 개로 고정한다.

   ```text
   accepted | generated_absent | restarted_invalid | restarted_duplicate
   ```

   Valid header는 32자 trace ID만 유지한다. Parent ID, flags, suffix와 raw header는 저장/log하지 않는다.
   Header가 없거나 invalid/duplicate면 request를 400으로 바꾸지 않고 `secrets.token_hex(16)`을
   all-zero가 아닌 값이 나올 때까지 호출해 cryptographically secure nonzero 128-bit lowercase hex
   trace ID를 새로 만든다. Clock, caller/tenant/source나 request body를 seed로 쓰지 않고 collision
   registry를 만들지 않는다. `tracestate`와 `baggage`는
   parse/store/log하지 않는다.
5. Runtime이 frozen+slots `TraceContext(trace_id, disposition)`, exact four-literal `TraceDisposition`,
   nested/finally-safe `trace_scope(context)`와
   `current_trace_context() -> TraceContext | None`, `current_trace_id() -> str | None` process-local provider
   interface를 소유하고 construction 때 trace ID/disposition을 검사한다. Scope는 exact object를 yield하고
   밖에서는 `None`이며 exception/cancel 포함 종료 뒤 `ContextVar` token으로 nested prior value를 복원한다.
   Delivery는 인증과 route scope 확인 뒤 context를 만들고 exact immutable object identity를 ASGI `scope["state"]`의
   `trace_context`에도 보존한 뒤 Runtime `ContextVar` scope를 set하며 `finally`에서 token을 reset한다.
   Async MCP/query child task는 copied context를 사용하고 request 밖 detached task로 넘기지 않는다.
   Inner auth/trace scope보다 바깥에서 request completion을 기록하는 MCP lifecycle middleware는 reset된
   ContextVar가 아니라 같은 ASGI state의 immutable object를 읽는다. Global formatter가 모든 log에
   자동 주입하지 않는다.
6. 기존 `mcp_http_request_id`, `mcp_call_id`, `query_id`, PostgreSQL `application_name`과 cancel lookup을
   그대로 유지한다. Delivery는 `_safe_call` 둘레에 별도 nested/finally-safe private MCP-call scope를
   두고 Gateway가 MCP에서 호출된 경우 existing `mcp_call_id`를 query start/success/failure/interruption
   audit에 추가한다. HTTP query에서는 이 field가 없다. Wire나 새 ID는 추가하지 않으며, 이 bridge가
   실패한 병렬 MCP query도 request→call과 call→query audit으로 구분한다.

   `trace_id`의 exact producer event는 policy-admitted MCP POST completion(기존 allowlisted caller/tenant 포함),
   MCP tool start/completion, Gateway query start/success/failure/interruption, Guarded Query execution
   failure/interruption/plan rejection, Gateway cancel-request와 executor cancel-signal이다. Auth failure에는
   붙이지 않는다. 일반 HTTP에는 새 response/request-ID 또는 completion audit을 만들지 않는다. Model/UUID
   validation과 cancel-not-found처럼 existing audit event 전의 실패는 disposition counter만 남는다.
   Operator cancel request와 그 signal에는 cancel request trace를 붙이고, 대상 query task의 terminal
   cancelled/interrupted audit에는 원래 실행 trace를 유지한다. 둘은 existing `query_id`로만 연결하며
   어느 trace도 다른 trace를 덮어쓰지 않는다.
7. Trace ID는 untrusted correlation hint다. Client는 opaque random ID만 사용해야 한다. PII,
   caller/tenant/source ID, question, SQL, token 또는 다른 의미 있는 식별자를 32-hex에 encode해서는
   안 된다(MUST NOT). Server는 이 위반을 의미적으로 검출할 수 없고 accepted ID에는 existing protected
   audit-log retention/access policy가 그대로 적용된다. Authentication/authorization, tenant/source visibility,
   idempotency, cache, admission/rate limit, cancel, sampling과 mutable-state key로 사용하지 않는다.
   Client가 ID를 forge/reuse해도 opaque ID로만 취급하고 existing protected audit-log retention/access를
   넘는 새 저장을 만들지 않는다. State를 merge/overwrite하지 않으며 운영 분석은
   existing log collector/container metadata가 제공하는 replica identity와 `(caller_id, tenant_id, trace_id)`
   및 server request/call/query ID를 함께 사용하며 application event에 새 `replica_id` field를 추가하지
   않는다. Trace용 collision registry나 dedup state를 만들지 않는다. 같은 caller가 unrelated workflow에 같은 trace를
   재사용하면 correlation이 모호해질 수 있는 잔여 위험도 인정한다.
8. Runtime structured-log allowlist에는 exact `^[0-9a-f]{32}$` fullmatch이고 all-zero가 아닌 string
   `trace_id`만 추가한다. 잘못된 type/value면 stringify/redact해 내보내지 않고 field 자체를 생략한다. Producer는 message 보간이
   아니라 structured `extra.trace_id`로만 전달한다. Raw trace header,
   `tracestate`, baggage, question, SQL, token, body, 비인가 source와 internal error는 금지한다. 다음
   replica-local, process-restart-reset aggregate counter만 추가하고 trace/caller/tenant를 metric label로
   쓰지 않는다. Cluster-global completeness를 주장하지 않는다.

   ```text
   trace_context_accepted
   trace_context_generated_absent
   trace_context_restarted_invalid
   trace_context_restarted_duplicate
   ```

   In-scope CallerContext-bearing request 하나마다 source-less counter 하나만 증가시키고 duration은 만들지 않는다.
   Process restart 뒤 아직 관측되지 않은 key의 부재는 0을 뜻한다. Gateway usage, Control DB, metadata
   revision과 PostgreSQL `application_name`에는 trace를 넣지 않는다.
9. Response header, MCP tool schema/response field와 outbound propagation은 추가하지 않는다. Missing 또는
   invalid header에서 생성한 trace ID도 response에 반환하지 않으므로 여러 POST correlation은 client가
   같은 valid trace ID를 다시 보낼 때만 가능하다. 같은 valid client trace ID는 어느 replica에서도 같은
   correlation key지만 shared registry/stickiness/persistence는 없다. 각 POST/call/query/retry는 새 server
   ID를 유지하고 revision retry의 workflow trace reuse 여부는 client resend에 달렸다. Mixed rollout에서
   old replica는 header를 무시해 correlation만 불완전하고 business response는 compatible하다. Rollback은
   parsing/audit field를 제거해도 request를 계속 처리하며 DB migration이 없고 과거 log는 보존된다.

### `TRACE-01-B` — strict invalid-header rejection

Invalid/duplicate header를 새 public `INVALID_TRACE_CONTEXT` 400으로 거부한다. Client feedback은
명확하지만 W3C restart model, auth/error parity, rolling compatibility와 기존 availability를 바꾸므로
권장하지 않는다.

### `TRACE-01-C` — server-only per-request trace

모든 client trace ID를 버리고 POST마다 server trace ID를 만든다. Forgery/collision 위험은 가장 작지만
여러 POST workflow를 연결한다는 목표를 달성하지 못한다. Custom `X-Query-Man-Trace-ID`/UUID도 표준
호환성 없이 별도 protocol을 만들므로 선택하지 않는다.

## Provider And Consumer Impact

- Provider boundaries: Runtime process-local trace scope/counter/log allowlist, Delivery auth-after exact-route
  header parser와 immutable ASGI request-state bridge
- Direct consumers: Delivery/MCP lifecycle·private MCP-call scope/Gateway audit와 Guarded Query audit propagation
- Unchanged: HTTP/MCP business schema/status, query result/hash, Source/Metadata/Control DB, authz,
  cancel/application name, metric dimensions와 persistence
- Security/privacy: policy-admitted client도 trace ID를 forge/reuse할 수 있다고 가정하며 raw header와
  sensitive input을 저장하지 않는다.

Coordinating agent가 Runtime module interface와 Delivery external wire semantics를 각각 먼저
동결한다. 구현 순서는 Runtime provider → Delivery parser/set-reset → MCP/Guarded Query consumer다.
Provider가 확정된 뒤 서로 다른 consumer 검증만 병렬화한다.

## Verification

- Missing/empty, mixed-case duplicate precedence, comma-folded duplicate, valid v00 flags `00|01|03`, future
  version 55-byte/lone-dash/visible-ASCII suffix와 ff/uppercase/zero/short/v00-suffix/non-ASCII/control/
  over-512/direct-ASGI whitespace parser corpus, real Uvicorn/h11 wire OWS normalization parity 및 generated
  nonzero `secrets.token_hex(16)` seam
- Bootstrap local anonymous in-scope와 managed bad bearer/non-operator cancel의 기존 401/403 및
  no-read/no-counter, exact decoded cancel
  path/trailing slash/404/405, health/ready/shutdown parity와 raw-header absence
- 같은 trace의 여러 MCP POST/list/context/query, revision retry와 request/call/query/executor correlation
- Failed parallel MCP query의 private call scope→Gateway `mcp_call_id` 연결, HTTP query의 call ID 부재,
  unique server IDs와 같은 trace에서 lifecycle state가 합쳐지지 않는 검증
- Auth 뒤·MCP content/protocol gate 전 parsing, exact in-scope/out-of-scope route matrix, unknown/denied source,
  unknown-source authorization-denied trace absence, protocol/output/request-model/UUID validation과 일반 HTTP
  `/query` redaction/counter-only limitation
- Parallel/nested trace ContextVar isolation, scope-outside `None`, sequential reset, ASGI-state outer MCP
  completion bridge, disconnect/task cancel/rollback과 cancel-request trace 대 target-query original trace 보존
- Two-replica same trace/parallel/retry에서 no shared state/stickiness, unique server IDs와 random absent/invalid ID
- 서로 다른 caller/tenant가 같은 client trace ID를 재사용해도 request/call/query state가 합쳐지지 않는
  격리와 process restart 뒤 disposition counter가 0에서 다시 시작하는 acceptance
- Formatter가 malformed/non-string/all-zero trace를 생략하고 producer가 `extra`만 쓰는지, trace/caller/tenant
  metric-label, operations key, GatewayUsage/Control payload absence
- No-header old client와 mixed rollout/rollback의 response/status/header/schema preservation
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`, 실제 두
  process/replica correlation을 포함하도록 확장한 `uv run pytest -m soak`

## Approval Boundary

이 문서는 정확한 제안일 뿐 현재 승인 baseline이나 구현이 아니다. 승인하려면 해당되는 모든 변경
범주와 영향을 정확히 지정해야 한다. 앞 priority가 모두 끝난 뒤에는 아래 첫 문장을 생략할 수 있다.
현재 exact implementation-ready 승인은 A만 아래에 제시한다. B/C를 선택하면 route, audit/counter,
rollout/rollback과 B의 public error envelope를 다시 exact restatement해 승인받아야 하며 ID 선택만으로
구현하지 않는다.

```text
현재 열린 ENC-01~02, TIME-03, COST-01~05보다 P4의 TRACE-01 결정과 승인된
TRACE-02~04 구현·검증을 먼저 수행하도록 전역 작업 우선순위를 명시적으로 변경한다.
TRACE-01-A를 승인한다. Configured auth policy가 CallerContext를 만든 exact POST /mcp, POST /query와
operator=true 확인 뒤 decoded exact DELETE /queries/{query_id}에서만 ASGI raw W3C traceparent occurrence를
읽고 ASGI-observable 1~512 visible ASCII bytes로 fail-soft 처리한다. Bootstrap local anonymous caller도
in-scope이고 transport가 제거한 wire OWS는 구분하지 못해 normalized valid core로 처리하는 한계를
인정한다. Auth failure/non-operator/out-of-scope에서는 읽거나 세지 않고 MCP에서는
content/protocol validation 전에 처리한다. 중복/comma가 invalid보다 우선하며 v00 exact core와 v01~fe
fixed core/lone-dash 포함 opaque visible-ASCII suffix, ff/zero/uppercase/whitespace 거부 의미를 적용하고,
유효하면 trace-id만 untrusted correlation 용도로 이어받으며, 없음·중복·무효이면 요청을
거부하지 않고 secrets.token_hex(16)으로 nonzero 128-bit random trace-id를 새로 생성한다. GET /sources,
POST /meta, admin source, MCP GET, trailing slash, 404/405, health/ready/shutdown/pre-auth, 인증 실패와
non-operator cancel route는 적용 범위에서 제외한다.
기존 request/call/query ID와 인증·인가·취소·저장 의미는 유지하고,
tracestate/baggage/response/outbound propagation, trace metric label, Control DB 및 PostgreSQL
application_name 사용은 제외한다. Runtime 소유 frozen+slots validated context와 nested/finally-safe
process-local trace scope를 Delivery가 인증 뒤 set/reset하고 immutable ASGI request-state bridge로 outer
MCP completion까지 보존한다. Delivery-private nested MCP-call scope로 failed parallel query까지 existing
mcp_call_id를 Gateway audit에 연결하고 MCP/Guarded Query가 trace를 소비한다. Client는 trace ID에
PII/caller/tenant/source/question/SQL/token을
encode하지 않으며 server가 이를 의미적으로 검출할 수 없다는 잔여 위험을 인정한다. Cancel 요청
trace와 대상 query의 원래 실행 trace를 각각 보존한다. trace_id audit field, trace/caller/tenant label이
없는 네 source-less replica-local/restart-reset aggregate disposition counter, malformed trace field 생략,
policy-admitted MCP completion의 caller/tenant 연결, unknown-source authorization_denied에는 trace를 붙이지
않는 경계, stateless multi-replica rollout/rollback 영향까지 승인한다.
```
