# 검증과 Git 기록

Status: Current — 현재 gate와 과거 repository 기록 조회 안내

현재 구현의 증거는 오래된 날짜별 성공 문서가 아니라 **검증한 exact Git commit과 그 commit에서
실행한 gate 결과**입니다. 과거 `Complete` 문서는 이후 변경을 증명하지 않으므로 current tree에서
유지하지 않습니다.

| 항목 | 현재 판정 |
|---|---|
| `LAUNCH-01-A` repository 구현 | Accepted; exact launch scope는 ADR 0025 |
| `QB-YAML-SOURCE-AUTHORITY-20260829` repository removal | Accepted; exact authority는 ADR 0030 |
| `DBENV-01` protected DB binding | 미실행; Active TODO |
| `AUTHENV-01` protected authentication binding | 미실행; Active TODO |
| `LAUNCH-02` protected deployment | 미실행; Active TODO |
| 외부 Control DB inventory·보존·폐기 | 이 repository 작업에서 접근·변경·실행하지 않음; 별도 승인 필요 |

## 현재 repository gate

모든 변경의 최소 gate는 다음과 같습니다.

```bash
uv run ruff check .
uv run ruff check src/query_man/runtime --select C901 --config "lint.mccabe.max-complexity=19"
uv run mypy src
uv run pytest
```

DB catalog/query 경계는 관련 integration lane을, container·release 경계는 container acceptance와
`uv run query-man-verify`를 추가합니다. 정확한 범위는
[활성 개발 지침](../development-guidelines.md#tests)과 primary module README가 정합니다.

Verified query의 format, 비교 순서와 rollback은
[Assurance module](../modules/assurance/README.md#verified-query-회귀검사)을 따릅니다. 현재 first-launch
profile의 exact acceptance 항목은 [ADR 0025](../decisions/0025-static-non-rls-first-launch.md)에 있습니다.

## protected environment evidence

Repository test 통과는 protected environment 배포 승인이거나 실행 증거가 아닙니다. 실제 작업은
target, operator, artifact digest, source·DDL·role·setting inventory, result, stop/rollback 상태와
change-record owner를 승인된 환경 기록 시스템에 append-only/immutable하게 남깁니다. Secret, token,
SQL literal과 내부 DB 오류는 기록하지 않습니다.

Repository에는 그 운영 기록을 복사한 날짜별 서술 문서를 추가하지 않습니다. 필요하면 비밀이 없는
change-record ID와 exact Git commit만 handoff에 연결하고, 환경 기록의 retention·access 정책을
그 시스템에서 유지합니다.

## 삭제한 기록 찾기

2026-08-22~2026-08-29의 날짜별 verification 34개와 과거 implementation roadmap은 정리 직전 commit
`1ff390ab67df215181810a84ac8b2ca8570eceee`에 그대로 남아 있습니다.

```bash
git ls-tree -r --name-only 1ff390ab67df215181810a84ac8b2ca8570eceee docs/verification
git show 1ff390ab67df215181810a84ac8b2ca8570eceee:docs/verification/2026-08-26-static-first-launch.md
git show 1ff390ab67df215181810a84ac8b2ca8570eceee:docs/implementation-roadmap.md
```

그보다 앞선 1차 정리에서 제거한 retired Control Plane tombstone과 완료된 module-boundary·onboarding
계획 8개는 baseline `95b3068a16629bf043696938d049e36efc9a162f`에서 확인합니다.

```bash
git ls-tree -r --name-only 95b3068a16629bf043696938d049e36efc9a162f docs
```

이 기록은 당시 commit·환경·명령 범위만 설명합니다. 현재 상태는 [Architecture](../architecture.md),
[Active TODO](../development-todo.md), 현행 ADR과 지금 실행한 test/CI 결과로 판단합니다. Git history를
rewrite하거나 archived record를 현재 의미로 소급 해석하지 않습니다.

## 새 기록을 남기는 기준

- 현행 계약·절차가 바뀌면 owner 문서, 필요한 ADR과 runnable test를 같은 change set에서 갱신합니다.
- Repository 완료 사실은 commit/PR/CI provenance로 남깁니다. 날짜별 PASS 요약 문서를 만들지 않습니다.
- Protected action의 실제 evidence는 실행 승인을 받은 뒤 환경 change record에만 append합니다.
- 보류 연구는 [Active TODO의 보류 표](../development-todo.md#현재-일정에-없는-일)에 한 줄로 유지하고,
  요구와 승인 전에는 설계 일지를 늘리지 않습니다.
