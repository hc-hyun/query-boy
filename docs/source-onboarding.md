# Source Onboarding

Status: Git-reviewed YAML source authority

이 authority의 결정 기준은 [ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)이다.

## 먼저 알아둘 점

Query Man에서 **database**는 PostgreSQL의 실제 데이터 저장 공간이고, **source**는 그
database를 안전하게 조회하기 위해 검토한 연결 정보, 공개 view, 설명과 사용량 제한을 묶은
등록 단위다.

Source·verified query·budget의 유일한 authority는 Git에서 review하는 다음 YAML이다.

- `config/sources/*.yaml`
- `config/verified-queries.yaml`
- `config/budget-profiles.yaml`

새 source와 기존 source 변경은 모두 같은 pull request·test·배포 흐름을 따른다. 실행 중
admin API, Control DB, hot reload로 authority를 바꾸지 않는다. `QUERY_MAN_SOURCE_MODE`,
`QUERY_MAN_CONTROL_DSN`, `QUERY_MAN_SOURCE_ENCRYPTION_KEY`, `QUERY_MAN_REPLICA_ID`,
`QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS`는 폐기된 설정이며 하나라도 존재하면 시작을 거부한다.

현재 첫 오픈 source는 `development-issues`, `market-voc` 두 개다. 새 source는 inventory,
안전성, 품질과 rollback을 검토하고 정확한 승인을 받은 뒤 repository와 배포를 함께
변경한다. RLS source나 RLS에 의존하는 view는 현재 등록하지 않는다.

## 어떤 문서를 읽어야 하나요?

| 하려는 일 | 결과 |
|---|---|
| 새 DB/source 추가 또는 기존 source 변경 | [Source Extension Checklist](source-extension-checklist.md)로 end-to-end 영향을 검토한다. |
| 추가 가능성과 필요한 준비만 정리 | [`query-man-source-onboarding` Skill](../skills/query-man-source-onboarding/SKILL.md)로 plan-only handoff를 만든다. |
| 현재 source를 조회 | `uv run qm source list`, `show`, `validate`로 local YAML을 읽는다. 명령은 변경하지 않는다. |
| 현재 source의 데이터 질문 | [`query-man-text-to-sql` Skill](../skills/query-man-text-to-sql/SKILL.md)의 query workflow를 사용한다. |

## 핵심 용어

| 용어 | 쉬운 뜻 |
|---|---|
| Source ID | Query Man이 source를 구분하는 바뀌지 않는 이름이다. 예: `market-voc`. |
| Curated view | DB owner가 조회 목적에 맞는 column과 한 가지 grain만 공개한 읽기용 view다. |
| Reader | Curated view를 `SELECT`할 최소 권한 PostgreSQL login이다. |
| Manifest | `config/sources/*.yaml`의 source 설정이다. Secret 값은 아니라 secret 환경 변수 이름만 둔다. |
| Budget profile | Query 시간, 동시 실행, 임시 자원과 결과 크기의 제한 묶음이다. |
| Verified query | 대표 질문의 SQL과 예상 결과를 검사하는 회귀 시험이다. 사용자 SQL allowlist가 아니다. |

## 현재 새 source 검토 흐름

1. 사용할 질문, source owner, 환경과 database 변경 이력을 정한다.
2. DB owner가 PostgreSQL 18/UTF-8, non-RLS curated view와 최소 권한 reader를 준비한다.
3. Table·column comment에 grain, 단위, 상태값과 주의사항을 설명한다. Type과
   numeric precision/scale은 catalog에서 자동 수집한다. PII 여부는 comment만으로 허가하지
   않고 curated view·reader grant·policy로 강제한다.
4. 기존 budget profile을 선택하고 `config/sources/<source-id>.yaml`을 strict validation한다.
5. Metadata revision, SQL/result policy, verified query와 HTTP/MCP parity를 검증한다.
6. 대상 inventory, artifact, 배포, 중단 조건과 rollback을 승인받은 뒤 traffic 밖에서
   acceptance를 실행하고 재배포한다.

## 즉시 멈춰야 하는 경우

- RLS source 또는 RLS에 의존하는 view다.
- PostgreSQL 18, server/client/driver UTF-8 조건이 맞지 않는다.
- Reader 권한, 공개 relation, grain, PII 처리나 join 의미가 불명확하다.
- 현재 지원하는 최종 결과 type으로 대표 질문에 답할 수 없다.
- 기존 budget profile이 workload에 맞는다는 근거가 없다.
- Verified query, revision, container 또는 rollback 검증이 실패했다.
- Password, token, 전체 DSN 또는 encryption key가 YAML, 문서, log에 들어갔다.
- 정확한 inventory·배포 승인이나 protected environment 실행 승인이 없다.

## Plan-only Skill

[`query-man-source-onboarding`](../skills/query-man-source-onboarding/SKILL.md)은 비밀이 아닌 사실을 정리해 YAML
변경 plan을 만들지만 SQL/DDL/role/grant, credential 조회, repository 수정·commit·push를 실행하지
않는다. 결과의 `mutation_count: 0`은 계획만 준비됐다는 뜻이며, source가 등록됐다는
증거가 아니다.

## 관련 문서

- [Source Extension Checklist](source-extension-checklist.md)
- [Query Man 용어 사전](glossary.md)
- [ADR 0025](decisions/0025-static-non-rls-first-launch.md)
- [Source Catalog module](modules/source-catalog/README.md)
- [Query Cost And Resource Control](query-cost-control.md)
- [Verified Query Baseline](verified-queries.md)
