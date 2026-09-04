# 검증과 Git 기록

Repository PASS와 protected environment evidence는 서로 다른 사실입니다.

## 현재 repository gate

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
```

PR/push CI는 Docker 없는 static/unit gate를 실행합니다. DB catalog/query 경계는 Query Cave integration,
container/release 경계는 built-image acceptance와 bounded load, dependency·history·image 검사는 security
workflow를 사용합니다. 정확한 실행 방법은 [Query Cave](../../query-cave/README.md)를 따릅니다.

PASS는 실행한 exact commit, command 또는 CI run에만 적용합니다. 과거 PASS나 다른 commit의 결과를 현재
tree 전체의 증거로 확장하지 않습니다.

## Protected environment evidence

현재 작업 상태와 의존성은 [Active TODO](../development-todo.md), 실행과 rollback 순서는
[Operations](../operations.md)가 소유합니다. Protected 실행은 access, scope, target, stop condition과
change-record 책임을 승인받고 해당 환경의 append-only/immutable 기록 시스템에 남깁니다.

Repository 문서는 실제 credential, raw SQL/result, protected target이나 mutable evidence ledger가
아닙니다. Repository 또는 Query Cave PASS를 protected 작업 완료로 기록하지 않습니다.

## 삭제한 기록 찾기

과거 roadmap, 날짜별 verification과 retired 구현 문서는 archive baseline
`1ff390ab67df215181810a84ac8b2ca8570eceee` 또는 각 경로의 Git history에서 읽습니다.

```bash
git show 1ff390ab67df215181810a84ac8b2ca8570eceee:<path>
git log --follow -- <path>
```

과거 문서의 `Complete`는 현재 support나 protected activation을 뜻하지 않습니다. Current tree에 완료
요약을 되살리거나 Git history를 rewrite하지 않습니다. 새 current 문서는 계속 적용되는 계약이나 절차가
code/config만으로 충분히 드러나지 않을 때만 추가합니다.
