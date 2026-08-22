# Source Onboarding

## Boundary

신규 PostgreSQL source 추가는 runtime 코드 변경 없이 끝나야 한다. 다음 두 종류의
metadata를 분리한다.

자동 수집:

- reader가 `SELECT`할 수 있는 relation과 column
- data type, relation kind와 database comment
- 권한 범위 안의 primary key, foreign key와 단순 non-partial index key
- view definition의 revision hash 재료

사람이 선언:

- source 설명과 credential 환경 변수 이름
- 허용 schema와 relation kind, budget profile
- relation의 role, grain과 대표 시간 column
- 물리 이름만으로 알 수 없는 업무 alias
- 검증된 join key, cardinality와 fanout guidance
- source별 business predicate, value hint와 measure aggregation
- 지원 불가능하거나 정의 확인이 필요한 질문 규칙
- 여러 grain을 각각 집계한 뒤 결합해야 하는 검증된 composition

Table 전체를 Python 객체로 다시 모델링하거나 `COMMENT` 문장에서 join 규칙을
추출하지 않는다.

Source manifest는 임의 host로 연결할 수 있는 운영 설정이므로 application code와 같은
trust boundary에서 review·publish한다. 낮은 권한의 self-service 입력으로 직접 받지 않는다.

## Steps

1. 조회 목적에 맞는 `ai` view를 만든다. 한 view의 grain은 하나로 고정한다.
2. 별도 LOGIN reader에 해당 view의 `USAGE`와 `SELECT`만 부여한다.
3. Reader에 read-only, timeout, temp resource와 connection limit을 설정한다.
4. `config/sources/<source-id>.yaml` manifest를 추가한다.
5. `password_env`가 가리키는 secret을 실행 환경에 추가한다.
6. Metadata API process를 재시작해 manifest를 다시 읽는다.
7. `uv run ruff check .`, `uv run mypy src`, `uv run pytest`와 실제 golden question으로 relation 선택을 검증한다.

Production caller에게 source를 공개할 때는 access-policy manifest의
`allowed_sources`에도 source ID를 명시하고, token 값은 manifest가 참조하는 환경
변수에만 저장한다. 예시는
[`config/access-policies.example.yaml`](../config/access-policies.example.yaml)을 따른다.

최소 manifest는 semantic overlay 없이도 동작한다.

```yaml
version: 1
source_id: example-source
name: 예제 Source
description: 예제 분석 데이터
connection:
  host: 127.0.0.1
  port: 5432
  database: example_database
  user: example_reader
  password_env: EXAMPLE_SOURCE_READER_PASSWORD
  ssl: false
allowed_schemas: [ai]
allowed_relation_kinds: [view]
budget_profile: interactive
```

이 상태는 L0 best-effort 검색이다. 한국어 질문, grain과 비표준 join이 필요할 때만
`semantic_overlay`를 추가한다.

```yaml
semantic_overlay:
  default_relation: ai.case_overview
  relations:
    - relation: ai.case_overview
      role: event
      aliases: [고객 접수, 시장 불량]
      grain:
        name: customer_case
        description: 고객 접수 1건
        key_columns: [case_id]
      default_time_column: received_at
      use_for: [월별 접수 건수, 미해결 접수]
  joins: []
```

## Publish Checks

Registry와 metadata refresh는 다음 조건을 만족하지 않으면 source를 publish하지 않는다.

- 중복되지 않은 opaque `source_id`
- 존재하는 budget profile과 secret 환경 변수
- overlay relation이 allowed schema 안에 있음
- Reader 세션의 database, session user와 read-only 상태가 profile과 일치함
- Grain key, 대표 시간 column과 column alias 대상이 실제 catalog에 존재함
- 승인된 join 양쪽 column이 존재하고 data type이 같음
- Relation/column 수와 metadata response가 budget 상한 안에 있음

Catalog refresh가 일시 실패하면 제한된 stale 기간 동안 마지막 정상 revision을
`stale` 상태와 함께 반환하고 backoff 후 다시 시도한다. 정상 snapshot이 한 번도
없거나 stale 상한을 넘었거나 overlay가 현재 schema와 맞지 않으면 `/meta`는
`503 METADATA_UNAVAILABLE`을 반환한다. 권한 회수나 schema drift는 stale fallback을
사용하지 않는다.

40개를 넘는 wide relation은 질문과 직접 관련된 column, grain/time key, measure,
business predicate와 approved join key를 우선 반환한다. `columns_truncated=true`는 전체
catalog가 사라졌다는 뜻이 아니라 question context에서 일부 column을 생략했다는 뜻이다.
이는 SQL column authorization 경계가 아니므로 민감 column은 curated view 자체에서
제거해야 한다.

Production에서는 `./scripts/apply-db.sh`가 생성하는 `control` schema를 사용하고,
`query_man_control_writer` role을 상속한 전용 LOGIN의 DSN을
`QUERY_MAN_CONTROL_DSN`에 설정한다. 정상 refresh는 immutable snapshot과 active pointer를
원자적으로 publish한다. Rollback한 source는 pin되므로 이후 refresh가 active revision을
덮지 않으며, 검증 후 automatic publish를 명시적으로 resume해야 한다. Control-plane
DSN이나 snapshot payload는 metadata/MCP 응답에 포함되지 않는다.

## Security Checks

- Request에는 `source_id`, `question`, `max_objects` 외 필드를 허용하지 않는다.
- Loopback 밖에 HTTP를 bind할 때는 bearer token 없이는 process가 시작되지 않는다.
- Metadata catalog는 allowed schema, relation kind, schema/table/column 권한을 모두 확인한다.
- Foreign key의 양쪽 relation/column이 모두 허용된 경우에만 공개하고 physical key를
  approved semantic join으로 자동 승격하지 않는다.
- System schema는 manifest 단계에서 거부하고 base/temp schema와 `pg_stat_statements`는 API에 노출하지 않는다.
- `password_env`는 `<SOURCE_ID>_READER_PASSWORD` 형식의 source-scoped secret만 허용한다.
- Reader search path는 `pg_catalog, ai` 순서로 둔다.
- DB comment는 instruction이 아니라 길이가 제한된 description data로 취급한다.
- Production connection은 TLS certificate 검증을 사용한다.
