# 검증과 Git 기록

Repository PASS와 protected environment evidence는 서로 다른 사실입니다.

## 현재 repository gate

기본 local gate는 다음과 같습니다.

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
```

DB catalog/query 경계는 하나의 test-local source를 사용하는 integration lane, container/release 경계는
built-image acceptance와 bounded load를 추가합니다. Production source별 schema·seed나 업무 결과 corpus를
CI fixture로 복제하지 않습니다.

PASS는 실행한 exact commit, command/CI run과 결과에만 적용됩니다. 과거 PASS나 다른 commit의 결과를
현재 tree 전체의 증거로 확장하지 않습니다.

## Protected environment evidence

다음 작업은 repository에서 실행되지 않았습니다.

| 작업 | 필요한 별도 evidence | 상태 |
|---|---|---|
| `DBENV-01` | Exact DB/source/reader, privilege, marker/revision과 safety probes | 미실행 |
| `AUTHENV-01` | Exact token authority, secret path와 query/operator 분리 | 미실행 |
| `LAUNCH-02` | Approved image/config, traffic-off acceptance, cutover/rollback | 미실행 |

Protected 실행은 access, scope, target, stop condition과 change-record 책임을 승인받고 환경의
append-only/immutable 기록 시스템에 남깁니다. Repository 문서는 실제 credential, raw SQL/result,
protected target이나 mutable evidence ledger가 아닙니다.

## 삭제한 기록 찾기

과거 roadmap, 날짜별 verification과 retired 구현 문서는 archive baseline
`1ff390ab67df215181810a84ac8b2ca8570eceee` 또는 각 경로의 Git history에서 읽습니다.

```bash
git show 1ff390ab67df215181810a84ac8b2ca8570eceee:<path>
git log --follow -- <path>
```

과거 문서의 `Complete`는 현재 support나 protected activation을 뜻하지 않습니다. Current tree에 완료
요약을 되살리거나 Git history를 rewrite하지 않습니다. 새 current 문서는 계속 적용되는 계약이나 실행
절차가 code/config만으로 충분히 드러나지 않을 때만 추가합니다.
