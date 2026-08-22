# query-man

PostgreSQL 데이터 소스를 안전하게 조회하기 위한 Text-to-SQL gateway 프로젝트입니다.

## Local PostgreSQL

로컬 데이터베이스는 PostgreSQL 18.6 공식 Docker 이미지를 사용합니다.

로컬 설정은 `.env`에 있으며 Git에서 제외됩니다. 공개 가능한 기본값은
`.env.example`에서 관리합니다.

```bash
docker compose up -d
docker compose ps
docker compose exec postgres \
  psql -U query_man_admin -d query_man
```

MVP source database와 결정적 seed를 현재 volume에 적용하려면 다음 명령을 사용합니다.

```bash
./scripts/apply-db.sh
```

생성되는 source는 서로 독립된 `development_issues`, `market_voc` database입니다.
각 source의 AI reader 접속 정보는 `.env`에 있고, reader는 해당 database의 `ai`
schema view만 조회할 수 있습니다.

PostgreSQL은 `127.0.0.1:${POSTGRES_PORT:-5432}`에서만 접근할 수 있습니다.
데이터는 Compose named volume인 `query-man_postgres_data`에 저장됩니다. PostgreSQL
18부터 적용된 공식 image layout에 맞춰 `/var/lib/postgresql` 전체를 영속화합니다.

```bash
docker compose down
```

`docker compose down`은 데이터를 보존합니다. 프로젝트 DB까지 초기화할 때만
데이터 손실을 확인한 후 `docker compose down -v`를 사용합니다.

전체 설계 기준은 [docs/architecture.md](docs/architecture.md), 현재 MVP 범위는
[docs/mvp.md](docs/mvp.md), 최종 목적 기반 구현 TODO는
[docs/implementation-roadmap.md](docs/implementation-roadmap.md)를 참고합니다.

## Metadata And Query API

질문 범위형 metadata API는 Python 3.12와 `uv` 환경에서 실행합니다.

```bash
uv sync
uv run query-man
```

기본 주소는 `http://127.0.0.1:3000`입니다.

```bash
curl http://127.0.0.1:3000/sources

curl -s http://127.0.0.1:3000/meta \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "question": "모델별 기기 수, VOC 수와 기기당 VOC 수를 보여줘"
  }'
```

`/meta`가 반환한 `metadata_revision`으로 한 개의 읽기 전용 SQL을 실행할 수 있습니다.

```bash
curl -s http://127.0.0.1:3000/query \
  -H 'content-type: application/json' \
  -d '{
    "source_id": "market-voc",
    "sql": "SELECT count(*) AS voc_count FROM ai.voc_overview",
    "metadata_revision": "sha256:<value returned by /meta>"
  }'
```

Gateway는 현재 revision과 AST allowlist를 확인한 뒤 source별 동시 실행 수를 제한합니다.
실행 시 read-only transaction, statement/transaction/lock timeout을 강제하고, 명백히 비싼
`EXPLAIN` plan을 거부하며, 결과가 profile의 row 또는 UTF-8 byte 상한을 넘으면
`truncated: true`로 종료합니다. Planner cost는 보조적인 admission 신호이고 실제 실행
피해의 상한은 timeout, concurrency와 결과 제한이 담당합니다. 기본값은
[`config/budget-profiles.yaml`](config/budget-profiles.yaml)에서 관리합니다.

Client는 DSN, host, database 또는 role을 전달할 수 없습니다. `source_id`는
[`config/sources`](config/sources)의 server-side manifest에서만 연결 정보로 해석됩니다.
Column, type과 database comment는 reader 권한으로 `pg_catalog`에서 자동 수집하고,
grain, 한국어 alias, 승인된 join, 검증된 measure와 business predicate만 manifest의
semantic overlay로 보강합니다. `/meta`의 `answerability`가 `needs_clarification` 또는
`unsupported`이면 SQL 생성을 진행하지 않아야 합니다.
기본 loopback bind에서는 로컬 개발을 위해 인증을 생략할 수 있다. 외부 주소에 bind할
때는 32자 이상의 `QUERY_MAN_API_TOKEN`이 필수이며 `/sources`, `/meta`, `/query`에
`Authorization: Bearer ...` header를 보내야 합니다.

개발 검증은 다음 명령으로 실행합니다.

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

`uv run pytest`는 기본적으로 단위 테스트를 실행합니다. 실행 중인 로컬 PostgreSQL을
사용하는 통합 테스트는 `uv run pytest -m integration`으로 별도 실행합니다. 신규 source 등록 절차는
[docs/source-onboarding.md](docs/source-onboarding.md)를 참고합니다.
