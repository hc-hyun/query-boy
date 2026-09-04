# 현재 결정

이 index는 current tree의 accepted 정책만 가리킵니다. 코드와 절차가 이미 단순해진 과정이나 완료 이력은
반복하지 않습니다.

## Core safety

| 결정 | 현재 의미 |
|---|---|
| [ADR 0001](0001-postgresql-ast-validation.md) | PostgreSQL AST와 relation/function/operator allowlist |
| [ADR 0002](0002-guarded-query-contract.md) | Query revision, limit, result, error와 cancel·rollback |
| [ADR 0003](0003-reader-and-resolved-object-policy.md) | Exact reader/session과 resolved DB object 검증 |
| [ADR 0025](0025-static-non-rls-first-launch.md) | Static non-RLS, PostgreSQL 18/UTF-8와 exact result OID launch |

## Source authority

| 결정 | 현재 의미 |
|---|---|
| [ADR 0030](0030-git-reviewed-yaml-source-authority.md) | Budget YAML authority와 retired managed capability |
| [ADR 0031](0031-no-pii-curated-view-boundary.md) | DB-owner-confirmed no-PII curated view |
| [ADR 0032](0032-reader-temp-admission-relaxation.md) | Database `TEMP`를 admission 조건으로 쓰지 않는 제한된 예외 |
| [ADR 0033](0033-explicit-source-tls-modes.md) | Explicit `disable`/`require`/`verify-full` transport policy |
| [ADR 0034](0034-source-view-package-and-direct-admission.md) | Manifest version 5, two-file package, marker와 direct admission |
| [ADR 0035](0035-reviewed-source-package-inventory.md) | Reviewed package directory 전체가 startup inventory |

현재 application surface는 HTTP `/sources`, `/meta`, `/query`와 health/admin monitoring endpoint입니다.
Authentication은 loopback anonymous, 단일 opaque API token 또는 opaque access-policy token만 지원합니다.
정확한 wire는 implementation과 [ADR 0002](0002-guarded-query-contract.md), 운영 절차는
[Operations](../operations.md)를 따릅니다.

## 별도 결정이 필요한 변경

다음은 현재 구현 일정이나 승인으로 해석하지 않습니다.

- RLS/tenant serving과 cross-source federation
- Runtime source reload 또는 DB-backed authority
- Result OID/canonical encoding 확대
- Multi-replica shared quota와 distributed query state
- DB-native 비용·경보와 external trace propagation

External API, persisted format, policy/revision, safety lifecycle, ownership 또는 protected procedure 의미가
바뀌면 [개발 지침](../development-guidelines.md#승인-규칙)에 따라 영향과 rollback을 별도 승인받습니다.

## Git archive

삭제한 roadmap, 날짜별 verification, retired 기능과 과거 ADR은 archive baseline
`1ff390ab67df215181810a84ac8b2ca8570eceee` 또는 해당 경로의 Git history에서 확인합니다. 과거 `Complete`
표시는 현재 serving이나 protected 실행 증거가 아닙니다. Git history를 rewrite하지 않습니다.
