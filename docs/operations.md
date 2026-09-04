# Operations Guide

이 문서는 현재 단일-replica, static non-RLS 배포의 전체 실행 순서와 rollback 경계를 소유합니다.
Source/View apply의 상세 절차는 [Source onboarding](source-extension-checklist.md), DB 인증서는
[Database certificate guide](database-certificate-authentication.md), query limit 조사는
[Query 제한과 자원](query-cost-control.md)을 따릅니다. Repository 변경 승인과 protected 환경 실행 승인은
별개입니다.

## 시작 전 고정할 것

- Approved commit과 immutable image revision
- Reviewed source package 전체, database profile과 budget
- 실제 PostgreSQL target, DBA·service 실행자와 change-record 위치
- DB별 client certificate fingerprint/expiry/mount와 API secret 전달 방식
- Traffic-off 검증 시간, stop condition, 직전 image/config/route
- DB view 변경이 있으면 backup과 DBA rollback SQL

RLS source나 unreviewed package를 임시로 허용하지 않습니다. Production DB profile은
`client-certificate`와 `verify-full`만 사용하며 인증 실패를 password나 약한 TLS로 downgrade하지
않습니다. 현재 선행 작업과 의존성은 [Active TODO](development-todo.md)를 확인합니다.

## 1. Repository와 DB 준비

Source package와 database profile은 protected action 전에 repository에서 review하고 다음 read-only
명령으로 검사합니다.

```bash
uv run python .agents/skills/query-man-admin/scripts/validate_source_packages.py
```

이 repository-local skill helper는 versioned artifact만 검증하며 environment나 certificate file을 읽거나
DB에 연결하고 DDL을 실행하지 않습니다. 첫 source와 새 DB를 준비하는 파일·review·apply 절차는
[Source onboarding](source-extension-checklist.md), certificate/HBA/DN mapping은
[Database certificate guide](database-certificate-authentication.md)의 owner와 중단 조건을 따릅니다.

DB apply 중 target, object dependency, privilege 또는 curated output이 approved artifact와 다르거나
transaction이 실패하면 rollback하고 중단합니다. 현장에서 manifest, DDL 또는 인증 정책을 고쳐 계속하지
않습니다.

## 2. Traffic-off acceptance

단일 replica를 traffic 밖에서 시작하고 다음 경계를 확인합니다.

1. 모든 database profile과 reviewed source package가 strict load되고 startup inventory 전체가
   admission됩니다.
2. PostgreSQL 18/UTF-8, CA/hostname/client certificate/DN mapping, source/version marker, RLS 0개와 exact
   reader identity가 확인됩니다.
3. `/health`는 process 생존, `/ready`는 startup source와 query pool 준비 상태를 반영합니다.
4. `/sources`와 `/meta`는 인증·source authorization 뒤 secret 없는 projection만 반환합니다.
5. `/query`는 두 revision, AST/object allowlist, read-only transaction과 resource limit을 적용합니다.
6. 잘못된 인증서·권한, stale revision, write SQL, forbidden object와 unsupported OID가 fail-closed합니다.
7. Timeout, disconnect와 shutdown에서 cancel·rollback·connection cleanup을 확인합니다.

응답이나 log에 token, private key/certificate path, password, DSN, SQL literal 또는 PostgreSQL 내부
message가 보이거나 위 경계 하나라도 확인되지 않으면 traffic을 연결하지 않습니다.

## 3. Cutover와 rollback

Acceptance evidence와 실행 승인을 확인한 뒤에만 traffic을 제한적으로 연결합니다. Readiness, public error,
queue/elapsed, row/byte truncation, plan rejection과 pool/query health를 승인된 관찰 기간 동안 확인합니다.

다음 상황에서는 신규 admission을 막고 rollback합니다.

- Readiness 또는 source health가 안정적으로 유지되지 않음
- Revision mismatch가 예상 배포 창 밖에서 반복됨
- Timeout·overload·unavailable이 승인된 기준을 넘음
- Authorization, redaction, cancel·rollback 또는 result limit 위반
- DB schema, role/grant, TLS나 semantic setting drift

Rollback 순서는 traffic 차단, 활성 query drain 또는 cancel, process 종료, 직전 image/config/route 복원,
readiness와 negative probe 재검증입니다. DB 변경 rollback은 application rollback과 분리해 DBA가 승인된
역순 DDL 또는 직전 view definition을 복구합니다. 부분 성공을 완료로 기록하지 않습니다.

## 상태와 로그

- `GET /health`: process liveness
- `GET /ready`: startup admission과 serving readiness
- `GET /admin/health`: operator용 component 상태
- `GET /admin/metrics`: bounded process-local counters

일반 log는 request/query ID, source, pseudonymous caller, public outcome, duration과 bounded resource 수치만
기록합니다. Authorization header, raw request body, question, SQL, literal, DSN과 database error는 기록하지
않습니다. Metrics는 process-local 관측값이며 billing, durable audit 또는 multi-replica 합계가 아닙니다.

## Local Compose

실제 source를 연결한 local API는 명시적으로 시작하고 종료합니다.

```bash
docker compose --env-file .env up --build -d
curl -fsS http://127.0.0.1:3000/ready
docker compose --env-file .env down
```

`.env`의 `QUERY_MAN_DATABASE_CREDENTIAL_MOUNT`는 DB profile ID별 `ca.crt`, `client.crt`, `client.key`를
포함하는 host directory를 가리켜야 합니다. Query Cave의 disposable 검증과 계속 실행하는 개발 세션은
[Query Cave 안내](../query-cave/README.md)를 따릅니다. Query Cave는 production inventory, backup 또는
protected evidence가 아닙니다.

## Graceful shutdown

SIGTERM 뒤 server는 신규 admission을 중단하고 하나의 monotonic shutdown deadline 안에서 활성 query를
drain합니다. 남은 query는 cancel하고 transaction rollback 뒤 query와 catalog pool을 닫습니다.
Orchestrator의 stop grace는 application grace와 cleanup overhead보다 길어야 합니다. 강제 kill이 발생하면
미완료 cleanup으로 기록하고 다음 start 전에 DB session과 lock을 확인합니다.
