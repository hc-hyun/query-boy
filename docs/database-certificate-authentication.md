# Database Client Certificate Guide

이 문서는 Query Man 한 배포가 물리 PostgreSQL database 하나에 client certificate 하나로 인증하고,
여러 source reader role을 사용하는 protected 절차를 설명합니다. Repository 변경 승인은 실제 인증서
발급, DB 설정 또는 배포 권한이 아닙니다. 실행 전 target, PKI/DBA 실행자, traffic-off window, stop
condition과 change-record 위치를 별도로 승인받습니다.

## 모델

```text
Query Man deployment
  └─ database profile: erp-prod
       ├─ client certificate: 1개
       ├─ source sales     → sales_reader
       └─ source inventory → inventory_reader
```

인증서는 Query Man deployment의 system identity를 증명합니다. PostgreSQL reader role과 exact grant는
각 source의 database 권한을 제한하고, Query Man의 source/SQL allowlist는 요청별 허용 범위를 제한합니다.
동일 process가 모든 reader를 사용하는 현재 경계에서는 source마다 private key를 복제하지 않습니다.
서로 다른 보안 등급, owner 또는 독립 폐기 조건이 필요하면 별도 database profile과 인증서를 사용합니다.

## Versioned 설정

물리 DB 연결과 인증 방식은 `config/database-profiles.yaml`에 한 번 정의합니다.

```yaml
version: 1

profiles:
  erp-prod:
    host: erp-db.internal
    port: 5432
    database: erp
    sslmode: verify-full
    authentication:
      type: client-certificate
```

같은 DB를 사용하는 source는 profile과 reader만 참조합니다.

```yaml
version: 6
source_id: sales
database_profile: erp-prod
reader_user: sales_reader
```

```yaml
version: 6
source_id: inventory
database_profile: erp-prod
reader_user: inventory_reader
```

Source package의 나머지 public 설명, provenance, view contract, schema와 budget은
[Source extension checklist](source-extension-checklist.md)를 따릅니다. 새 source가 기존 DB profile을
사용하면 database profile, Compose와 인증서를 수정하지 않습니다.

## Client credential layout

Runtime은 `QUERY_MAN_DATABASE_CREDENTIAL_DIRECTORY` 아래에서 database profile ID와 exact filename으로
세 파일을 찾습니다.

```text
/run/secrets/query-man/databases/
└── erp-prod/
    ├── ca.crt
    ├── client.crt
    └── client.key
```

- `ca.crt`: PostgreSQL server certificate를 검증할 신뢰 anchor와 필요한 intermediate
- `client.crt`: DB가 신뢰하는 client CA가 발급한 Query Man certificate와 필요한 intermediate
- `client.key`: matching private key

Private key와 certificate 원문은 repository, image, YAML, environment, command argument와 일반 log에
넣지 않습니다. 배포 secret store가 host의 제한된 directory 또는 read-only secret volume으로
제공해야 합니다. Unix private key는 Query Man UID만 읽는 `0600`, 또는 root 소유와 Query Man 전용
group에 한정한 `0640`을 사용합니다. Symlink 기반 secret volume을 사용한다면 최종 target과 권한도
같은 기준으로 검증합니다.

Compose는 host credential root를 한 번 mount합니다.

```dotenv
QUERY_MAN_DATABASE_CREDENTIAL_MOUNT=/secure/query-man/databases
```

```yaml
volumes:
  - ${QUERY_MAN_DATABASE_CREDENTIAL_MOUNT}:/run/secrets/query-man/databases:ro
```

새 물리 DB는 profile directory 하나를 추가하지만 기존 DB에 source를 추가할 때는 mount를 바꾸지
않습니다.

## PostgreSQL 설정

PostgreSQL server TLS를 활성화하고 server certificate/key와 client certificate를 발급한 CA를
설정합니다. Server certificate의 SAN은 database profile의 실제 접속 hostname과 일치해야 합니다.
Query Man은 `sslmode=verify-full`만 사용하며 CA 또는 hostname 검증을 downgrade하지 않습니다.

하나의 client identity를 여러 source reader에 매핑하려면 exact DN을 사용합니다. 실제 DN 문자열은
발급된 인증서에서 RFC 2253 형식으로 추출해 승인 기록에 고정합니다.

```bash
openssl x509 -in client.crt -noout -subject -nameopt RFC2253
```

예시 `pg_ident.conf`:

```text
# MAP              SYSTEM-USERNAME                       DATABASE-USERNAME
query_man_erp       "CN=query-man-erp-prod,O=Example"    sales_reader
query_man_erp       "CN=query-man-erp-prod,O=Example"    inventory_reader
```

예시 `pg_hba.conf`:

```text
# TYPE    DATABASE  USER                            ADDRESS       METHOD
hostssl   erp       sales_reader,inventory_reader   10.20.0.0/24 cert map=query_man_erp clientname=DN
```

HBA는 exact database, reader와 Query Man egress CIDR로 좁힙니다. 넓은 `all`, 평문 fallback 또는 뒤쪽
password rule을 certificate 실패의 fallback으로 사용하지 않습니다. 설정 reload 전
`pg_hba_file_rules` 오류와 rule order를 확인합니다.

각 reader는 `LOGIN`, 양의 connection limit을 가지며 데이터 접근 권한은 exact database `CONNECT`, source
schema `USAGE`, curated view `SELECT`로 제한합니다. `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `INHERIT`, `REPLICATION`, `BYPASSRLS`,
base relation, write, schema `CREATE`와 role switch 권한은 주지 않습니다. View는 별도 `NOLOGIN` owner가
소유하고 필요한 base relation만 읽습니다.

### Reader의 budget 설정 권한

Reader의 database-local 기본값은 `default_transaction_read_only=on`과 승인된 budget의 시간·자원 제한에
맞춥니다. Runtime은 metadata/query transaction을 시작할 때 budget을 다시 설정합니다.
`temp_file_limit`은 일반 reader가 기본적으로 변경할 수 없는 parameter이므로, DBA가 해당 reader에
이 parameter의 `SET` 권한도 부여해야 합니다. `ALTER ROLE ... SET temp_file_limit`로 기본값만 지정하면
runtime의 `set_config` 호출 권한은 생기지 않습니다.

승인된 reader가 `sales_reader`인 경우의 예시입니다. 실제 target과 role을 확인한 DBA 실행 범위에서만
적용합니다.

```sql
GRANT SET ON PARAMETER temp_file_limit TO sales_reader;
SELECT pg_catalog.has_parameter_privilege('sales_reader', 'temp_file_limit', 'SET')
  AS can_set_temp_file_limit;
```

Parameter grant는 cluster 범위이므로 공유 cluster의 `PUBLIC` 권한을 일괄 변경하거나 reader에
superuser 권한을 주지 않습니다. Query Cave bootstrap의 cluster-wide revoke를 production에 복사하지
않습니다. 기존 권한이 예상과 다르면 승인된 변경 범위를 다시 확인합니다.

이후 실제 reader 인증으로 read-only transaction을 열어 승인된 budget의 `temp_file_limit` 값을
transaction-local로 설정하고 `current_setting`으로 일치 여부를 확인한 뒤 rollback합니다. DBA session의
성공만으로 reader의 설정 권한을 증명하지 않습니다. 자세한 parameter 권한은
[PostgreSQL 문서](https://www.postgresql.org/docs/18/runtime-config-resource.html)를 따릅니다.

## Traffic-off acceptance

다음 순서로 검증합니다.

1. Approved image, database profile, source package와 certificate fingerprint/expiry를 고정합니다.
2. Server CA/hostname과 client CA/DN mapping을 확인합니다.
3. Query Man UID가 세 credential file을 읽을 수 있고 다른 container user가 private key를 읽지 못하는지
   확인합니다.
4. 올바른 certificate로 각 reader login, parameter SET 권한과 실제 transaction budget 설정, metadata
   admission과 bounded query를 확인합니다.
5. 인증서 없음·만료, 잘못된 CA/key, 잘못된 hostname, 미매핑 DN, 미허용 reader/database와 source 밖
   view 접근이 모두 실패하는지 확인합니다.
6. `/ready`, `/admin/health`, credential redaction, timeout·rollback과 pool reuse를 확인합니다.

하나라도 실패하면 traffic을 연결하지 않습니다. Runtime은 certificate file을 직접 발급하거나 DB
설정을 변경하지 않으며 연결 실패를 password나 약한 TLS로 fallback하지 않습니다.
전체 application 활성화는 [Operations의 traffic-off acceptance](operations.md#2-traffic-off-acceptance)도
모두 통과해야 합니다. DB 준비 완료와 application 활성화 완료는 별도로 기록합니다.

## Rotation과 rollback

인증서 만료 전에 PKI가 같은 database identity용 새 certificate/key를 발급합니다. 새 chain과 DN mapping을
DB에서 먼저 신뢰하게 하고 traffic 밖의 새 Query Man instance로 negative/positive probe를 통과한 뒤
전환합니다. Pool의 기존 TLS session이 남을 수 있으므로 파일만 교체하지 말고 process를 재시작합니다.

전환이 확인된 뒤에만 이전 certificate trust를 제거하거나 CRL에 반영합니다. 실패 시 신규 admission을
막고 새 instance를 종료한 뒤 직전 credential mount, image와 DB trust 설정으로 rollback합니다. Private
key 유출 의심에서는 일반 rollback보다 즉시 폐기와 incident 절차를 우선합니다.

Disposable test database의 `password` profile 지원은 TLS certificate 기능을 대신하는 production mode가
아닙니다. 현재 Query Cave integration은 production 경로와 같은 client certificate를 사용합니다.
