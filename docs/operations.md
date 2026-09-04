# Operations Guide

이 문서는 현재 단일-replica, static non-RLS 배포 절차만 다룹니다. Repository 변경 승인과 protected
환경 실행 승인은 별개입니다.

## 시작 전 고정할 것

- Approved commit과 immutable image revision
- `config/sources/<source-id>/{source.yaml,views.sql}` 전체, `config/database-profiles.yaml`과 budget
- 실제 PostgreSQL target, DBA·service 실행자와 change-record 위치
- DB별 client certificate fingerprint/expiry/mount와 API token 전달 방식
- Traffic-off 검증 시간, stop condition, 직전 image/config/route
- DB view 변경이 있으면 backup과 DBA rollback SQL

RLS source나 unreviewed package를 임시로 허용하지 않습니다. Production DB profile은
`client-certificate`와 `verify-full`만 사용하며 인증 실패를 password나 약한 TLS로 downgrade하지
않습니다.

## Source 검증과 DB apply

Repository 안의 package는 local read-only 명령으로 검사합니다.

```bash
uv run qm source validate
```

이 명령은 database/source manifest와 desired SQL을 검증할 뿐 certificate file을 읽거나 DB에 연결하고
DDL을 실행하지 않습니다.

DB/data owner는 `views.sql`의 explicit output, no-PII 경계, base relation과 comment를 review합니다. DBA는
별도 승인 뒤 traffic 밖에서 exact SQL을 적용하고 reader grant를 확인합니다. Runtime이 대신 적용하거나
repository test 결과를 production apply evidence로 사용하지 않습니다.

적용 중 다음 중 하나라도 발생하면 즉시 중단합니다.

- Target database·role·approved SQL이 불명확함
- Existing object ownership이나 dependency가 예상과 다름
- Lock timeout, statement failure 또는 partial transaction
- View output에 개인정보·민감정보 또는 검토하지 않은 column이 포함됨
- Reader가 base relation, write, role switch나 허용 밖 schema에 접근할 수 있음

Transaction 실패는 rollback하고 이전 view가 그대로인지 확인합니다. Commit 뒤 문제가 발견되면 신규
application admission을 차단하고 DBA가 승인된 역순 DDL 또는 직전 view definition을 복구합니다.

## Traffic-off acceptance

단일 replica를 traffic 밖에서 시작하고 다음을 순서대로 확인합니다.

1. 모든 database profile과 reviewed package가 strict load되고 unknown field/reference는 startup을
   실패시킵니다.
2. DB별 CA/hostname/client certificate/DN mapping과 live catalog의 PostgreSQL 18/UTF-8,
   source/version marker, RLS 0개와 reader identity를 admission합니다.
3. `/health`는 process 생존, `/ready`는 startup source와 query pool 준비 상태를 반영합니다.
4. `/sources`와 `/meta`는 인증·source authorization 뒤 secret 없는 public projection만 반환합니다.
5. `/query`는 두 revision, AST/allowlist, read-only transaction과 모든 resource limit을 적용합니다.
6. 인증서 없음·잘못된 CA/key/hostname/DN, stale revision, write SQL, forbidden
   relation/function/operator와 unsupported OID를 fail-closed합니다.
7. Timeout, disconnect와 shutdown에서 cancel·rollback·connection reuse를 확인합니다.

실패 응답이나 log에서 token, private key/certificate path, password, DSN, SQL literal과 PostgreSQL 내부
message가 보이면 전환하지 않습니다.

## Cutover와 rollback

Acceptance evidence와 실행 승인을 확인한 뒤에만 traffic을 제한적으로 연결합니다. 관찰할 항목은
readiness, error code, queue/elapsed, row/byte truncation, plan rejection과 pool/query health입니다.

다음이면 신규 admission을 막고 rollback합니다.

- Readiness 또는 source health가 안정적으로 유지되지 않음
- Revision mismatch가 예상 배포 창 밖에서 반복됨
- Timeout·overload·unavailable이 승인된 기준을 넘음
- Authorization, redaction, cancel·rollback 또는 result limit 위반
- DB schema, role/grant, TLS나 semantic setting drift

Rollback 순서는 traffic 차단, 활성 query drain 또는 cancel, process 종료, 직전 image/config/route 복원,
readiness와 negative probe 재검증입니다. DB 변경 rollback은 application rollback과 분리해 DBA가 수행합니다.
부분 성공을 완료로 기록하지 않습니다.

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
포함하는 host directory를 가리켜야 합니다. 발급, PostgreSQL mapping과 rotation은
[Database certificate guide](database-certificate-authentication.md)를 따릅니다.

Query Cave 검증은 lifecycle을 소유하는 script로 실행합니다.

```bash
./scripts/verify-query-cave.sh
./scripts/verify-container.sh
```

Script는 격리된 Query Cave project만 사용하고 성공·실패 때 모두 container, volume과 임시 인증서를
삭제합니다. Query Cave는 개발·온보딩·수동 assurance용이며 production inventory, backup이나 protected
evidence가 아닙니다. 자동 삭제 대상에는 synthetic test data만 두어야 합니다.

개발자가 Query Cave를 계속 살펴볼 때만 별도 local project를 명시적으로 시작합니다.

```bash
./scripts/query-cave.sh up
./scripts/query-cave.sh status
./scripts/query-cave.sh down
```

이 project는 기본 Compose나 production startup inventory에 포함되지 않습니다. `down`은 전용 synthetic
database volume과 임시 credential까지 삭제하며 실제 DB나 production credential을 참조하지 않습니다.

PR/push는 Docker 없는 경량 gate만 자동 실행합니다. Query Cave DB/container와 image security scan은 각각
GitHub Actions의 `query-cave`, `security` workflow를 수동 실행합니다.

## Graceful shutdown

SIGTERM 뒤 server는 신규 admission을 중단하고 하나의 monotonic shutdown deadline 안에서 활성 query를
drain합니다. 남은 query는 cancel하고 transaction rollback 뒤 query와 catalog pool을 닫습니다.
Orchestrator의 stop grace는 application grace와 cleanup overhead보다 길어야 합니다. 강제 kill이 발생하면
미완료 cleanup으로 기록하고 다음 start 전에 DB session과 lock을 확인합니다.

현재 protected 작업과 완료 조건은 [Active TODO](development-todo.md), query limit과 조사 순서는
[Query 제한](query-cost-control.md), source 변경 checklist는
[Source extension](source-extension-checklist.md)에 있습니다.
