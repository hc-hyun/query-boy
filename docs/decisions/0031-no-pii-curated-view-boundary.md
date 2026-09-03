# ADR 0031: No-PII Curated-View Boundary

Status: Accepted

Decision ID: `QB-NO-PII-VIEW-BOUNDARY-20260830`

## Context

Application이 모든 source의 개인정보를 정확히 탐지·분류·masking한다고 약속하면 업무별 규칙과
원본 접근 권한이 gateway에 다시 생깁니다. Column 이름 검사만으로도 공개 안전성을 증명할 수 없습니다.

## Decision

DB owner는 개인정보와 개인 민감정보를 제거한 reviewed curated view만 `ai` schema에 공개합니다.
필요한 탐지, 분류, masking/pseudonymization 또는 column 단위 접근 제어는 source database에서 적용하고
review합니다. Query Man reader는 curated view만 읽으며 base relation 접근을 갖지 않습니다.

`source.yaml`의 public 설명과 `views.sql` comment에도 실제 개인정보를 넣지 않습니다. Query Man은
source/column allowlist, output row·byte limit, literal/credential redaction을 계속 강제하지만 이를
DB-owner-confirmed no-PII 경계의 대체 수단으로 표현하지 않습니다.

## 변경과 rollback

새 source 또는 view output 변경은 DB/data owner의 no-PII sign-off와 DBA apply 승인이 필요합니다.
개인정보 가능성, unexpected column, excessive reader privilege 또는 검토하지 않은 dependency가 발견되면
적용·전환을 중단합니다. 이미 적용됐다면 application admission을 차단하고 DBA가 직전 view/grant를
복구한 뒤 incident 절차로 노출 범위를 확인합니다.

Source package와 marker 절차는 [ADR 0034](0034-source-view-package-and-direct-admission.md)를 따릅니다.
