# Query Boy 로컬 scale fixture

Status: Optional local fixture; production 사용 금지

이 절차는 `voc-gen`이 만든 합성 데이터를 Query Boy의 기존 `market_voc` schema에 적재해 대량 조회를
시험하기 위한 로컬 전용 절차다. `compose.yaml`과 `compose.scale.yaml`을 함께 사용할 때만 동작하며,
기본 `query-man` project나 기본 PostgreSQL volume은 수정하지 않는다.

## 격리 경계

| 항목 | Scale fixture 값 |
|---|---|
| Compose project | `query-man-scale-fixture` |
| PostgreSQL container | `query-man-scale-fixture-postgres` |
| Query Man container | `query-man-scale-fixture-app` |
| PostgreSQL volume | `query-man-scale-fixture-postgres-data` |
| PostgreSQL host port | `${QUERY_MAN_SCALE_POSTGRES_PORT:-55433}` |
| Query Man host port | `${QUERY_MAN_SCALE_PORT:-3100}` |
| Container marker | `QUERY_MAN_SCALE_FIXTURE=1` |
| PostgreSQL marker | `query_boy.scale_fixture=on` |

Overlay는 base Compose의 init mount를 추가하거나 교체하지 않는다. 따라서 시작 상태는 기존
`development-issues`, `market-voc` 두 source와 동일하며 다른 source fixture를 포함하지 않는다.
PostgreSQL command도 base의 `pg_stat_statements`와 slow-query
logging 설정을 보존하고 scale marker만 추가한다.

이 구성은 loopback, 로컬 secret과 관리자 적재 계정을 사용하는 disposable fixture다. TLS, backup,
운영 inventory, migration 또는 protected environment 변경 절차가 아니므로 production에 사용하지
않는다.

## 1. 깨끗한 base fixture 시작

Query Boy root에서 로컬 `.env`를 먼저 준비한다. `compose.scale.yaml`은 Compose 2.24 이상의
`!override` tag를 사용해 base host port를 scale port로 완전히 교체한다.

```bash
export COMPOSE_FILE=compose.yaml:compose.scale.yaml
docker compose config --quiet
docker compose up -d --wait postgres

test "$(docker compose exec -T postgres printenv QUERY_MAN_SCALE_FIXTURE)" = "1"
test "$(
  docker compose exec -T postgres \
    psql \
      --username query_man_admin \
      --dbname market_voc \
      --tuples-only \
      --no-align \
      --command "SELECT pg_catalog.current_setting('query_boy.scale_fixture', true)"
)" = "on"

./scripts/apply-db.sh
QUERY_MAN_POSTGRES_HOST=127.0.0.1 \
POSTGRES_PORT="${QUERY_MAN_SCALE_POSTGRES_PORT:-55433}" \
  uv run query-man-verify
```

두 marker 검사 중 하나라도 실패하면 적재하지 않는다. `COMPOSE_FILE`을 export하는 이유는 내부에서
bare `docker compose`를 호출하는 `apply-db.sh`도 반드시 scale project를 보게 하기 위해서다.
`query-man-verify`는 대량 적재 **전**의 기존 9개 static baseline을 확인한다.

`apply-db.sh`는 적재 후 다시 실행하지 않는다. 이 script의 exact base-row 검사는 대량 데이터가 있는
상태에서 의도대로 실패한다. Base를 다시 만들려면 아래의 scale-only `down -v`로 volume을 지우고 이
절을 처음부터 반복한다.

## 2. `voc-gen` loader 연결

Loader에는 DSN이나 secret literal을 command line으로 전달하지 않고 다음 환경 변수만 사용한다.

| 환경 변수 | 의미 | 기본값 또는 요구사항 |
|---|---|---|
| `QUERY_BOY_DB_HOST` | PostgreSQL host | `127.0.0.1` |
| `QUERY_BOY_DB_PORT` | PostgreSQL host port | `55433`; port override 시 같은 값으로 지정 |
| `QUERY_BOY_DB_NAME` | Target database | 반드시 `market_voc` |
| `QUERY_BOY_DB_USER` | Loader user | `query_man_admin` |
| `QUERY_BOY_DB_PASSWORD` | Loader password | 필수; 문서나 shell history에 값을 넣지 않음 |
| `QUERY_BOY_DB_SSLMODE` | Local connection SSL mode | 기본 `disable` |
| `QUERY_BOY_DB_CONNECT_TIMEOUT` | 연결 timeout(초) | 기본 `10` |

Password는 화면에 남지 않게 현재 shell에서 입력한다.

```bash
export QUERY_BOY_DB_HOST=127.0.0.1
export QUERY_BOY_DB_PORT="${QUERY_MAN_SCALE_POSTGRES_PORT:-55433}"
export QUERY_BOY_DB_NAME=market_voc
export QUERY_BOY_DB_USER=query_man_admin
read -r -s -p 'QUERY_BOY_DB_PASSWORD: ' QUERY_BOY_DB_PASSWORD
echo
export QUERY_BOY_DB_PASSWORD
```

Loader는 transaction 안에서 target database 이름, `query_boy.scale_fixture=on`, exact `voc` base table
inventory를 확인한 뒤에만 `COPY` staging을 시작한다. Stage/target count와 foreign key audit가 모두
맞아야 commit하고, 실패하면 전체 transaction을 rollback한다.

## 3. Pilot 적재

먼저 1/1000 크기의 pilot을 적재한다.

```bash
cd ../voc-gen
uv run voc-factory load-query-boy-scale \
  --profile scale_profiles/query_boy.market_voc.pilot.json
cd ../query-boy
```

성공 report에서 `staged`와 `loaded`가 각각 users 24, models 29, devices 100, cases 300, comments
600으로 같고 모든 `integrity` 값이 0이어야 한다. Users는 base와 같은 natural ID를 upsert하고,
대표 Galaxy model 29개는 기존 예제 model 8개와 함께 남는다. 물리 table의 pilot 완료 count는 다음과
같다.

| Table | Base | Pilot scale-owned | Pilot 완료 |
|---|---:|---:|---:|
| `voc.users` | 24 | 24 | 24 |
| `voc.product_models` | 8 | 29 | 37 |
| `voc.devices` | 400 | 100 | 500 |
| `voc.cases` | 1,200 | 300 | 1,500 |
| `voc.case_comments` | 3,000 | 600 | 3,600 |

아래 [적재 후 검사](#적재-후-검사)를 실행해 pilot을 확인한다. Pilot이 통과하면 full fixture를 깨끗한
base에서 재현하도록 scale project의 volume만 삭제한다.

```bash
docker compose \
  --file compose.yaml \
  --file compose.scale.yaml \
  down -v --remove-orphans
```

그런 다음 [깨끗한 base fixture 시작](#1-깨끗한-base-fixture-시작)을 다시 실행한다. 이 명령은 명시한
`query-man-scale-fixture` project와 `query-man-scale-fixture-postgres-data`만 제거하며 기본
`query-man_postgres_data`에는 닿지 않는다.

## 4. Full 100만 건 적재

깨끗한 base fixture와 두 marker를 다시 확인한 뒤 full profile을 실행한다.

```bash
cd ../voc-gen
uv run voc-factory load-query-boy-scale \
  --profile scale_profiles/query_boy.market_voc.scale_1m.json
cd ../query-boy
```

Full profile이 소유하는 fact row는 devices 100,000, cases 300,000, comments 600,000으로 정확히
1,000,000건이다. 기존 static fixture를 보존하므로 최종 물리 count는 다음과 같다.

| Table | Base | Full scale-owned | Full 완료 |
|---|---:|---:|---:|
| `voc.users` | 24 | 24 | 24 |
| `voc.product_models` | 8 | 29 | 37 |
| `voc.devices` | 400 | 100,000 | 100,400 |
| `voc.cases` | 1,200 | 300,000 | 301,200 |
| `voc.case_comments` | 3,000 | 600,000 | 603,000 |

다섯 table의 전체 row는 1,004,661건이다. Loader는 natural ID 기준 scale-owned count와 참조 무결성을
감사하므로 단순히 전체 table count만 보고 성공으로 판단하지 않는다.

## 적재 후 검사

Loader가 다섯 table을 `ANALYZE`하고 성공 report에 대상을 기록한다. 수동 변경이 뒤따랐다면 아래처럼
통계를 다시 수집한 뒤 physical count, orphan과 다양성을 확인한다.

```bash
docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname market_voc \
    --set=ON_ERROR_STOP=1 <<'SQL'
ANALYZE voc.users, voc.product_models, voc.devices, voc.cases, voc.case_comments;

SELECT
  (SELECT count(*) FROM voc.users) AS users,
  (SELECT count(*) FROM voc.product_models) AS models,
  (SELECT count(*) FROM voc.devices) AS devices,
  (SELECT count(*) FROM voc.cases) AS cases,
  (SELECT count(*) FROM voc.case_comments) AS comments;

SELECT
  count(DISTINCT defect_category) AS defect_categories,
  count(DISTINCT severity) AS severities,
  count(DISTINCT status) AS statuses,
  count(DISTINCT intake_channel) AS intake_channels,
  count(DISTINCT market_region) AS market_regions,
  count(DISTINCT country_code) AS countries
FROM voc.cases
WHERE voc_no >= 'VOC-100001';

SELECT
  (SELECT count(*)
   FROM voc.cases AS case_row
   LEFT JOIN voc.devices AS device ON device.id = case_row.device_id
   WHERE device.id IS NULL) AS orphan_cases,
  (SELECT count(*)
   FROM voc.case_comments AS comment
   LEFT JOIN voc.cases AS case_row ON case_row.id = comment.case_id
   WHERE case_row.id IS NULL) AS orphan_comments;

SELECT
  model.model_name,
  count(*) AS voc_count,
  count(*) FILTER (
    WHERE case_row.status NOT IN ('RESOLVED', 'CLOSED')
  ) AS unresolved_voc_count
FROM voc.cases AS case_row
JOIN voc.devices AS device ON device.id = case_row.device_id
JOIN voc.product_models AS model ON model.id = device.product_model_id
GROUP BY model.model_name
ORDER BY voc_count DESC, model.model_name
LIMIT 8;
SQL
```

`orphan_cases`와 `orphan_comments`는 모두 0이어야 한다. 그다음 scale Query Man container를 시작하고
기존 container/MCP smoke를 같은 project에 대해 실행한다.

```bash
docker compose up -d --build --wait query-man
test "$(docker compose exec -T query-man printenv QUERY_MAN_SCALE_FIXTURE)" = "1"
./scripts/verify-container.sh
```

`verify-container.sh`는 unchanged `development-issues` source로 guarded-query path를 확인한다. Scale
`market-voc` 질문은 `/meta`에서 받은 현재 `metadata_revision`과 `sql_policy_revision`으로 `/query`를
호출하며, host URL은 `http://127.0.0.1:${QUERY_MAN_SCALE_PORT:-3100}`을 사용한다. 전체 반출 대신
집계 또는 indexed filter와 `LIMIT`으로 시작한다.

## Baseline과 종료/rollback

Scale 적재는 별도 volume의 business row만 바꾸며 source manifest, metadata revision algorithm,
`config/verified-queries.yaml`과 기본 fixture를 수정하지 않는다. 하지만 scale row가 추가되면
`market-voc`의 기존 5개 expected result hash는 당연히 달라진다. 이를 새 baseline으로 갱신하지 않고,
`query-man-verify`는 scale 적재 전 또는 scale volume을 초기화한 뒤에만 실행한다.

데이터를 보존한 채 container만 멈추려면 scale file pair로 `down`한다.

```bash
docker compose \
  --file compose.yaml \
  --file compose.scale.yaml \
  down --remove-orphans
```

Pilot 폐기, full rollback 또는 완전 초기화는 같은 scale project에만 `-v`를 추가한다.

```bash
docker compose \
  --file compose.yaml \
  --file compose.scale.yaml \
  down -v --remove-orphans

unset COMPOSE_FILE
unset QUERY_BOY_DB_HOST QUERY_BOY_DB_PORT QUERY_BOY_DB_NAME QUERY_BOY_DB_USER
unset QUERY_BOY_DB_PASSWORD QUERY_BOY_DB_SSLMODE QUERY_BOY_DB_CONNECT_TIMEOUT
```

기본 project에 대한 bare `docker compose down -v`를 rollback으로 사용하지 않는다. Scale-only 명령
후 기본 fixture와 verified baseline은 그대로 남고, 필요하면 평소 `compose.yaml` 절차로 별도 실행한다.
