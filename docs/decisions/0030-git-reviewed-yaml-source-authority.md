# ADR 0030: Git-Reviewed Budget Authority

Status: Accepted; source-package details superseded by ADR 0034 and ADR 0035

Decision ID: `QB-YAML-SOURCE-AUTHORITY-20260829`

## Context

초기 구현의 Control DB, encrypted source registry와 reload protocol은 static first launch에 비해 별도
schema, key lifecycle, cache와 recovery 절차를 만들었습니다. Source별 결과 corpus도 runtime publication
조건을 중복했습니다.

## Decision

- `config/budget-profiles.yaml`이 query/metadata resource budget의 유일한 versioned authority입니다.
- Source는 manifest에서 기존 profile 이름을 선택하며 request나 environment로 limit을 override하지
  않습니다.
- Runtime source authority는 [ADR 0034](0034-source-view-package-and-direct-admission.md)의 두 파일
  package이고, startup inventory는 [ADR 0035](0035-reviewed-source-package-inventory.md)를 따릅니다.
- Managed Control DB, source encryption key, source reload/replica protocol과 별도 result/quality registry는
  retired 상태입니다. 해당 설정이 들어오면 alternate authority로 fallback하지 않고 startup을
  fail-closed합니다.

SQL AST/allowlist, reader privilege, read-only transaction, timeout/concurrency/row/byte limit,
cancel·rollback, revision drift와 secret redaction은 authority 단순화와 무관하게 유지합니다.

## 변경과 rollback

Budget 변경은 policy 의미 변경이므로 YAML, registry cross-field validation, query admission/transaction,
load test와 운영 memory/timeout을 함께 review합니다. Protected 환경에서는 approved commit을 고정하고
traffic 밖에서 검증하며 문제가 있으면 직전 YAML과 application revision으로 rollback합니다.

Retired capability를 다시 도입하려면 실제 dynamic-source 요구, ownership, migration, key recovery,
multi-replica consistency와 별도 failure-mode 검증을 승인받아야 합니다. 과거 구현은 current tree가 아닌
[Git 기록 안내](../verification/README.md)에서 확인합니다.
