# Query Man 용어 사전

정확한 API와 정책 숫자는 owner 문서와 accepted ADR이 기준입니다.

## 데이터와 Source

| 용어 | 뜻 |
|---|---|
| Source | Query Man이 조회 대상으로 다루는 PostgreSQL database 단위입니다. Client는 접속 주소 대신 Source ID를 사용합니다. |
| Source package | Git에서 review하는 `config/sources/<source-id>/` 폴더입니다. `source.yaml`과 `views.sql`만 둡니다. |
| Source manifest | `source.yaml`입니다. 접속 환경 변수, 허용 범위, budget과 provenance를 정의하며 password 값은 넣지 않습니다. |
| Desired view SQL | `views.sql`입니다. DB owner가 적용할 curated view와 exact grant를 정의하지만 Runtime은 실행하지 않습니다. |
| Curated view | 원본 table에서 공개해도 되는 column과 의미를 DB owner가 정리한 읽기 전용 view입니다. |
| View contract version | Manifest와 공개 view comment marker가 공유하는 양의 정수입니다. 공개 구조 변경 때 올립니다. |
| Metadata | SQL 작성 전에 필요한 relation·column·type·description입니다. 업무 row 자체가 아닙니다. |
| Revision | Context와 query가 같은 metadata·SQL policy를 사용했는지 확인하는 내용 지문입니다. |

## 접속과 실행

| 용어 | 뜻 |
|---|---|
| Reader | Source를 읽는 최소 권한 DB 계정입니다. 원본 schema 쓰기나 관리자 권한이 없습니다. |
| DB owner / DBA | View, role, grant와 backup을 관리하는 주체입니다. Query 요청 처리에는 사용하지 않습니다. |
| DSN | DB 접속 주소와 설정의 묶음입니다. Client가 선택하거나 볼 수 없습니다. |
| Allowlist | 명시적으로 허용한 schema, relation, function, operator 등만 통과시키는 정책입니다. |
| Budget | Query 시간, 동시 실행 수, plan, memory, 결과 행·byte 같은 제한 묶음입니다. |
| Read-only transaction | PostgreSQL도 쓰기를 막도록 설정한 transaction입니다. AST 검사와 별도 방어선입니다. |
| OID | PostgreSQL data type 식별자입니다. 현재 성공 결과는 검토한 일곱 OID로 제한합니다. |
| RLS | Row-Level Security입니다. 현재 first launch에서는 모든 RLS source를 거부합니다. |
| Cancel / rollback | 실행 중 query를 중단하고 transaction 상태를 되돌립니다. Disconnect와 shutdown에도 수행합니다. |
| Fail-closed | 확인할 수 없을 때 안전하다고 추측하지 않고 요청을 거부하는 방식입니다. |
| Fingerprint | SQL literal을 노출하지 않으면서 같은 query 형태를 식별하는 지문입니다. |

## 개발과 운영

| 용어 | 뜻 |
|---|---|
| Module | 같은 repository와 process 안에서 코드 이해·변경 책임을 나눈 단위입니다. 독립 service가 아닙니다. |
| Module interface | 허용 의존 안에서 provider가 consumer에 보장하는 중요한 entrypoint와 동작입니다. |
| External API | HTTP request, response, status, error와 인증 형식입니다. |
| Lifecycle | Startup, readiness, drain, shutdown과 실패 cleanup의 순서와 결과입니다. |
| Authority | 특정 사실을 결정하는 최종 기준입니다. Source는 reviewed package가 authority입니다. |
| Protected environment | 실제 secret, DB 권한, route와 변경 기록 책임이 있는 제한된 환경입니다. |
| Repository acceptance | 코드와 local/CI 검증 상태입니다. Protected 환경에 적용됐다는 뜻은 아닙니다. |
| Cutover / rollback | 새 version으로 traffic을 전환하거나 직전 안전한 version·설정으로 돌아가는 작업입니다. |

`DBENV-01` 같은 작업 ID는 [Active TODO](development-todo.md), 현재 결정은
[Decision index](decisions/README.md)에서 확인합니다.
