# YAML Source Authority Removal Verification — 2026-08-29

Date: 2026-08-29

Status: Repository removal acceptance complete; protected deployment and external Control DB disposition not performed

Decision: [ADR 0030](../decisions/0030-git-reviewed-yaml-source-authority.md)
`QB-YAML-SOURCE-AUTHORITY-20260829`

Baseline: `7b4e717c7775ff262c716d36f6f172aadc162892`

## Accepted Scope

- `config/sources/*.yaml`, `config/verified-queries.yaml`과 `config/budget-profiles.yaml`만 각각 source,
  verified query와 budget authority로 남겼다.
- Runtime은 retired managed/Control 설정을 값 비공개 상태로 fail-closed하고 YAML과 Control DB를 merge,
  poll 또는 fallback하지 않는다.
- `qm source`는 local YAML의 `list`, `show`, `validate`만 제공하며 network, database, credential 또는
  mutation 경로를 갖지 않는다.
- `query_man.managed`, Control migration/store, admin mutation route, hot reload, replica/usage reporter,
  managed acceptance fixture와 recovery procedure를 제거했다.
- PostgreSQL comment와 catalog type/precision/scale는 metadata 입력으로 유지하되 authority, allowlist,
  masking, grant 또는 PII 노출 승인을 대체하지 않는다.
- AuthBridge OAuth2 bearer access-token 검증은 issuer Discovery의 `jwks_uri`, JWKS cache/unknown `kid`
  refresh, fixed algorithm, exact issuer, audience, time claim과 scope/role/group 검증 경계를 유지했다.

## Executed Acceptance

| Command or gate | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 37 source files |
| `uv run pytest -q -m 'not integration and not load and not soak and not mcp_server'` | PASS — 495 passed, 55 deselected |
| `uv run pytest -q -m 'integration and not load and not soak and not mcp_server'` | PASS — 42 passed, 508 deselected |
| `uv run pytest -q -m load tests/test_load.py` | PASS — 1 passed |
| `uv run pytest -q -m 'mcp_server and not soak' tests/test_mcp_server.py tests/test_mcp_server_load.py` | PASS — 9 passed |
| `uv run pytest -q tests/test_oauth_authentication.py` | PASS — 15 passed; Discovery/JWKS access-token validation regression 포함 |
| Documentation, onboarding Skill와 package-boundary tests | PASS — 27 passed |
| Skill Creator `quick_validate.py` | PASS — `Skill is valid!` |
| `qm source validate` and `qm source list` | PASS — YAML authority, two source, `live_database_checked: false` |
| `docker compose config --quiet` and shell syntax checks | PASS |
| `scripts/apply-db.sh` | PASS — existing local volume에 두 static source schema/seed/role validation 재적용 |
| `query-man-evaluate` | PASS — 16 cases, relation accuracy/answerability recall 1.0 |
| `query-man-verify` | PASS — existing verified queries 9/9 |
| `docker compose build query-man`, recreate and `scripts/verify-container.sh` | PASS — healthy, unauthenticated 401, non-root/read-only, actual MCP query |
| `git diff --check` | PASS |

## Preserved Boundaries

- SQL AST validation, source/schema/relation/function/operator allowlist, least-privilege reader, read-only
  transaction, timeout/concurrency/row/byte limits와 cancel/rollback 처리를 유지했다.
- Existing two-source metadata revisions와 nine verified-query result hashes를 변경하지 않았다.
- ID token과 refresh token은 API credential로 받지 않으며 service는 refresh나 token acquisition을 하지
  않는다. Token과 Authorization header의 non-logging 경계도 유지했다.
- Historical ADR, dated verification 원문과 기존 roadmap row를 소급 수정하거나 삭제하지 않았다.

## Explicit Non-Actions And Limits

- 어떤 live 또는 protected Control DB에도 접속하지 않았고 Control schema/table/row, credential,
  backup이나 data를 inspect, migrate, decrypt, delete 또는 drop하지 않았다.
- Local Compose PostgreSQL data volume은 보존했다. 컨테이너와 application image만 current Compose와
  working tree로 재생성했으며 static fixture schema/seed를 idempotent하게 재적용했다.
- Protected environment 배포, GitHub Enterprise branch protection 결과, external Control DB inventory,
  retention 또는 폐기는 이 evidence가 증명하지 않는다. 각각 별도 접근 범위, target, stop condition,
  rollback과 실행 승인이 필요하다.
