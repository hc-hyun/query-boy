# ADR 0006: MCP Transport And Text-to-SQL Workflow

Status: Accepted

Date: 2026-08-23

## Context

Query Man의 MCP 경로가 HTTP 경로와 별도 registry, 인증 또는 query 실행기를 가지면 같은
질문이 transport에 따라 다른 source와 비용 경계를 사용할 수 있다. MCP client가 host,
credential 또는 임의 tool을 선택하도록 하면 server-side source registry도 우회된다.
Metadata revision이 바뀐 뒤 기존 SQL을 그대로 재전송하는 흐름도 schema 일관성을
보장하지 못한다.

## Decision

공식 Python MCP SDK 2.x의 stateless Streamable HTTP transport를 사용하고 단일 `/mcp`
endpoint를 제공한다. JSON response mode를 사용하며 request body는 1 MiB로 제한한다.
SDK의 host 검증과 애플리케이션의 bind 설정을 함께 적용한다.
Application은 POST media type과 Authorization header를 exact/단일 값으로 먼저 검증하고,
`mcp-protocol-version`도 정확히 한 개의 `2026-07-28` 값만 허용한다. Header가 누락되거나
이전·미지원 값 또는 중복 값이면 child SDK에 전달하기 전에 bounded protocol error로
거부한다. 개발 단계에서는 이전 initialize handshake나 version별 compatibility branch를
유지하지 않으며 version 변경을 명시적인 server/client 동시 upgrade로 취급한다.
tool argument model은 coercion과 추가 field를 거부한다. Argument validation 오류는
`INVALID_REQUEST`와 다음 bounded detail로 반환한다.

```text
{
  action: CALL_GET_CONTEXT | CORRECT_ARGUMENTS,
  retryable: true,
  issues: [{path, reason_code, message}],
  truncated: boolean
}
```

Revision 누락·형식 오류는 `CALL_GET_CONTEXT`, 나머지 argument 오류는 `CORRECT_ARGUMENTS`로
안내한다. Issue는 최대 8개이며 `path`는 알려진 tool field 또는 `arguments`만 사용한다.
`reason_code`는 `ARGUMENT_REQUIRED`, `ARGUMENT_FORMAT_INVALID`, `ARGUMENT_LENGTH_INVALID`,
`ARGUMENT_TYPE_INVALID`, `ARGUMENT_OUT_OF_RANGE`, `ARGUMENT_NOT_ALLOWED`, `ARGUMENT_INVALID`의
고정 집합이다.
Pydantic type·URL·input과 알 수 없는 추가 field 이름은 반사하지 않는다. `retryable=true`는
`action`을 수행한 뒤 한 번만 재시도해도 된다는 뜻이며 blind retry를 허용하지 않는다.
Tool output schema validation 실패는 세부를 숨긴 generic `INTERNAL_ERROR`로 만들고 실패
audit/metric으로 기록한다. Application 오류는 structured error payload를 유지하면서 MCP
`isError=true`로 표시한다.

MCP child application은 FastAPI application 아래에 mount한다. Parent bearer middleware가
인증한 `CallerContext`를 request-local `ContextVar`로 전달하고, MCP tool은 HTTP와 같은
`GatewayService`를 호출한다. 따라서 source authorization, metadata revision, AST 검증,
budget, query cancel/rollback과 public error reason code를 transport별로 다시 구현하지
않는다.

Tool은 다음 세 개로 고정한다.

```text
list_sources()
get_context(source_id, question, max_objects=2)
query(source_id, sql, metadata_revision, sql_policy_revision)
```

입력 schema는 HTTP와 같은 길이, revision 형식과 `max_objects` 1~4 범위를 갖고 host,
database, role 또는 credential field를 노출하지 않는다. Metadata와 result payload 크기는
source budget으로 제한하며 MCP protocol serialization overhead는 그 payload budget에
포함하지 않는다. `get_context`의 자연어 tool description도 `max_objects`가 정수 1~4이고
기본값이 2임을 명시한다. `get_context`와 `query`의 description은 context가 반환한
exact `metadata_revision`과 `sql_policy_revision`을 같은 `query` 호출에 모두 전달하라고
명시한다. 응답의 `sql_capabilities`는 validator의 단일 allowlist에서 직접 생성한
정렬된 `functions`, `cast_types`, `unqualified_cast_types`를 포함한다. 전역 validator
정책은 source metadata revision과 분리된 `sql_policy_revision`으로 digest하고 query 시 함께
검사한다. Capability도 metadata response byte 상한과 MCP output schema에 포함한다.

Parent application의 바깥쪽 ASGI middleware는 각 `/mcp` 요청에 server-generated request ID를
부여하고 request arrival부터 response start와 final response body 전달까지의 시간, response
byte, HTTP status와 outcome만 INFO event와 aggregate metric으로 기록한다. Request ID는 같은
tool completion event로 전달해 `mcp_call_id`와 성공 query의 `query_id`에 연결한다. Header,
body, token, question과 SQL은 기록하지 않으며 final-body 시간은 ASGI server에 넘긴 시점이지
client 수신이나 model 재개 시각이 아니다.

공통 [`query-man-text-to-sql` Skill](../../skills/query-man-text-to-sql/SKILL.md)은 다음
workflow를 담당한다.

- `unsupported`와 `needs_clarification`에서는 `query`를 호출하지 않는다.
- Returned grain, business predicate, approved join과 composition hint만 사용한다.
- Returned SQL capability 안의 함수와 cast form만 사용한다.
- 같은 context의 exact `metadata_revision`과 `sql_policy_revision`을 함께 전달한다.
- `METADATA_REVISION_MISMATCH`이면 context를 다시 받고 새 metadata로 SQL을 재생성해 한
  번만 재시도한다.
- `INVALID_REQUEST`와 `QUERY_INVALID`이면 bounded `action`과 reason을 따라 사용자 의미를
  보존하는 수정이 명확할 때만 한 번 교정하고, 두 번째 실패에서는 중단한다.

안전 정책은 Skill에 맡기지 않는다. Skill이 잘못된 SQL이나 stale revision을 보내도
gateway와 PostgreSQL hard limit이 거부한다.

## Consequences

- MCP와 HTTP가 하나의 source inventory, caller policy와 execution budget을 사용한다.
- Stateless transport이므로 대화나 source 선택 상태는 client가 관리하고 매 query마다 두
  revision을 명시한다.
- 기존 argument validation의 `structured_content is None`이나 SDK가 만든 validation
  문자열을 파싱하던 client는 structured `INVALID_REQUEST.details`로 전환해야 한다.
  이전 문자열 error format을 동시에 제공하는 compatibility shim은 두지 않는다.
- SDK 2.0의 modern JSON response 경로는 ASGI disconnect를 감시하지 않으므로 Query Man의
  `query` tool이 request disconnect와 gateway 실행을 경쟁시켜 PostgreSQL cancel/rollback을
  전파한다.
- Strict argument/output validation은 MCP SDK 2.0의 private tool metadata에 일부 의존한다.
  `uv.lock`의 MCP/Pydantic version을 바꿀 때는 in-memory API compatibility test와 실제 MCP server
  gate를 함께 실행해야 한다.
- 이전 handshake와 protocol version은 지원 대상이 아니므로 legacy cancellation이나
  stateful compatibility session도 제공하지 않는다. 지원 version의 POST disconnect와
  database timeout이 각각 조기 취소와 최종 실행 상한이다.
- HTTP lifecycle과 tool duration의 차이는 SDK pre-dispatch/serialization 구간을 좁히지만,
  client scheduling·network receive·JSON decode와 model reasoning은 client-side trace 없이는
  귀속할 수 없다.
- Protocol version을 바꿀 때는 parent header gate, 공식 client mode, raw transport 회귀,
  container verification과 운영 문서를 한 변경에서 함께 갱신한다.
- 현재 인증은 기존 bearer policy를 재사용한다. OAuth discovery나 별도 MCP identity
  provider는 이 ADR의 범위가 아니다.
- Immutable control-plane revision이 도입되어도 tool schema와 application service 경계는
  유지한다.
