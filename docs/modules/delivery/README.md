# Delivery Module

Status: Active

## 30초 요약

Delivery는 caller를 인증·인가하고 Source Catalog, Metadata와 Guarded Query의 application service를 하나의
HTTP API로 제공합니다. DB 정책을 transport에서 다시 구현하지 않습니다.

## 책임과 interface

- Loopback anonymous, single opaque API token과 opaque access-policy token 인증
- Query/operator capability와 source authorization
- `GatewayService`: source list, metadata context와 guarded query
- HTTP strict body/header validation, public response/error와 disconnect propagation
- `/health`, `/ready`, `/admin/health`, `/admin/metrics`

업무 API는 `GET /sources`, `POST /meta`, `POST /query`이며 `/meta` input은 `source_id` 하나입니다.
Operator만 admin monitoring endpoint를 사용할 수 있습니다. Non-loopback bind는 API token 또는
access-policy가 없으면 startup을 거부합니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `delivery/access.py` | Caller context, capability, access-policy와 audit identity |
| `delivery/gateway.py` | Transport-facing application operations |
| `delivery/app.py` | FastAPI routes, strict validation, middleware, disconnect와 lifespan |

Runtime만 concrete providers를 조립합니다. Delivery는 source YAML, catalog adapter나 query executor private
state를 직접 읽지 않습니다.

## 불변조건과 승인

- Authentication 뒤 source authorization을 metadata/query보다 먼저 수행합니다.
- Token, request body, SQL, literal, DSN과 internal exception을 log/error에 반사하지 않습니다.
- Disconnect는 running query cancel·rollback으로 전파합니다.
- HTTP path/method/body/response/error, authentication과 authorization 의미는 별도 승인 대상입니다.

## 검증

```bash
uv run pytest tests/test_access.py tests/test_http.py tests/test_integration.py
```

## 집중해서 읽을 범위

Auth는 `access.py`, `authentication.py`와 access/runtime config tests, business wire는 `gateway.py`,
`app.py`와 HTTP tests, disconnect/lifespan은 integration과 Runtime cleanup tests까지 읽습니다.
