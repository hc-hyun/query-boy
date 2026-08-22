# Verified Query Contract

Status: Active

[`config/verified-queries.yaml`](../config/verified-queries.yaml)은 MVP golden question의
versioned executable contract다. 각 항목은 다음을 함께 고정한다.

- 안정적인 query ID와 source ID
- 사용자 질문과 deterministic read-only SQL
- SQL이 참조해야 하는 relation 집합
- SQL 생성에 사용한 exact metadata revision
- 정렬된 결과의 column, row count와 literal을 저장하지 않는 SHA-256 hash

검증은 현재 published metadata revision을 먼저 비교하고, AST에서 추출한 relation이
계약과 같은지 확인한 다음 guarded query service로 SQL을 실행한다. 결과가 truncate되거나
column, row count, hash 중 하나라도 바뀌면 publish/release gate를 실패시킨다.

```bash
uv run query-man-verify
```

시간 상대 질문은 회귀 결과가 날짜에 따라 움직이지 않도록 검증 기준일을 SQL literal로
고정한다. 실제 Text-to-SQL 요청은 현재 시각을 사용하되, regression fixture 갱신은
의도적인 revision과 expected hash review로만 수행한다.

현재 파일은 Git이 immutable history와 rollback을 제공하는 bootstrap 저장소다.
Runtime active snapshot과 no-deploy publish를 위한 control-plane 저장은 `META-05/06`에서
이 계약을 그대로 수용해야 한다.
