# Repository Skill 사용 가이드

Query Man의 관리 작업은 설치형 관리 CLI 대신 repository에 함께 versioning하는 두 Codex skill로
수행합니다. Codex는 repository의 `.agents/skills/`를 발견하며, 두 skill 모두 자동 호출을 끄고
`$skill-name`으로 명시했을 때만 사용하도록 설정했습니다.

Skill 호출은 shell 명령이 아니라 Codex에게 보내는 요청입니다. Repository root 또는 그 하위 directory에서
Codex 작업을 시작하고 요청 첫 부분에 정확한 이름을 적습니다.

## 어떤 skill을 쓰나

| Skill | 사용 범위 | 기본 side effect |
|---|---|---|
| `$query-man-admin` | Source/database profile 작성, versioned package 검증, 실행 중인 서버 상태 조회 | Repository 파일 변경 또는 read-only HTTP |
| `$query-man-dba-onboarding` | DB/role, reviewed view, grant, certificate DN, HBA/ident 작업 계획과 승인된 실행 | 기본은 secret-free 계획만 작성 |

업무 데이터를 질문하고 SQL로 조회하는 용도에는 이 두 skill을 사용하지 않습니다. Query Man source를 통한
업무 조회는 별도의 text-to-SQL skill 경계입니다.

## Source와 database profile 준비

기존 physical DB에 source를 추가하는 예:

```text
$query-man-admin sales source를 추가해줘.
기존 database profile은 erp-prod이고 reader는 sales_reader야.
실제 DB와 credential은 건드리지 마.
```

첫 physical DB와 source를 함께 준비하는 예:

```text
$query-man-admin erp-prod database profile과 sales source package를 준비해줘.
host/database/reader/schema/view 정보는 아래와 같아. Repository 변경과 검증까지만 해줘.
```

이 단계는 `config/database-profiles.yaml`과 source package의 `source.yaml`, `views.sql`만 다룹니다. Password,
DSN, token, certificate body/private key와 secret-store 식별자를 대화나 Git에 넣지 않습니다. 실제 DB에는
접속하지 않습니다.

설치형 `qm` 명령은 없습니다. Skill이 사용하는 credential-free validator는 필요할 때 다음처럼 직접
실행할 수 있습니다.

```bash
uv run python .agents/skills/query-man-admin/scripts/validate_source_packages.py
```

이 helper는 versioned 설정만 읽고 `.env`, process environment, certificate file과 database를 읽지 않습니다.
완료 전에는 source checklist의 focused test와 repository 전체 gate도 실행합니다.

## 서버 상태 조회

```text
$query-man-admin http://127.0.0.1:3000 Query Man의 readiness와 상세 상태를 확인해줘.
```

`/ready`는 token이 필요 없습니다. `/admin/health`, `/admin/metrics`, `/sources`, `/meta`에는 승인된 operator
token을 사용합니다. 회사 환경에서는 token 값을 대화나 shell 인자에 넣지 않고 승인된 read-only token
file을 `QUERY_MAN_OPERATOR_TOKEN_FILE`로 제공합니다. Local disposable 환경에서만
`QUERY_MAN_OPERATOR_TOKEN`을 사용할 수 있습니다. 자세한 규칙은 skill의
`references/credential-safety.md`를 따릅니다.

Skill 내부 HTTP helper의 직접 사용 예:

```bash
export QUERY_MAN_SERVER_URL=https://query-man.example
export QUERY_MAN_OPERATOR_TOKEN_FILE=/approved/non-repository/path/operator-token
python3 .agents/skills/query-man-admin/scripts/query_man_request.py status
```

현재 Query Man server 자체의 query/operator token 전달은 여전히 runtime access policy의 environment
contract입니다. 위 token-file 방식은 관리 client helper의 credential 노출을 줄이는 기능이며 server-side
secret delivery를 변경하지 않습니다.

## DBA 작업 계획과 실행

먼저 실제 접속 없는 계획을 만듭니다.

```text
$query-man-dba-onboarding erp-prod의 sales source DBA 실행 계획을 만들어줘.
아직 DB, PKI, Kubernetes에는 접속하지 마.
```

실행 요청은 다음 범위를 모두 명시해야 합니다.

- Approved commit/image와 exact cluster/database/source/profile
- DB/data owner의 output·no-PII 승인
- DBA, PKI와 deployment 실행자
- Credential 값이 아닌 승인된 access mechanism 이름
- Database 생성, role, DDL, HBA/ident, certificate, mount 중 승인된 정확한 범위
- Traffic-off window, stop condition, backup/rollback owner
- Append-only change-record 위치와 책임자

DBA password는 Query Man이나 agent에 전달하지 않습니다. 사용자가 회사의 Vault/SSO/bastion/short-lived
certificate/credential wrapper로 인증된 session을 준비하고, agent에는 비밀 없는 service alias 또는
wrapper invocation만 제공합니다. 불가피한 interactive credential은 agent-visible input/output 밖에서
사용자가 직접 입력합니다.

Skill 생성이나 repository merge는 실제 DB 변경 승인이 아닙니다. 정보가 빠졌거나 target, privilege,
certificate, HBA, output이 예상과 다르면 skill은 실행을 시작하거나 계속하지 않고 중단합니다.

## 권장 순서

1. `$query-man-admin`으로 versioned source/database profile을 준비하고 repository gate를 통과합니다.
2. Owner review와 approved commit을 고정합니다.
3. `$query-man-dba-onboarding`으로 secret-free 실행 packet을 만들고 protected 실행 범위를 승인합니다.
4. Traffic을 끈 상태에서 승인된 owner가 DB/PKI/deployment 작업과 positive/negative probe를 수행합니다.
5. 별도 승인으로 Query Man을 시작한 뒤 `$query-man-admin`으로 readiness와 operator 상태를 조회합니다.

Repository PASS를 protected DB 적용이나 production activation 완료로 기록하지 않습니다. 실제 결과는
승인된 환경의 append-only change record에만 남깁니다.
