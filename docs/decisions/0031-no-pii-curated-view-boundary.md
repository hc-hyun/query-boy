# ADR 0031: No-PII Curated-View Boundary

Status: Accepted

Date: 2026-08-30

Decision ID: `QB-NO-PII-VIEW-BOUNDARY-20260830`

Baseline: `1ff390ab67df215181810a84ac8b2ca8570eceee`

## Context

Query Man의 query 경로는 relation allowlist와 reader grant를 강제하지만, curated view 안의 column을
개인정보(PII)로 탐지·분류하거나 별도로 인가하지 않는다. Comment와 question rule에 PII 검토 안내를
반복해도 실제 노출을 차단하지 못하고, database view owner의 공개 범위 결정과 애플리케이션의 책임이
겹쳐 보이게 한다.

현재 source는 DB owner가 만든 reviewed curated view를 통해서만 공개한다. 따라서 개인정보 경계도
view를 만들고 권한을 부여하는 시점에 한 번 명확히 정하는 편이 더 작고 실제 통제 위치와 일치한다.

## Decision

- Query Man은 개인정보나 개인 민감정보를 탐지, 분류, masking/pseudonymization 또는 column 단위로
  인가하지 않는다.
- DB owner는 개인정보와 개인 민감정보를 제거한 reviewed curated view만 Query Man에 제공한다.
  정확히 공개되는 view의 데이터 범위를 owner가 확인하지 못하면 onboarding과 publish를 중단한다.
- Comment, source manifest, semantic question rule와 prompt는 이 책임을 대신하거나 노출을 허가하지
  않는다. Comment에는 실제 개인정보 값이나 secret을 넣지 않는다.
- Query Man은 DB owner가 확인한 view를 no-PII source boundary로 신뢰한다. View 안에 잘못 포함된
  개인정보를 애플리케이션이 사후 탐지하거나 보정한다고 약속하지 않는다.

이 정책은 onboarding과 source admission의 책임 경계다. 개인정보 분류 기준과 view에서 제거하거나
비식별화하는 방법은 해당 database owner의 data-governance 절차가 소유한다.

## Compatibility And Supersession

이 결정은 [ADR 0030](0030-git-reviewed-yaml-source-authority.md)의 comment 기반 PII-review guidance와
그 guidance를 확인하는 verification 항목만 supersede한다. Git-reviewed YAML authority, comment의
untrusted-input 처리, secret 비공개, 최소 권한 reader, schema/relation/function/operator allowlist,
read-only transaction, resource limit와 query safety는 유지한다.

Git archive baseline `1ff390ab67df215181810a84ac8b2ca8570eceee`의 ADR 0009에 기록된 미래
classification/authorization 선택지는 현재 capability가 아니며 다시 도입하려면 별도 승인과 새 정책
결정이 필요하다.

Source manifest schema, Python module interface, HTTP/MCP wire format, SQL policy, reader policy와 database
DDL은 바뀌지 않는다. Domain-lab의 PII 전용 question rule을 제거하면 해당 source의 metadata revision은
정상적으로 바뀌지만 verified SQL과 expected result identity는 유지한다.

## Consequences

책임과 통제가 curated view 한 곳에 모여 application-level PII rule, comment classification guidance와
중복 검토 절차를 제거할 수 있다. 반면 DB owner가 view를 잘못 구성하면 Query Man은 그 안의 개인정보를
찾아내지 못한다. 이 위험은 exact view review, no-PII 확인, 최소 권한 reader와 불명확할 때의 onboarding
중단으로 관리한다.

## Change And Rollback

Active source/onboarding 문서와 plan-only Skill을 이 경계로 통일하고, domain-lab source YAML의 PII 전용
question rule과 연결된 quality case를 제거한다. 변경된 metadata revision만 verified-query YAML에
반영한다. 실제 database, view, grant, credential 또는 protected environment는 변경하지 않는다.

Rollback은 이 Git change set을 review된 revert로 되돌리고 이전 YAML과 revision이 포함된 artifact를
다시 배포하는 것이다. Runtime fallback이나 database mutation은 rollback이 아니다.

## Verification

- Source/onboarding 문서에 단일 no-PII curated-view 경계가 일관되게 표현되는지 확인한다.
- Domain-lab quality case가 남은 semantic question rule과 정확히 대응하는지 확인한다.
- Clinical metadata revision을 실제 catalog에서 다시 계산하고 세 verified query가 같은 revision을
  사용하는지 확인한다.
- Source registry/revision, onboarding Skill, documentation, domain-lab assurance와 전체 test gate를
  통과한다.
