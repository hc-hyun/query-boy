# Verified Query Baseline

Status: Active ADR 0025 static-launch executable dataset; managed authority preserved but inactive

현재 authority와 허용 result 범위는
[ADR 0025](decisions/0025-static-non-rls-first-launch.md)를 따른다.
RLS source는 현재 전면 quarantine되므로 이 active dataset에 포함하지 않는다.

[`config/verified-queries.yaml`](../config/verified-queries.yaml)은 static launch의
`development-issues`, `market-voc` 9개 golden question을 고정한 versioned executable dataset이다.
별도로 활성화하는 managed runtime은 이 file을 열지 않고
`control.verified_query_contracts`만 L2 evidence로 사용한다. 각 항목은 다음을 함께 고정한다.

- 안정적인 query ID와 source ID
- 사용자 질문과 deterministic read-only SQL
- SQL이 참조해야 하는 relation 집합
- SQL 생성에 사용한 exact metadata revision과 그 revision에 포함된 execution budget
- 정렬된 결과의 column, row count와 literal을 저장하지 않는 SHA-256 hash

검증은 현재 published metadata revision을 먼저 비교하고, AST에서 추출한 relation이
dataset에 선언된 relation set과 같은지 확인한 다음 guarded query service로 SQL을 실행한다. 결과가 truncate되거나
column, row count, hash 중 하나라도 바뀌면 publish/release gate를 실패시킨다.
Result hash는 guarded query의 canonical JSON scalar를 사용한다. 현재 final result는 PostgreSQL
base OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용하며 9개 baseline은 이 집합 안에 있다.
특히 `numeric`은 scale을 보존한 문자열이므로 float나 임의 text 변환으로 hash를 다시 만들지
않는다. Boolean과 bytea/JSON/float/array 등 다른 final OID는 첫 fetch 전에 거부된다. Source execution budget이나 revision-scoped source policy가
바뀌어도 revision이 바뀌며 새 경계 안에서 해당 revision의 verified-query baseline을 다시 실행해야 한다. Application 전역
SQL policy code 변경은 release regression으로 별도 검증한다.
Aware datetime은 UTC `+00:00` ISO 문자열이며 `Z`를 쓰지 않는다. Date는 기존 ISO 표현을
유지한다. Canonical-time policy material은 SQL policy와 모든 metadata
revision에 함께 들어가므로 global 변경 시 bootstrap 전체와 managed current/rollback-preserved
verified-query baseline 전체를 새 exact revision에서 재실행한다. 기존 immutable record를 수정·삭제하거나
membership을 자동 승계하지 않는다.

```bash
uv run query-man-verify
```

시간 상대 질문은 회귀 결과가 날짜에 따라 움직이지 않도록 검증 기준일을 SQL literal로
고정한다. 실제 Text-to-SQL 요청은 현재 시각을 사용하되, regression fixture 갱신은
의도적인 revision과 expected hash review로만 수행한다.

이 명령은 static launch repository 회귀용이며 managed verified-query inventory를 검증하거나
Control DB에 import하지 않는다.

현재 file은 Git이 immutable history와 rollback을 제공하는 static launch 저장소다. Managed
runtime은 동일한 shape의 verified-query record를 Control DB의 immutable revision-scoped row로 저장하며 no-deploy
source의 L2 publish 전에 실제 guarded execution과 결과 invariant를 검증한다. Filesystem verified-query dataset을
기존 source와 함께 이관할 때는 traffic 밖의 managed runtime에서 source를 L0/L1로 publish하고
verified-query admin endpoint로 exact-revision verified-query record를 저장한 뒤 L2로 승격한다. Startup
auto-import, file/DB merge, seed marker와 Control DB verified-query record의 filesystem write-back은 없다.
