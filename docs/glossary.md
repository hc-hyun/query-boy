# Query Man 용어 사전

낯선 단어가 나오면 이 문서에서 먼저 찾으세요. 정확한 정책 숫자나 API 형식은 각 기술 문서와
accepted ADR이 기준이고, 이 문서는 개념을 쉽게 이해하기 위한 설명입니다.

## 제품과 데이터

| 용어 | 쉽게 말하면 |
|---|---|
| Query Man | AI나 사용자가 만든 SQL을 검사하고, 허용된 PostgreSQL 데이터만 제한 안에서 조회하는 관문입니다. AI 모델 자체는 포함하지 않습니다. |
| Source | Query Man이 조회 대상으로 다루는 데이터 한 묶음입니다. 보통 PostgreSQL database 하나와 연결되지만, 사용자는 접속 주소 대신 Source ID만 봅니다. |
| Source ID | Source를 가리키는 공개 이름입니다. 예: `market-voc`. 비밀번호나 DB 주소가 아닙니다. |
| Source manifest | Git에서 review하는 `config/sources/*.yaml`입니다. Source의 위치, reader 이름, 허용 schema, 제한과 업무 설명을 적고 실제 비밀번호는 넣지 않습니다. |
| Curated view | 원본 table을 AI 조회에 적합하도록 DB owner가 정리해 공개한 읽기 전용 view입니다. Query Man은 현재 `ai` schema의 승인된 view를 사용합니다. |
| Grain | 결과 한 행이 무엇 하나를 뜻하는지 나타냅니다. 예: “VOC 한 건”, “판매 기기 한 대”. |
| Fanout | 서로 다른 grain을 잘못 join해 같은 사실이 여러 번 복제되는 문제입니다. 합계나 건수가 부풀 수 있습니다. |
| Semantic overlay | DB 이름만으로 알 수 없는 업무 의미를 보충하는 설정입니다. 별칭, grain, 안전한 join과 상태 판정 같은 내용만 둡니다. |
| Seed / fixture | 개발과 검증을 위해 반복해서 같은 상태로 만들 수 있는 예제 데이터·DB입니다. 실제 운영 데이터가 아닙니다. |
| Golden question | 제품이 반드시 제대로 답해야 하는 대표 질문입니다. 현재 두 source에 아홉 개가 있습니다. |

## 접속과 실행

| 용어 | 쉽게 말하면 |
|---|---|
| Reader | Query Man이 source를 읽을 때 쓰는 최소 권한 DB 계정입니다. 원본 schema 쓰기나 관리자 권한이 없습니다. |
| DB owner / DBA | View·role·grant·backup처럼 데이터베이스 자체를 관리하는 사람 또는 계정입니다. Reader보다 훨씬 강한 권한이므로 Query Man 요청 처리에 사용하지 않습니다. |
| DSN | DB 접속 주소와 설정을 묶어 표현한 값입니다. Client나 AI는 DSN을 선택하지 못합니다. |
| Connection pool | 매 query마다 새 DB 연결을 만들지 않도록 제한된 연결을 재사용하는 보관함입니다. |
| Read-only transaction | DB 내용을 바꿀 수 없도록 시작한 transaction입니다. SQL 검사와 별개로 PostgreSQL도 쓰기를 막습니다. |
| Allowlist | 허용한다고 명시한 대상만 통과시키는 목록입니다. Schema, relation, function, operator 등에 사용합니다. |
| Budget profile | Query 한 건의 시간, 동시 실행 수, 메모리·임시 파일, 결과 행·byte 같은 제한 묶음입니다. 돈 단위 예산이 아닙니다. |
| OID | PostgreSQL이 data type을 식별하는 숫자입니다. 현재 첫 오픈은 결과 type 일곱 종류의 OID만 허용합니다. |
| RLS | Row-Level Security. 같은 table에서도 사용자·tenant에 따라 보이는 행을 DB가 제한하는 기능입니다. Query Man의 현재 첫 오픈에서는 RLS source를 전부 거부합니다. |
| Cancel / rollback | 실행 중 query를 중단하고 transaction에서 생긴 작업 상태를 되돌리는 절차입니다. Client 연결이 끊겨도 수행합니다. |
| Fail-closed | 확인할 수 없을 때 안전하다고 추측하지 않고 요청을 거부하는 방식입니다. |
| Resource Server | OAuth access token을 받아 자기 API의 audience와 권한을 검증하는 서비스입니다. Query Man은 token을 발급하거나 refresh하지 않습니다. |
| JWT access token | AuthBridge가 서명해 발급한 OAuth bearer token입니다. Payload decode만으로 신뢰하지 않고 서명, issuer, audience, 만료와 scope를 함께 검사합니다. |
| JWKS | JWT 서명을 검증할 공개 key 모음입니다. Discovery의 `jwks_uri`에서 읽어 cache하며 새로운 `kid`가 나타날 때 제한적으로 갱신합니다. |

## Metadata와 검증

| 용어 | 쉽게 말하면 |
|---|---|
| Metadata | Table·column·type·설명·grain·join처럼 SQL을 만들기 전에 알아야 할 데이터 설명입니다. 일반 업무 row 자체가 아닙니다. |
| Physical catalog | PostgreSQL의 `pg_catalog`에서 자동으로 읽은 relation, column, key, index와 type 정보입니다. |
| Metadata revision | 특정 source의 metadata와 그 의미·제한이 정확히 어느 버전인지 나타내는 내용 지문입니다. 보통 업무 row가 추가·수정되는 것만으로는 바뀌지 않습니다. |
| SQL policy revision | 전체 애플리케이션이 허용하는 SQL 문법, 함수, operator, 결과 type과 canonical 정책 버전의 내용 지문입니다. Source별 metadata revision과 별개입니다. |
| Revision mismatch | Context를 받은 뒤 metadata나 SQL 정책이 바뀌어, 낡은 정보로 만든 SQL을 실행할 수 없는 상태입니다. Context를 다시 받아야 합니다. |
| Verified query | 대표 질문·SQL·예상 결과를 묶은 회귀 시험입니다. 사용자 query 허용 목록이 아닙니다. |
| Fingerprint | SQL literal을 노출하지 않으면서 같은 형태의 query를 식별하는 지문입니다. |
| Pseudonymous subject | 환경별 secret key로 caller·tenant를 HMAC해 일반 audit에서 직접 식별자를 대신하는 값입니다. 같은 key에서는 연결 가능하므로 익명 사용자를 뜻하지 않습니다. |
| Diagnostic consent / capture | 만료 가능한 server-side 동의 receipt가 있을 때만 질문 원문과 literal-free SQL을 일반 log와 분리된 최대 7일 암호화 저장소에 남기는 진단 기능입니다. |
| Canonical encoding | 같은 결과가 언제나 같은 byte 표현과 hash를 만들도록 값 표현을 고정하는 규칙입니다. |
| Invariant | 구현이 바뀌어도 반드시 참이어야 하는 조건입니다. 예: 실패한 query가 rollback되고 secret이 응답에 나오지 않음. |
| L0 / L1 / L2 | Source metadata 품질 단계입니다. L0는 기본 catalog, L1은 업무 설명·grain 보강, L2는 현재 revision의 verified query까지 통과한 상태입니다. |

## 실행 모드와 운영

| 용어 | 쉽게 말하면 |
|---|---|
| Git-reviewed YAML authority | `config/sources/*.yaml`, `config/verified-queries.yaml`, `config/budget-profiles.yaml`이 source·verified query·제한을 결정하는 유일한 기준인 방식입니다. 변경은 review·test·배포로 반영합니다. |
| Static launch | Git에서 검토한 두 source와 단일 replica를 배포물에 고정한 현재 첫 오픈 범위입니다. 실행 중 새 source를 추가하지 않습니다. |
| Replica | 같은 Query Man 애플리케이션을 실행하는 process 한 개입니다. 현재 first launch 계획은 단일 replica입니다. |
| Freshness / stale | Metadata가 얼마나 최근 것인지 나타냅니다. `stale`은 마지막 정상 snapshot은 있지만 신선도 기준을 넘었다는 뜻입니다. |
| Projection | 내부 정보 중 외부에 보여도 되는 필드만 골라 만든 응답 모양입니다. Secret이나 내부 row 전체를 그대로 내보내지 않습니다. |
| Cutover | 준비한 새 version으로 실제 요청 경로를 전환하는 작업입니다. |
| Rollback | 문제가 생겼을 때 요청을 끊고 직전 안전한 version·설정·경로로 돌아가는 작업입니다. |
| RPO / RTO | 각각 “최대 어느 시점까지의 데이터 손실을 감수할지”와 “얼마 안에 서비스를 복구할지”라는 운영 목표입니다. 테스트 실행 시간과 같은 말이 아닙니다. |

## 개발과 문서

| 용어 | 쉽게 말하면 |
|---|---|
| Modular monolith | 배포 process는 하나지만, 개발 책임과 의존 규칙을 여러 논리 모듈로 나눈 구조입니다. Microservice라는 뜻은 아닙니다. |
| Physical module package | 같은 repository·wheel·process 안에서 한 module owner의 Python 파일을 모은 directory입니다. 별도 service, 독립 배포나 임의 구현 교체를 뜻하지 않습니다. |
| Leaf import | Package `__init__.py`의 재수출에 기대지 않고 `query_man.metadata.service`처럼 실제 symbol을 소유한 module을 직접 import하는 방식입니다. |
| Module owner | 특정 기능·파일·의미를 최종적으로 책임지는 모듈입니다. |
| Module interface | Allowed dependency 안에서 provider가 다른 내부 모듈에 보장하는 안정된 entrypoint와 호출 동작입니다. 문서는 중요한 경계만 설명하며 모든 public Python symbol을 목록화하지 않습니다. |
| External API / wire format | HTTP·MCP·CLI에서 주고받는 request, response, error와 인증 형식입니다. 내부 module interface와 별도 경계입니다. |
| Provider / consumer | Interface를 제공하는 모듈이 provider, 그것을 사용하는 모듈이 consumer입니다. |
| Protocol | Python에서 “이 method들을 제공해야 한다”는 객체의 모양을 나타내는 type입니다. |
| DTO | 모듈이나 계층 사이에서 정해진 값을 옮기는 data object입니다. |
| Lifecycle | 시작, 준비, reload, drain, 종료와 실패 정리의 순서와 결과입니다. |
| Composition root | 여러 모듈의 실제 구현을 골라 연결하는 허용된 조립 지점입니다. Production server는 Runtime이 조립합니다. |
| Trust boundary | 입력이나 권한을 그대로 믿으면 안 되어 검증·격리가 필요한 경계입니다. |
| Authority | 어떤 사실을 결정하는 최종 기준입니다. 예: 현재 첫 오픈 범위의 authority는 ADR 0025입니다. |
| ADR | 중요한 설계 선택과 이유·영향을 보존하는 Architecture Decision Record입니다. |
| Active TODO | 승인돼 실제로 지금 진행할 남은 일입니다. |
| Parked research | 조사 기록은 있지만 일정과 구현 승인은 없는 미래 후보입니다. |
| Evidence | Repository에서는 exact commit과 test/CI provenance, protected environment에서는 승인된 append-only change record입니다. 과거 PASS가 현재 전체 상태를 자동으로 증명하지 않습니다. |
| Repository acceptance | 코드와 로컬·CI 검증이 통과한 상태입니다. 실제 보호 환경에 배포했다는 뜻이 아닙니다. |
| Protected environment | 실제 secret, TLS, DB 권한, backup, route와 변경 기록 책임이 있는 제한된 운영 대상 환경입니다. |

## 약어를 만났을 때

`LAUNCH-02`, `CTRL-08`, `RLS-01` 같은 표기는 작업이나 결정의 안정적인 ID입니다. ID만 보고
의미를 추측하지 말고 [Active TODO](development-todo.md)와 [현재 결정](decisions/README.md)에서
현재 상태를 확인하세요. 삭제한 과거 ID는 [Git 기록 안내](verification/README.md)의 archive commit에
있습니다.
