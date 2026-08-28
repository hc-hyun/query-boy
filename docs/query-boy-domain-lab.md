# Query Boy 다중 도메인 로컬 lab

Status: Optional local fixture; production 사용 금지

Change-set: `QB-DOMAIN-LAB-20260828` (baseline `deef37a`)

이 lab은 Query Boy가 질문에 맞는 source를 먼저 고르고, 선택한 source 안에서 grain·분모·시간·통화
모호성을 안전하게 처리하는지 시험한다. 서로 전혀 다른 다섯 합성 도메인과 기존 두 source를 한
로컬 catalog에 노출한다. Full profile은 **다섯 도메인을 합쳐 fact 1,000,000건**이며 source마다
1,000,000건이 아니다.

이 문서는 로컬 disposable fixture 절차다. `compose.yaml`, 기본 두 source inventory,
`config/verified-queries.yaml`, 운영 migration이나 managed source authority를 변경하지 않는다.

## 격리 경계와 source inventory

| 항목 | Domain lab 값 |
|---|---|
| Compose project | `query-man-domain-lab` |
| PostgreSQL container | `query-man-domain-lab-postgres` |
| Query Boy container | `query-man-domain-lab-app` |
| PostgreSQL volume | `query-man-domain-lab-postgres-data` |
| PostgreSQL host port | `${QUERY_MAN_DOMAIN_POSTGRES_PORT:-55434}` |
| Query Boy host port | `${QUERY_MAN_DOMAIN_PORT:-3101}` |
| App marker | `QUERY_MAN_DOMAIN_LAB=1` |
| PostgreSQL marker | `query_boy.domain_lab=on` |
| Source directory | `/app/config/domain-lab/sources` |

Domain-lab catalog는 기존 `development-issues`, `market-voc`와 다음 다섯 source를 합쳐 정확히 일곱
개다.

| Source ID | 데이터베이스 | 읽기 전용 공개 view |
|---|---|---|
| `retail-commerce` | `retail_commerce` | `ai.order_overview`, `ai.order_lines`, `ai.customer_overview` |
| `parcel-logistics` | `parcel_logistics` | `ai.shipment_overview`, `ai.tracking_events`, `ai.hub_overview` |
| `energy-telemetry` | `energy_telemetry` | `ai.meter_readings`, `ai.outage_overview`, `ai.meter_overview` |
| `clinical-operations` | `clinical_operations` | `ai.appointment_overview`, `ai.lab_results`, `ai.patient_overview` |
| `saas-billing` | `saas_billing` | `ai.subscription_overview`, `ai.invoice_overview`, `ai.usage_daily` |

기존 두 manifest와 verified-query registry는 domain-lab directory에 byte-for-byte 복사한다. 따라서
기존 두 source의 L2 baseline은 유지되고, 신규 다섯 source는 verified query를 꾸며내지 않은 L1이다.
Overlay는 base Compose와 함께만 사용한다. `compose.scale.yaml`이나 기본/scale volume과 섞지 않는다.

모든 데이터는 seed `2026082802`, 기준 시각 `2026-08-28T00:00:00Z`의 결정적 합성 데이터다.
`clinical-operations`에는 실제 환자, PII, 진단 또는 처방이 없다.

## 데이터 규모

| 도메인 | Dimension | Fact | Full fact 합계 |
|---|---:|---:|---:|
| Retail | customers 20,000; products 500 | orders 80,000; order_lines 120,000 | 200,000 |
| Logistics | hubs 40 | shipments 60,000; tracking_events 140,000 | 200,000 |
| Energy | sites 1,000; meters 20,000 | meter_readings 180,000; outages 20,000 | 200,000 |
| Clinical | synthetic_patients 30,000; providers 200 | appointments 80,000; lab_results 120,000 | 200,000 |
| SaaS | plans 6; tenants 20,000 | subscriptions 50,000; invoices 75,000; usage_daily 75,000 | 200,000 |

Full physical row 합계는 dimension 91,746건과 fact 1,000,000건을 더한 1,091,746건이다. Pilot은
dimension 935건과 fact 10,000건, 총 10,935건이다.

| Pilot dimension | 건수 | Pilot fact | 건수 |
|---|---:|---|---:|
| Retail customers / products | 200 / 5 | orders / order_lines | 800 / 1,200 |
| Logistics hubs | 4 | shipments / tracking_events | 600 / 1,400 |
| Energy sites / meters | 10 / 200 | meter_readings / outages | 1,800 / 200 |
| Clinical patients / providers | 300 / 10 | appointments / lab_results | 800 / 1,200 |
| SaaS plans / tenants | 6 / 200 | subscriptions / invoices / usage_daily | 500 / 750 / 750 |

## 의도한 코너와 실패시킬 의미 오류

| 도메인 | 의도적으로 포함한 코너 |
|---|---|
| Retail | 4개 통화(환율 없음), 취소, 부분·전체 반품/환불, 주문 없는 고객 1명, Unicode·apostrophe 상품명 |
| Logistics | 스캔 없는 `CREATED` 운송장 1건, 같은 시각·순서 역전 스캔, 늦은 ingest, scan보다 이른 recorded 시각, 지연·분실·파손·주소·통관 예외 |
| Energy | 음수 `net_kwh` 역송전, `MISSING` null과 실제 0의 구분, reset, Texas DST 반복 01시(-300/-360), 검침 없는 계량기, 진행 중·겹치는 정전 |
| Clinical | 실제 PII가 없는 합성 코드, 취소와 노쇼, null인 pending 결과, corrected·critical 결과, 예약 없는 검사와 활동 없는 환자 |
| SaaS | trial·pause·cancel·expire, 부분결제·credit·연체·0원 VOID, 사용량 0·overage, subscription 없는 invoice/usage와 구독 없는 tenant |

반대로 분석 의미를 깨는 우연한 이상치는 성공 데이터로 인정하지 않는다. Loader는 FK와 exact count 외에도
고객 가입·상품 출시·허브 개장·사이트 가동·계량기 설치·환자 등록·tenant 획득·subscription 기간보다
이른 하위 event, 배송 완료보다 이른 `DELIVERED` scan, 지역·계절과 다른 UTC offset/local clock,
정상 범위의 critical 검사, 미정산 주문의 양수 순수취액, VOID 미수액과 invoice status/due-date 불일치를
모두 0건으로 감사한다.

## 1. 로컬 secret과 Compose 확인

Query Boy root의 git-ignored `.env`에 `.env.domain-lab.example`의 누락 항목을 병합하고 모든
`replace-with-...` 값을 로컬 값으로 바꾼다. `.env`가 아직 없을 때만 아래 첫 명령이 example을
복사한다. 파일을 commit하거나 secret을 command line에 직접 넣지 않는다.

```bash
cp --no-clobber .env.domain-lab.example .env
${EDITOR:-vi} .env

docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  config --quiet
```

Compose 2.24 이상의 `!override` 지원이 필요하다. Rendered config에는 PostgreSQL base command의
`shared_preload_libraries`, slow-query logging과 domain marker가 모두 있어야 하며 secret 값은
출력·기록하지 않는다.

## 2. 깨끗한 schema 시작과 marker 검사

```bash
docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  up -d --build --wait postgres

test "$(
  docker compose \
    --env-file .env \
    --file compose.yaml \
    --file compose.domain-lab.yaml \
    exec -T postgres \
    psql --username query_man_admin --dbname retail_commerce \
      --tuples-only --no-align \
      --command "SELECT pg_catalog.current_setting('query_boy.domain_lab', true)"
)" = "on"
```

Marker가 `on`이 아니거나 target database·table inventory가 다르면 loader를 실행하지 않는다. Init
bootstrap은 다섯 database/reader/view-owner를 만들고 각 reader를 자신의 database와 `ai` view에만
제한한다. Reader 기본값은 read-only, statement 5초, transaction 8초, lock 250ms, idle 2초,
`work_mem=8MB`, `temp_file_limit=64MB`, parallel 0, JIT off, `search_path=pg_catalog`이다.

## 3. `voc-gen` loader 연결

Loader는 database 이름을 caller 입력으로 받지 않고 고정된 다섯 target을 순회한다.

| 환경 변수 | 의미 | 기본값 또는 요구사항 |
|---|---|---|
| `QUERY_BOY_DOMAIN_DB_HOST` | PostgreSQL host | `127.0.0.1` |
| `QUERY_BOY_DOMAIN_DB_PORT` | PostgreSQL host port | `55434`; Compose override와 같아야 함 |
| `QUERY_BOY_DOMAIN_DB_USER` | 로컬 loader user | `query_man_admin` |
| `QUERY_BOY_DOMAIN_DB_PASSWORD` | loader password | 필수; 문서나 command line에 쓰지 않음 |
| `QUERY_BOY_DOMAIN_DB_SSLMODE` | 로컬 SSL mode | `disable` |
| `QUERY_BOY_DOMAIN_DB_CONNECT_TIMEOUT` | 연결 timeout(초) | `10` |

Loader 연결 값은 현재 shell에만 둔다. Password는 화면과 shell history에 남지 않게 입력하며 local
admin password와 같은 값을 사용한다.

```bash
export QUERY_BOY_DOMAIN_DB_HOST=127.0.0.1
export QUERY_BOY_DOMAIN_DB_PORT=55434
export QUERY_BOY_DOMAIN_DB_USER=query_man_admin
export QUERY_BOY_DOMAIN_DB_SSLMODE=disable
export QUERY_BOY_DOMAIN_DB_CONNECT_TIMEOUT=10
read -r -s -p 'QUERY_BOY_DOMAIN_DB_PASSWORD: ' QUERY_BOY_DOMAIN_DB_PASSWORD
echo
export QUERY_BOY_DOMAIN_DB_PASSWORD
```

Loader는 각 database transaction 안에서 exact database 이름, `query_boy.domain_lab=on`, physical
table inventory를 먼저 검사한다. `TRUNCATE`, `DELETE`, DDL은 실행하지 않으며 natural key upsert,
exact count와 FK orphan audit, `ANALYZE`까지만 수행한다.

다섯 database를 하나의 PostgreSQL transaction으로 묶을 수는 없다. 한 database commit 뒤 다음
database가 실패할 수 있지만 같은 profile 재실행은 결정적이고 idempotent하게 완료 상태로 수렴한다.
작은 profile로 이미 채운 volume에 더 작은 count를 적용하거나 pilot에서 full로 상태를 섞지 말고,
profile 전환 전에는 아래 전용 volume reset 절차를 사용한다. Generator SQL 자체를 바꾸어 natural key가
가리키는 보조 unique 값이나 시간 의미가 달라진 경우도 이전 버전 위에서 migration하지 말고 전용
volume을 reset한다. 멱등성 계약은 같은 generator version과 같은 profile의 재실행에 적용된다.

## 4. Pilot 적재

```bash
cd ../voc-gen
uv run voc-factory load-query-boy-domains \
  --profile domain_profiles/query_boy.domain_lab.pilot.json
cd ../query-boy
```

성공 report는 위 표의 exact count, 모든 orphan/integrity 값 0, 다섯 database의 `ANALYZE` 완료를 보여야
한다. Pilot으로 source selection과 작은 집계 질문을 확인한 뒤 full 전에 domain-lab volume만 지운다.

```bash
docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  down -v --remove-orphans
```

이 명령은 domain-lab project가 소유한 PostgreSQL volume과 optional diagnostic volume만 삭제한다.
기본 `query-man_postgres_data`나 scale fixture volume을 대상으로 bare `docker compose down -v`를
실행하지 않는다. 그다음 [깨끗한 schema 시작과 marker 검사](#2-깨끗한-schema-시작과-marker-검사)를
반복한다.

## 5. Full 100만 fact 적재

```bash
cd ../voc-gen
uv run voc-factory load-query-boy-domains \
  --profile domain_profiles/query_boy.domain_lab.scale_1m.json
cd ../query-boy
```

Loader report의 fact 합계가 정확히 1,000,000이고 physical 합계가 1,091,746이어야 한다. 전체 행을
Query Boy로 반출하지 말고 처음에는 `count`, 상태·지역·기간별 집계, indexed filter와 작은 `LIMIT`을
사용한다.

## 6. Physical/권한 검증

아래 count는 full profile 기준이다. Pilot이면 위 pilot 표의 값으로 읽는다.

```bash
docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  exec -T postgres \
  psql --username query_man_admin --dbname retail_commerce --set=ON_ERROR_STOP=1 <<'SQL'
SELECT
  (SELECT count(*) FROM retail.retail_customers) AS customers,
  (SELECT count(*) FROM retail.products) AS products,
  (SELECT count(*) FROM retail.orders) AS orders,
  (SELECT count(*) FROM retail.order_lines) AS order_lines;
\connect parcel_logistics
SELECT
  (SELECT count(*) FROM logistics.hubs) AS hubs,
  (SELECT count(*) FROM logistics.shipments) AS shipments,
  (SELECT count(*) FROM logistics.tracking_events) AS tracking_events;
\connect energy_telemetry
SELECT
  (SELECT count(*) FROM energy.sites) AS sites,
  (SELECT count(*) FROM energy.meters) AS meters,
  (SELECT count(*) FROM energy.meter_readings) AS meter_readings,
  (SELECT count(*) FROM energy.outages) AS outages;
\connect clinical_operations
SELECT
  (SELECT count(*) FROM clinical.synthetic_patients) AS synthetic_patients,
  (SELECT count(*) FROM clinical.providers) AS providers,
  (SELECT count(*) FROM clinical.appointments) AS appointments,
  (SELECT count(*) FROM clinical.lab_results) AS lab_results;
\connect saas_billing
SELECT
  (SELECT count(*) FROM billing.plans) AS plans,
  (SELECT count(*) FROM billing.tenants) AS tenants,
  (SELECT count(*) FROM billing.subscriptions) AS subscriptions,
  (SELECT count(*) FROM billing.invoices) AS invoices,
  (SELECT count(*) FROM billing.usage_daily) AS usage_daily;
SQL
```

각 database에서 다음 type audit가 0행이어야 한다. Public result column은 Query Boy가 lossless하게
지원하는 `int2`, `int4`, `int8`, `text`, `date`, `timestamptz`, `numeric`만 쓴다.

```sql
SELECT table_name, column_name, udt_name
FROM information_schema.columns
WHERE table_schema = 'ai'
  AND udt_name NOT IN ('int2', 'int4', 'int8', 'text', 'date', 'timestamptz', 'numeric');
```

각 reader로 자신의 database에 연결했을 때 `ai` view SELECT는 성공하고 private schema/table SELECT,
TEMP, 다른 여섯 database CONNECT는 실패해야 한다. 오류 메시지나 credential을 외부 결과에 복사하지
않는다.

## 7. Query Boy와 source-selection 검증

Full 또는 pilot load 뒤 app을 시작한다.

```bash
docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  up -d --build --wait query-man

test "$(
  docker compose \
    --env-file .env \
    --file compose.yaml \
    --file compose.domain-lab.yaml \
    exec -T query-man printenv QUERY_MAN_DOMAIN_LAB
)" = "1"

export QUERY_MAN_CODEX_MCP_TOKEN="$(
  sed -n 's/^QUERY_MAN_CODEX_MCP_TOKEN=//p' .env
)"
curl -sS "http://127.0.0.1:${QUERY_MAN_DOMAIN_PORT:-3101}/sources" \
  -H "Authorization: Bearer $QUERY_MAN_CODEX_MCP_TOKEN"
```

응답은 정확히 일곱 source를 보여야 한다. 기존 `verify-container.sh`는 기본 two-source inventory를
검증하므로 domain-lab app에 그대로 사용하지 않는다.

Source 선택 검증 순서는 다음과 같다.

1. AI에게 인증된 `/sources` 또는 MCP `list_sources`의 `source_id`, `name`, `description`만 준다.
2. `config/domain-lab/source-selection-cases.json` 질문마다 정확히 한 source가 명확하면 그 ID를 고르고,
   여러 source가 타당하거나 cross-source 결합이 필요하면 `needs_clarification`을 고르게 한다.
3. Source 선택 전 여러 `/meta`를 탐색하거나 여러 database를 federation하지 않는다.
4. 선택한 하나의 source로만 `/meta` 또는 MCP `get_context`를 호출한다.
5. `answerability.status`가 `needs_clarification` 또는 `unsupported`면 `/query`를 호출하지 않는다.
6. `best_effort`인 경우에만 같은 응답의 metadata/sql-policy revision으로 guarded query를 실행한다.

특히 `invoice 결제 실패 후 회수율`은 catalog 단계에서 `saas-billing`을 선택한 뒤 context 단계에서
`unsupported`가 되어야 한다. Payment attempt/retry/recovery history가 없으므로 회수율 SQL을 만들면
실패다. `배송 지연이 환불에 미친 영향`처럼 Logistics와 Retail의 cross-source join이 필요한 질문도
clarification/unsupported로 남겨야 한다.

Text-to-SQL skill과 corpus의 구조 gate는 다음처럼 실행한다.

```bash
uv run pytest tests/test_text_to_sql_skill.py -q
```

독립 blind catalog-only 평가에서 최초 corpus는 24/25였다. 실패한 `취소 후 재개율` 문구를 domain이
드러나지 않는 `취소 후 다시 이용한 비율`로 고친 뒤 재평가가 clinical/SaaS clarification 기대값과
일치해 최종 25/25였다. 이 숫자는 source label/description만 본 **catalog-only blind selection
evaluation**이며 실제 PostgreSQL, metadata, MCP 또는 guarded-query end-to-end 성공률이 아니다.
End-to-end는 위 marker, count, privilege, `/sources`, `get_context.answerability`, revision-bound query를
별도로 통과해야 한다.

## 종료와 reset

데이터를 보존하고 container만 멈춘다.

```bash
docker compose \
  --env-file .env \
  --file compose.yaml \
  --file compose.domain-lab.yaml \
  down --remove-orphans
```

Pilot 폐기, full rollback 또는 완전 초기화는 같은 명령에만 `-v`를 추가한다. 삭제되는 것은
domain-lab project가 소유한 PostgreSQL 및 optional diagnostic volume이며 복구할 수 없다. 기본/scale
project volume에는 영향이 없다. 마지막에는 loader connection을 shell에서 지운다.

```bash
unset QUERY_BOY_DOMAIN_DB_HOST QUERY_BOY_DOMAIN_DB_PORT QUERY_BOY_DOMAIN_DB_USER
unset QUERY_BOY_DOMAIN_DB_PASSWORD QUERY_BOY_DOMAIN_DB_SSLMODE
unset QUERY_BOY_DOMAIN_DB_CONNECT_TIMEOUT QUERY_MAN_CODEX_MCP_TOKEN
```
