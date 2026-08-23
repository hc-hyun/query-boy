# Managed Source Startup Verification — 2026-08-23

Status: Complete

## Scope

`CTRL-02`의 mutually exclusive bootstrap/managed runtime mode, zero-bootstrap managed startup,
Control DB lifecycle/verified-contract precedence와 기존 admin API를 사용한 one-time cutover를
검증한다.

검증 대상은 다음과 같다.

- `bootstrap`은 repository source/verified contract만 사용하고 Control DSN/key를 거부한다.
- `managed`는 Control DSN/key를 모두 요구하고 source/verified file을 열지 않는다.
- Managed restart는 Control DB generation, rollback, deactivate와 verified contract만 복원한다.
- Cold Control scan 실패는 empty registry와 unavailable readiness를 유지하고 file로 fallback하지
  않는다. 정상 적용 뒤 poll 실패는 마지막 verified state를 유지한다.
- 기존 source는 traffic 밖에서 L0/L1 publish, Control DB verified contract, L2 publish 순서로
  이관한다. Startup import, marker, 새 endpoint와 schema migration은 없다.

## Commands And Result

아래 결과는 현재 worktree에서 local fixture credential을 `.env`로 주입해 실행했다.

```text
uv run pytest -q tests/test_runtime_config.py tests/test_managed_mode.py tests/test_source_admin.py
  PASS (42 passed in 6.81s)
uv run pytest
  PASS (285 passed, 33 deselected in 30.03s)
uv run pytest -m integration -q tests/test_control_startup.py
  PASS (1 passed in 16.38s)
uv run pytest -m integration -q
  PASS (21 passed, 297 deselected in 95.23s)
uv run ruff check .
  PASS
uv run mypy src
  PASS (Success: no issues found in 26 source files)
```

Result: PASS

## Acceptance Matrix

| Boundary | Expected evidence | Result |
|---|---|---|
| Configuration matrix | Bootstrap+Control 설정과 managed missing DSN/key가 startup 전에 거부됨 | PASS |
| Bootstrap regression | Existing local/CI source와 filesystem verified query contract가 그대로 동작함 | PASS |
| Zero-bootstrap | Missing source directory/verified file에서도 managed app이 Control state로 시작함 | PASS |
| File non-read | Managed construction이 source/verified loader를 호출하지 않음 | PASS |
| Contract precedence | Filesystem-only contract는 managed L2 gate를 만족하지 않고 Control DB contract만 만족함 | PASS |
| Lifecycle restart | Fresh process가 active generation/state version/metadata revision을 Control DB에서 복원함 | PASS |
| Deactivate precedence | 같은 ID의 bootstrap seed가 있어도 deactivated source가 restart 뒤 absent임 | PASS |
| Rollback precedence | Restart 뒤 Control pointer가 선택한 older generation/revision이 적용됨 | PASS |
| Cold scan failure | Registry가 empty이고 `/ready`가 503 unavailable이며 file fallback이 없음 | PASS |
| Warm scan failure | Last verified source는 유지되고 reload component/readiness가 degraded임 | PASS |
| Schema boundary | Existing immutable lifecycle/metadata/contract tables만 사용하고 new migration/marker가 없음 | PASS |

Integration test는 development authority DB를 재사용하지 않고 `CTRL-01`의 function-scoped disposable
Control DB를 사용한다. Test가 만든 source generation과 verified contract는 pool close 뒤 scratch
database와 함께 제거된다. 최종 확인에서 `query_man_control_test_%` database와 container의
worktree migration temp directory는 각각 0개였다.

첫 integration 실행은 Compose container의 기존 bind mount가 다른 checkout의 migration tree를
가리켜 current worktree schema와 어긋나는 문제를 드러냈다. Fixture는 current worktree의 numbered
migration과 security reconciliation file을 container temp directory로 복사해 그 경로만 적용하도록
수정했다. 수정 뒤 `test_control_startup.py`가 통과했다. 이는 production migration 동작 변경이
아니라 disposable test가 검사 중인 checkout과 같은 schema를 사용하게 한 격리/재현성 수정이다.

전체 integration 첫 실행에서는 기존 representative local-load test가 공유 PostgreSQL의 순간
부하로 1초 queue timeout을 한 번 넘었다. 같은 test를 단독 재실행하면 `1 passed in 1.69s`였고,
전체 suite 재실행도 21개 모두 통과했다. 안전 limit을 완화하지 않았으며 같은 flake가 반복되면
test 전용 DB resource 격리 또는 host/DB saturation evidence 수집을 먼저 추가한다.

## Operator Cutover Acceptance

실제 production cutover change record에는 다음을 별도로 첨부한다.

1. Traffic 밖의 managed instance와 대상 Control DB identity
2. Source별 L0/L1 publish generation과 metadata revision
3. Import한 reviewed contract ID/revision과 guarded invariant 결과
4. L2 generation, intended active/deactivated state와 serving replica restart 결과
5. Source/verified file이 없거나 같은 ID seed가 남은 상태의 동일 inventory/revision 확인

Credential, encryption key, DSN, question 원문, SQL과 result literal은 이 evidence에 기록하지 않는다.
Mutation timeout은 blind retry하지 않고 authoritative Control DB state를 먼저 reconcile한다.

## Intentionally Unchanged

- Runtime mode는 deployment configuration이며 Control DB에 mode/origin/import marker를 저장하지
  않는다.
- Existing staged source and verified-query admin endpoints를 재사용한다. Bulk/startup import endpoint는
  없다.
- Budget profile/access policy는 file-backed deployment configuration으로 유지한다.
- Query/admin identity 분리와 shared-access fail-closed 전환은 `CTRL-03`이다.

## Future Triggers

- Bootstrap runtime에 persistent Control DB metadata store가 실제로 필요해지면 managed DB와 격리된
  새 topology와 authority를 별도 ADR로 설계한다. DSN-only 조합을 암묵적으로 복원하지 않는다.
- Automated production import가 필요해지면 idempotent receipt, target-bound credential broker와
  별도 threat model을 먼저 설계한다.
- Backup restore와 multi-replica zero-bootstrap recovery의 최종 증거는 `CTRL-09`에서 추가한다.
- Representative local-load queue flake가 반복 관측되면 timeout을 높이기 전에 test DB resource
  격리와 host/DB saturation 진단을 추가한다.
