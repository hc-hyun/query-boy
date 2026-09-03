# Active Development TODO

Repository implementation과 local acceptance는 protected environment 전환 권한이 아닙니다. 현재 열린
작업은 실제 DB, 인증과 traffic 연결 세 가지뿐입니다.

## 현재 작업

- [ ] `DBENV-01`: 승인된 source DB와 최소 권한 reader를 traffic 밖에서 검증
- [ ] `AUTHENV-01`: protected 환경의 opaque-token authority와 secret 전달 경로를 확정
- [ ] `LAUNCH-02`: 앞선 evidence를 확인하고 단일 replica를 배포·전환

각 작업의 실행자는 access, exact target, stop condition과 append-only change-record 책임을 별도로
승인받아야 합니다.

## DBENV-01

Approved Git revision의 `config/sources/` package 전체와 `config/budget-profiles.yaml`을 고정합니다.
DB owner와 DBA가 다음을 traffic 밖에서 확인합니다.

- PostgreSQL 18, UTF-8와 manifest의 TLS mode
- View marker의 source ID와 contract version
- RLS 0개, 허용 schema의 view-only catalog와 예상 column
- Reader의 exact database/user, login과 최소 privilege
- Base relation 접근·write·role switch·함수/operator 우회 거부
- Metadata revision, bounded query, timeout/cancel/rollback과 credential 비공개

불일치하면 DDL이나 manifest를 현장에서 고치지 말고 중단합니다. 원인은 DB apply 또는 repository
change-set으로 분리해 review한 뒤 처음부터 다시 실행합니다.

## AUTHENV-01

Non-loopback 배포에서는 다음 중 하나를 exact authority로 정합니다.

- 단일 query consumer를 위한 `QUERY_MAN_API_TOKEN`
- 여러 query caller와 operator capability를 위한 `QUERY_MAN_ACCESS_POLICY_FILE`

Secret은 repository, image, command argument와 일반 log에 넣지 않습니다. 정상 query token, 잘못된
token, query/operator 권한 분리, source authorization과 redaction을 실제 배포 방식으로 확인합니다.
인증 mapper나 application code를 현장에서 새로 구현하지 않습니다.

## LAUNCH-02

`DBENV-01`과 `AUTHENV-01`의 exact inventory와 evidence가 완료된 뒤에만 진행합니다.

1. Approved commit과 immutable image revision을 고정합니다.
2. Source package, budget와 secret reference를 재확인합니다.
3. 단일 replica를 traffic 밖에서 시작하고 `/ready`, `/admin/health`, 대표 metadata/query와 negative
   safety probe를 확인합니다.
4. Traffic을 제한적으로 연결하고 error, timeout, queue, row/byte와 pool 지표를 관찰합니다.
5. 승인된 관찰 기간을 통과하면 전환을 완료합니다.

Rollback은 신규 admission 차단, 활성 query drain/cancel, 직전 image·config·route 복원 순서로 수행합니다.
Database 변경이 있었다면 DBA가 사전 승인한 역순 DDL 또는 기존 view 복구 절차를 사용합니다.

## 즉시 중단할 조건

- Target, approved commit, 실행자 또는 change-record 위치가 불명확함
- Source package와 live DB marker/catalog/revision이 다름
- RLS, 과도한 reader privilege, TLS 또는 server/encoding 불일치
- Secret, SQL literal, 내부 database error가 응답이나 log에 노출됨
- Timeout, disconnect, cancel, rollback 또는 shutdown cleanup이 확인되지 않음
- 두 인증 authority가 동시에 설정됐거나 query caller가 operator capability를 얻음
- Backup·rollback 가능성이나 traffic-off 경계가 확인되지 않음

## 현재 일정에 없는 일

RLS serving, cross-source federation, runtime source reload, result type 확대, DB-backed source authority,
multi-replica shared quota와 distributed tracing은 승인된 작업이 아닙니다. 실제 요구와 별도 영향·rollback
계획이 생기기 전까지 구현하지 않습니다.

완료 사실은 날짜별 문서가 아니라 exact commit/CI provenance와 protected 환경의 승인된
append-only/immutable record에 남깁니다. Git history를 rewrite하지 않습니다.
