# Query Cave

Query Cave는 production source inventory와 분리된 Query Man의 작은 개발·온보딩·assurance 환경입니다.
PostgreSQL 18, curated view, 최소 권한 reader와 client-certificate 인증을 실제 production 경로와 같은
형태로 조립합니다. Production DB, backup 또는 protected 실행 증거로 사용하지 않습니다.

## Hello World

Query Cave의 데이터 경계는 다음과 같습니다.

```text
gotham_schema.incidents_table
            │ reviewed view
            ▼
signal_schema.case_files_view ── query_cave_reader
```

- `gotham_schema.incidents_table`: 세 행의 private synthetic 원본
- `signal_schema.case_files_view`: Query Man에 공개되는 security-barrier view
- `query_cave_view_owner`: 원본을 읽고 view를 소유하는 `NOLOGIN` role
- `query_cave_reader`: 공개 schema와 view만 읽는 certificate-authenticated role

독립 DB 검사는 Query Man container 없이 Cave를 시작해 certificate reader로 다음 조회를 실행한 뒤 모든
container, volume과 임시 credential을 삭제합니다.

```bash
./scripts/verify-query-cave.sh
```

```sql
SELECT case_id, summary
FROM signal_schema.case_files_view
ORDER BY case_id;
```

Query Man image와 HTTP 경계까지 확인하려면 다음을 실행합니다.

```bash
./scripts/verify-container.sh
```

## 로컬 개발 세션

Query Cave를 직접 살펴보는 동안 계속 실행하려면 opt-in local project를 사용합니다. Production source
inventory에는 추가되지 않으며 기본 Compose와 함께 자동으로 시작되지 않습니다.

```bash
./scripts/query-cave.sh up
./scripts/query-cave.sh status
./scripts/query-cave.sh down
```

API는 기본 `http://127.0.0.1:33000`, PostgreSQL은 기본 `127.0.0.1:55432`에 열립니다. `up`은 API,
PostgreSQL과 certificate-authenticated reader를 유지하고 `status`는 container와 readiness를 확인합니다.
`down`은 전용 container, network, synthetic database volume과 임시 credential을 모두 삭제합니다. Local
상태와 secret은 `${XDG_STATE_HOME:-$HOME/.local/state}/query-man/query-cave`에만 두며 Git이나 image에
포함하지 않습니다.

## 온보딩 기준

`postgres/bootstrap.sql`은 disposable 원본 table, seed data와 role을 준비합니다. 실제 source onboarding에서
이 작업은 DB/data owner와 DBA가 소유합니다. `config/sources/query-cave/`의 `source.yaml`과 `views.sql`은
reviewed source package의 두 파일 계약을 그대로 따릅니다. Runtime은 `views.sql`을 실행하지 않습니다.

`pki/issue-certificates.sh`는 실행마다 별도 server CA와 client CA를 만들고 다음을 발급합니다.

- hostname 검증용 PostgreSQL server certificate
- database profile 단위 Query Man client certificate
- integration test용 admin certificate
- untrusted CA와 미매핑 DN negative probe certificate

Private key는 검증별 `mktemp` 작업공간이나 opt-in local session의 사용자 state directory에만 기록되고
Git과 image에는 들어가지 않습니다. Query Man client identity의 exact RFC 2253 DN만
`query_cave_reader`에 매핑합니다.

## CI

PR/push의 자동 필수 검사는 Docker를 시작하지 않고 source package 정적 계약과 unit/security test만
검사합니다. `.github/workflows/query-cave.yml`과 `.github/workflows/security.yml`의 무거운 검사는
GitHub Actions의 `workflow_dispatch`로만 실행합니다.
