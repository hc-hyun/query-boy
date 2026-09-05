# Query Man

Query Man은 AI가 만든 SQL을 그대로 데이터베이스에 전달하지 않고, 승인된 PostgreSQL view만 제한된
reader로 조회하게 하는 안전한 metadata gateway입니다. AI 모델이나 데이터베이스 관리 기능은
포함하지 않습니다.

무엇을 하려는지에 따라 읽을 문서는 [문서 안내](docs/README.md)에서 바로 찾을 수 있습니다.

## 사용 흐름

HTTP client는 다음 세 endpoint를 순서대로 사용합니다.

1. `GET /sources`로 호출자가 사용할 수 있는 source를 확인합니다.
2. `POST /meta`에 `source_id`를 보내 relation·column과 두 revision을 받습니다.
3. `POST /query`에 SQL과 같은 두 revision을 보내 한 source의 읽기 전용 SQL을 실행합니다.

Application 상태는 `/health`, `/ready`, operator용 상세 상태와 process-local 지표는
`/admin/health`, `/admin/metrics`로 제공합니다. 전체 요청 흐름과 trust boundary는
[Architecture](docs/architecture.md), 정확한 wire와 정책의 근거는 [현재 결정](docs/decisions/README.md)을
따릅니다.

## 안전 경계

- Source는 Git에서 review한 두 파일 package와 최소 권한 reader로 제한합니다.
- SQL AST와 live PostgreSQL object를 allowlist로 검사합니다.
- Read-only transaction, timeout, concurrency, plan, row와 byte 상한을 강제합니다.
- Timeout, disconnect와 shutdown은 query cancel·rollback·cleanup으로 끝냅니다.
- Credential, token, SQL literal과 내부 database 오류는 public response와 일반 log에 노출하지 않습니다.
- Metadata와 SQL policy revision이 다르거나 source admission을 확인할 수 없으면 fail-closed합니다.

구체적인 정책과 owner는 [Architecture](docs/architecture.md)와
[Module index](docs/modules/README.md)에 있습니다.

## 현재 상태

Production Runtime은 review된 source package와 database profile이 있어야 시작합니다. 현재 inventory와
실제 DB·인증 연결, traffic 전환의 남은 순서는 [Active TODO](docs/development-todo.md)를 확인합니다.
Query Cave는 production inventory와 분리된 개발·온보딩·assurance 환경입니다.

## 로컬 검증

Query Cave는 synthetic DB와 임시 client certificate로 production 경로를 검증하고 종료 때 전용
container, volume과 credential 작업공간을 정리합니다.

```bash
./scripts/verify-query-cave.sh
./scripts/verify-container.sh
```

계속 실행하는 개발 세션과 port는 [Query Cave 안내](query-cave/README.md)를 따릅니다. 실제 source를 연결한
Compose 시작·종료는 [Operations](docs/operations.md)에 있습니다. 실제 credential과 production data를
Query Cave에 넣지 않습니다.

## 인증과 source 추가

Loopback은 인증 설정이 없을 때 local anonymous query를 허용합니다. Non-loopback bind는 단일 API token
또는 query/operator capability를 나눈 access-policy가 없으면 시작을 거부합니다. 정확한 HTTP 경계는
[Delivery module](docs/modules/delivery/README.md)을 따릅니다.

Source 추가는 [Source onboarding checklist](docs/source-extension-checklist.md)를 사용합니다. 새 물리 DB를
연결할 때만 [Database client certificate guide](docs/database-certificate-authentication.md)도 함께
따릅니다. Repository review와 protected DB apply는 별도 작업입니다.

Agent와 함께 source/database profile을 준비하거나 서버 상태를 조회할 때는 `$query-man-admin`, DB/role,
reviewed view와 certificate/HBA 작업을 계획할 때는 `$query-man-dba-onboarding`을 명시적으로 호출합니다.
두 repository 전용 skill의 예시와 credential·실행 승인 경계는 [Skill 사용 가이드](docs/skills.md)에
있습니다. 설치형 `qm` 관리 CLI는 제공하지 않으며 `query-man` server entrypoint만 유지합니다.

## 개발

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
```

변경 전 [활성 개발 지침](docs/development-guidelines.md)과
[Module index](docs/modules/README.md)에서 primary module의 관련 범위만 읽습니다. 현재 gate와 과거 기록을
찾는 방법은 [검증과 Git 기록](docs/verification/README.md)에 있습니다.
