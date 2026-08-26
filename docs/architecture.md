# Query Man Architecture

Status: 현재 구조 안내 — ADR 0025 static non-RLS first launch

이 문서는 Query Man이 지금 어떻게 실행되는지 설명합니다. 정확한 Python interface는
[모듈 안내](modules/README.md), 외부 API와 정책 의미는 해당 모듈 문서와
[accepted ADR](decisions/README.md)이 기준입니다.

낯선 말은 [용어 사전](glossary.md)에서 먼저 확인하세요.

## 한눈에 보는 현재 상태

Query Man은 하나의 애플리케이션 process 안에서 여러 책임을 모듈로 나눈 modular monolith입니다.
현재 첫 오픈은 [ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 좁은 범위만 사용합니다.

| 구분 | 현재 상태 |
|---|---|
| Source | `development-issues`, `market-voc` 두 개만 사용 |
| Runtime | 단일 Query Man replica |
| PostgreSQL | Major version 18, server/client UTF-8 |
| RLS | 모든 RLS source를 실행 전에 거부 |
| Query 결과 | exact seven result OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용 |
| Source authority | Repository의 static bootstrap 설정 |
| Control Plane | 구현은 보존하지만 현재 launch composition에서는 비활성 |
| 남은 일 | 실제 대상 환경 전환 `LAUNCH-02` |

저장소 구현·로컬 container·CI 통과는 실제 운영 배포 완료와 다릅니다. TLS, secret, backup,
대상 DB inventory, artifact와 route를 확인한 protected environment 실행이 남아 있습니다.

## 현재 실행 구조

```text
AI 또는 API client
       |
       | HTTP / MCP
       v
+---------------- Query Man 한 process ----------------+
| Delivery                                               |
|   인증·인가, 요청 검증, HTTP/MCP 응답                 |
|        |                                               |
|        +--> Metadata ------> 질문에 필요한 DB 설명    |
|        |        |                                      |
|        +--> Guarded Query -> SQL 검사·제한·실행       |
|                 |                                      |
| Source Catalog -+------ source/reader/budget 설정      |
|                                                        |
| Runtime: 위 구현의 조립, 시작·상태·종료               |
+--------------------------------------------------------+
       |
       +--> development_issues PostgreSQL
       `--> market_voc PostgreSQL

Assurance는 같은 저장소에서 품질·회귀 검증을 수행하지만 요청을 serving하는 계층은 아닙니다.
Control Plane은 코드에 남아 있으나 현재 첫 오픈에서는 연결하지 않습니다.
```

HTTP와 MCP는 같은 application service를 사용합니다. Transport에 따라 다른 source, 권한 또는
query 실행기를 사용하지 않습니다.

## 요청 한 건이 처리되는 순서

사용자가 “VOC가 한 번도 없는 기기는 몇 대인가?”라고 물었다고 가정합니다.

1. Client가 `/sources` 또는 MCP `list_sources`로 사용할 Source ID를 확인합니다.
2. `/meta` 또는 `get_context`가 질문에 필요한 view·column·grain과 두 revision을 반환합니다.
3. AI나 client가 그 설명을 바탕으로 SQL을 만듭니다. Query Man이 SQL을 생성하는 것은 아닙니다.
4. Delivery가 caller와 source 접근 권한을 확인합니다.
5. Guarded Query가 SQL·revision·허용 객체를 검사하고 resource slot을 확보합니다.
6. PostgreSQL read-only transaction에서 제한을 적용해 실행한 뒤 결과를 반환하거나 cancel·rollback합니다.

실행 순서는 다음 안전 경계를 유지합니다.

```text
authorize
-> validate one read-only statement and allowlists
-> acquire bounded concurrency
-> begin read-only transaction
-> apply transaction-local limits
-> stream bounded rows and bytes
-> commit, or cancel and rollback
```

Prompt나 Skill은 이 순서를 대신하지 못합니다. Gateway와 PostgreSQL이 실제로 강제합니다.

## 데이터 설명이 만들어지는 방식

Query Man은 source를 통째로 AI에게 보여주지 않습니다. 두 종류의 정보를 합칩니다.

| 종류 | 어디서 오는가 | 예시 |
|---|---|---|
| Physical catalog | Reader 권한으로 PostgreSQL `pg_catalog`에서 자동 수집 | View, column, type, key, index |
| Semantic overlay | 사람이 필요한 부분만 선언 | Grain, 업무 별칭, 승인 join, 상태 predicate |

DB comment는 비신뢰 설명 데이터로 취급하고 길이·제어 문자를 제한합니다. Comment 문장을 분석해
join을 자동 승인하지 않습니다. 복잡한 join이나 여러 grain의 집계는 source DB owner가 `ai`
schema의 curated view로 캡슐화합니다.

Metadata는 immutable revision으로 발행됩니다.

- `metadata_revision`: 특정 source의 schema·설명·실행 budget이 어느 상태인지 식별합니다.
- `sql_policy_revision`: 애플리케이션 전체 SQL 문법·함수·operator·결과 정책을 식별합니다.

Client는 context에서 받은 두 값을 query에 그대로 전달합니다. 하나라도 현재 값과 다르면 낡은
정보로 만든 SQL을 실행하지 않고 context 재조회를 요구합니다. 일반 업무 row의 INSERT·UPDATE만으로
metadata revision이 바뀌지는 않습니다.

## 일곱 모듈의 역할

| 모듈 | 쉬운 비유 | 책임 |
|---|---|---|
| Source Catalog | 주소록 | Source, reader, budget과 업무 설명 설정 |
| Metadata | 지도 제작자 | DB 구조를 수집해 질문별 context와 revision으로 발행 |
| Guarded Query | 보안 검색대 | SQL을 검사하고 제한 안에서 실행·취소·rollback |
| Delivery | 현관 | 인증·인가 후 HTTP와 MCP로 같은 기능 제공 |
| Runtime | 조립·운영 담당 | 실제 구현 연결, 설정, 시작, 상태와 종료 |
| Assurance | 검사소 | Metadata 품질과 verified query 결과 검증 |
| Control Plane | 관리실 | Managed mode의 source·revision·이력 관리; 현재 launch에서는 비활성 |

Static core는 `src/query_man` 아래 `source_catalog`, `metadata`, `guarded_query`, `delivery`, `runtime`,
`assurance`의 여섯 physical package로 나뉘고 managed 구현은 `managed` package에 있습니다. 모두 같은
repository·wheel·process에 속하며 별도 service가 아닙니다. Package `__init__.py`는 marker-only이고
interface는 owner의 leaf module에서 직접 import합니다. 정확한 file owner와 허용 의존은
[module index](modules/README.md)의 map에서 확인합니다.

## 개발 모듈 경계

모듈 분리의 목적은 배포를 여러 서비스로 쪼개는 것이 아닙니다. AI agent나 개발자가 전체
repository를 먼저 학습하지 않고 담당 모듈의 완전한 실행 흐름에 집중하기 위한 경계입니다.

일반적인 작업 순서는 다음과 같습니다.

1. `AGENTS.md`와 [module index](modules/README.md)를 읽습니다.
2. Primary module의 README에서 제공·소비 interface와 코드·테스트를 확인합니다.
3. 변경 지점부터 직접 consumer, transaction·cleanup과 실패 테스트까지 읽습니다.
4. 다른 모듈로 넘어갈 때만 그 모듈의 관련 interface와 테스트를 추가로 읽습니다.

다른 내부 모듈이 사용하도록 provider 문서와 dependency map에 명시한 Python symbol만 official
module interface입니다. HTTP/MCP request·response, persisted DB/config 형식, revision/allowlist
정책과 lifecycle invariant는 중요하지만 각각 별도 변경 경계입니다. 의미를 바꾸려면 정확한 영향과
승인을 먼저 확인합니다.

Production 구현 조립은 Runtime, candidate source의 격리 staging 조립은 Control Plane,
offline acceptance 조립은 Assurance CLI만 수행합니다.

## 현재 첫 오픈에서 사용하지 않는 구현

다음 기능은 삭제하지 않았지만 현재 serving 경로에는 참여하지 않습니다.

| 기능 | 코드 상태 | 현재 운영 상태 |
|---|---|---|
| Managed source hot-add와 Control DB authority | 구현·검증 이력 보존 | 별도 운영 결정 전 비활성 |
| 여러 replica의 convergence 관측 | 구현·soak 이력 보존 | 현재는 단일 replica |
| RLS tenant serving | 과거 구현과 연구 보존 | 모든 RLS source quarantine |
| 넓은 PostgreSQL 결과 type | 연구 기록 보존 | 일곱 OID만 허용 |
| DB-native 금액 귀속·workflow trace | Parked research | 일정과 구현 승인 없음 |

구현이 있다는 이유로 설정만 켜서 현재 launch에 포함하지 않습니다. 활성화에는 대상, interface·정책,
migration, rollback과 protected procedure의 별도 검토가 필요합니다.

## 새 데이터베이스가 들어오면

현재 static launch에서 새 DB나 Source ID는 단순 설정 추가가 아닙니다. Inventory와 launch 범위가
달라지므로 별도 승인을 받고 다음 end-to-end slice를 함께 확인합니다.

```text
Source DB의 curated view와 최소 권한 reader
-> Source manifest와 기존 budget 선택
-> Metadata 수집·revision
-> Guarded Query의 SQL/result policy
-> Verified question과 결과
-> Runtime artifact·배포·rollback
```

일반적으로 source별 Python 분기, 새 HTTP/MCP endpoint나 Control DB schema 변경은 필요하지
않습니다. 자세한 현재 절차는 [source extension checklist](source-extension-checklist.md)를 따릅니다.
Managed hot-add가 실제 요구가 되면 [source onboarding 안내](source-onboarding.md)에서 별도 경로를
선택합니다.

## Success Criteria

현재 첫 오픈의 성공 기준과 장기 제품 목표를 구분합니다.

현재 첫 오픈:

- 검토된 두 source만 단일 replica에서 제공합니다.
- PostgreSQL 18/UTF-8, non-RLS와 일곱 결과 OID를 fail-closed로 강제합니다.
- HTTP와 MCP의 권한·metadata·query 결과가 같습니다.
- 아홉 verified query와 container·보안 검증을 통과합니다.
- 실제 환경에서는 별도 승인된 `LAUNCH-02` cutover와 rollback 증거를 남깁니다.

장기 목표:

- Source별 Python 분기 없이 여러 PostgreSQL database를 같은 안전 경계로 제공합니다.
- 업무 의미가 필요한 DB에만 semantic overlay나 curated view를 추가합니다.
- 보안과 resource policy를 prompt가 아니라 gateway와 PostgreSQL이 강제합니다.
- Managed 운영을 활성화한 환경에서는 source lifecycle과 관측값을 한 관리 surface에서 봅니다.

## 결정과 상세 문서

- 현재 launch 범위: [ADR 0025](decisions/0025-static-non-rls-first-launch.md)
- 현재 physical package 구조: [ADR 0026](decisions/0026-physical-module-packages.md)
- 결정 전체 색인: [ADR index](decisions/README.md)
- 정확한 모듈 owner와 interface: [module index](modules/README.md)
- 현재 운영 전환: [operations](operations.md)
- 예제 source와 질문: [MVP data](mvp.md)
- 새 source 검토: [source onboarding](source-onboarding.md)
- 지금 남은 작업: [active TODO](development-todo.md)

## Completion Tracking

[Implementation roadmap](implementation-roadmap.md)은 완료 ID와 당시 evidence를 보존하는 과거
원장입니다. [Verification index](verification/README.md)의 각 기록도 적힌 commit·환경·범위만
증명합니다. 현재 상태와 우선순위는 이 문서, [active TODO](development-todo.md), accepted ADR과
현재 runnable test를 함께 확인하세요.
