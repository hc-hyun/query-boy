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
[docs/mvp.md](docs/mvp.md)를 참고합니다.
