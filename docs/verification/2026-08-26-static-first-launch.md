# Static Non-RLS First-Launch Acceptance — 2026-08-26

Date: 2026-08-26

Status: Repository acceptance complete; protected environment execution pending LAUNCH-02

Decision: [ADR 0025](../decisions/0025-static-non-rls-first-launch.md) `LAUNCH-01-A`

Implementation commit: `7f2c2c6ef7d7cec4aa4cabef434a62afd5ba835c`

Remote acceptance: [GitHub Actions run 32927736174](https://github.com/hc-hyun/query-man/actions/runs/32927736174)

## Accepted Scope

- Static bootstrap authority에는 reviewed source `development-issues`, `market-voc`만 있다.
- Query Man serving process는 한 개이며 soak용 두 번째 replica는 중지했다.
- RLS source는 manifest, injected Runtime, managed publish/rotate, cold projection, QueryService와 direct
  executor 경로에서 data access 전에 fail-closed한다.
- Reader connection은 SQL 실행 전 PostgreSQL 18과 server/client UTF-8을 검사하고 mismatch connection을
  폐기한다.
- Final result는 PostgreSQL OID `20, 21, 23, 25, 1082, 1184, 1700`만 첫 fetch 전에 허용한다.
  Boolean과 broader scalar/collection OID는 first-launch success 범위 밖이다.
- RowDescription에서 base OID로 평탄화되는 scalar domain은 static Runtime과 offline Assurance Catalog가
  publication 전에 거부한다. Managed 기본 Catalog 동작은 바꾸지 않았다.
- SQL policy v3, pinned upstream image, exact ready body와 application revision label을 같은 기준선에서
  검증했다.

## Immutable Identities

| Item | Accepted value |
|---|---|
| Implementation commit / OCI revision | `7f2c2c6ef7d7cec4aa4cabef434a62afd5ba835c` |
| Local application image ID | `sha256:1cc9878c128e686b4c60e045d1a38609213c2944fb4efd06e19e65599749eac5` |
| PostgreSQL image | `postgres:18.6-bookworm@sha256:1c59e2c3c818eaa0f0628f695b36e7c9e362d6b219b36a54a32df645cbd7e1af` |
| SQL policy v3 | `sha256:2e94db36095f11f2e9cc4e804666598f79a2ee956002ffa60dbe26bc6ee81388` |
| `development-issues` metadata revision | `sha256:bc8f0e8463bbde03749d7aed3f1d50210f31462f6578814aa6bbaba89753d50b` |
| `market-voc` metadata revision | `sha256:5a28fecb7616e9cc35d936b15aa6b903508d84941e5e9317ecb81030632bb799` |
| Verified dataset | 기존 query 9개: development 4개, market 5개 |

Local application image ID는 이 workstation의 BuildKit image identity다. Protected registry artifact
digest나 실제 배포 환경의 change record를 뜻하지 않는다.

## Executed Acceptance

| Command or gate | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 29 source files |
| `uv run pytest` | PASS — 697 passed, 97 deselected |
| `uv run pytest -m integration` | PASS — 85 passed, 709 deselected |
| `uv run query-man-evaluate --root .` | PASS — 16 cases, relation accuracy/answerability recall 1.0 |
| `uv run query-man-verify --root .` | PASS — 9/9, existing revisions/columns/rows/result hashes 유지 |
| Revision label equality + `scripts/verify-container.sh` | PASS — exact label, ready body, unauthenticated 401, non-root/read-only image, HTTP/MCP query |
| `uv run pytest -m 'mcp_server and not soak' -s` | PASS — 9 tests; exact tool/query, parallel sessions, saturation and recovery |
| Locked dependency audit | PASS — known vulnerability 0 |
| Git history secret scan | PASS — 109 commits, finding 0 |
| GitHub Actions `verify` | PASS — static/unit, integration, quality and verified query gates |
| GitHub Actions `container` | PASS — SHA-labelled image, exact readiness, MCP usability/load |
| GitHub Actions `security` | PASS — dependency, history, repository configuration, PostgreSQL and application image scans |

GitHub runner의 tool mirror 404는 GitHub Releases fallback으로 복구된 warning이며 세 job의 결론은 모두
success였다.

## Preserved State

- `config/verified-queries.yaml`, persisted format와 current nine result hash를 수정하지 않았다.
- 두 metadata revision과 canonical result encoding은 그대로다.
- Control DB schema, generation, historical RLS row와 기존 verification evidence를 migrate, update 또는
  delete하지 않았다.
- Managed source lifecycle 구현은 보존하되 static launch에서 활성화하지 않는다.
- Local PostgreSQL data volume은 보존했고 물리 삭제를 수행하지 않았다.

## Not Proven Here

이 기록은 local fixture와 implementation commit의 remote CI를 증명한다. 대상 protected environment의
접근 권한, TLS, 비밀값 주입, backup/restore, source·DDL·role/settings inventory, registry artifact
digest, traffic drain/route, stop condition과 rollback drill은 실행하지 않았다. 해당 작업은
[active TODO의 `LAUNCH-02`](../development-todo.md#protected-environment-execution)에서 환경별 승인과
새 change record/evidence를 요구한다.
