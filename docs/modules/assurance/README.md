# Assurance Module

Status: Logical boundary; physical package split pending; `LAUNCH-01-A` repository acceptance complete;
protected execution separately gated

## 목적

Assurance는 Source Catalog, Metadata와 Guarded Query가 함께 지켜야 하는 품질·안전 기준을
실행 가능한 case로 검증한다. Metadata retrieval quality와 verified SQL의 revision, relation,
column, row count 및 result hash를 확인하고, offline CLI와 repository acceptance를 조립한다.

Assurance는 runtime 요청 처리나 protected environment 배포를 대신하지 않는다. 현재 launch
authority는 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 static non-RLS
two-source profile이며, 아래 항목과 immutable evidence는 이 baseline이 통과한 acceptance를 기록한다.

## 소유 책임

- Versioned metadata quality case와 accuracy, answerability, context-byte gate
- Versioned verified-query artifact, exact revision/relation/result expectation과 source별 membership
- Guarded Query를 거치는 live verification 순서와 bounded mismatch reporting
- Ordered columns와 canonical rows를 묶는 result hash 및 별도 exact row-count 비교
- `query-man-evaluate`와 `query-man-verify`의 offline composition 및 external command semantics
- Cross-module integration, HTTP/MCP parity, container와 database-corner acceptance 조립
- Repository gate와 immutable verification evidence의 범위·provenance 관리
- Managed/Control recovery와 onboarding acceptance fixture의 역사 보존

Metadata의 L0/L1/L2 publish 판정 구현은 [Metadata](../metadata/README.md)가 소유한다. Assurance는
그 판정에 필요한 verified revision membership과 실행 증거를 제공할 뿐 production 판정 코드를
복제하지 않는다.

## 소유하지 않는 책임

- HTTP/MCP runtime request 처리, authentication과 authorization
- Physical catalog/context algorithm, metadata revision이나 L0/L1/L2 판정 구현
- SQL AST policy, reader connection verifier, query execution이나 result scalar encoding
- Control DB table, verified persistence transaction과 active source pointer
- Source manifest, runtime topology, image build 또는 protected cutover 실행
- Expected result의 자동 생성·승인이나 business data 전체 정확성 보증
- Service SLO, provider billing, production metric과 alert 집계

RLS attestation, broader lossless encoding, cost attribution과 trace 설계는 첫 launch 범위 밖의
parked research다. 해당 연구 문서가 있다는 사실만으로 Assurance acceptance나 provider 의미가
추가되지 않는다.

## 현재 코드 위치

- [`quality.py`](../../../src/query_man/quality.py): quality case, gate와 report core
- [`verified.py`](../../../src/query_man/verified.py): verified DTO, registry, comparison과 hash core
- [`assurance_cli.py`](../../../src/query_man/assurance_cli.py): 두 offline command의 유일한 concrete
  composition root
- [`quality-evaluation.yaml`](../../../config/quality-evaluation.yaml): versioned retrieval quality cases
- [`verified-queries.yaml`](../../../config/verified-queries.yaml): static launch의 two-source, 9-query dataset
- [`security-evaluation.yaml`](../../../config/security-evaluation.yaml): parser/query safety corpus
- [`ci.yml`](../../../.github/workflows/ci.yml)과
  [`mcp-soak.yml`](../../../.github/workflows/mcp-soak.yml): repository gate orchestration
- [`verify-container.sh`](../../../scripts/verify-container.sh): Runtime container와 Delivery surface acceptance
- [`test_quality.py`](../../../tests/test_quality.py),
  [`test_verified.py`](../../../tests/test_verified.py),
  [`test_assurance_cli.py`](../../../tests/test_assurance_cli.py): Assurance focused tests
- [`test_reader_policy.py`](../../../tests/test_reader_policy.py),
  [`test_result_encoding.py`](../../../tests/test_result_encoding.py),
  [`test_integration.py`](../../../tests/test_integration.py),
  [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py): launch policy와
  cross-module acceptance
- [`verification index`](../../verification/README.md): 실행 시점별 immutable evidence 색인

`docker/postgres/init/00-bootstrap.sql`, `01-source-bootstrap.sh`와 source fixture SQL은 acceptance
infrastructure이며 production source schema authority가 아니다. Control migration과 persistence는
Control Plane 소유다.

## 제공 인터페이스와 소유 경계

이 절은 서로 다른 변경 범주를 구분한다. Python shape만 official module interface이고, artifact
schema, hash 재료, CLI, acceptance와 evidence를 모두 interface라고 부르지 않는다.

### Official module interfaces

- `ExpectedResult`와 `VerifiedQuery`: Control Plane이 verified artifact를 저장·검증할 때 소비하는
  immutable DTO
- `VerifiedQueryRegistry.revision_map()`: Metadata와 Runtime composition이 소비하는
  `source_id -> frozenset[metadata revision]` membership
- `create_result_hash(columns, rows) -> str`: Control Plane과 acceptance가 소비하는 hash capability

위 symbol의 Python shape와 호출 단위 오류 의미가 module interface다. Hash payload와 canonical
encoding의 의미는 별도 policy/compatibility identity이며, DTO를 저장하는 Control DB row는 별도
persisted format이다.

### Verified-query artifact schema

Static filesystem artifact는 strict version 1이며 한 file 안의 globally unique `query_id`와 다음
필드를 고정한다.

```text
source_id, question, deterministic read-only SQL
exact metadata_revision and referenced relation set
exact ordered columns, row_count and canonical result_hash
```

Managed Control DB의 immutable identity `(source_id, query_id, metadata_revision)`와 filesystem의
global `query_id` 규칙을 합치지 않는다. Static mode는 filesystem dataset만 읽고, managed mode는
Control DB projection만 사용한다. Import, fallback이나 두 authority의 merge는 없다.

### Result hash policy identity

```text
{"columns": ordered columns, "rows": canonical encoded rows}
-> compact UTF-8 JSON, ensure_ascii=false, sort_keys=true
-> SHA-256 with "sha256:" prefix
```

Rows는 Guarded Query가 성공적으로 반환한 canonical values다. Verification은 published metadata
revision, validated relation set, Guarded Query 실행, `truncated=false`, exact columns, row count와
hash 순서로 비교한다. Expected output을 live result에서 자동 갱신하지 않는다.

[Verified-query baseline](../../verified-queries.md)의 9개 metadata revision, columns, row counts와
result hashes는 SQL policy v3 전환에서도 그대로 유지한다. Canonical bytes나 metadata revision
algorithm을 바꾸는 결정이 아니며, 9개 전부를 새 policy token으로 다시 실행하는 것이 acceptance다.

### Offline CLI surface and composition boundary

`query-man-evaluate`와 `query-man-verify`의 console-script target은 각각
`query_man.assurance_cli:evaluate_main`과 `query_man.assurance_cli:verify_main`이다. Command 이름,
`--root`와 기본값, JSON stdout 및 exit 의미는 existing external CLI surface다.

`assurance_cli.py`만 offline acceptance에 필요한 concrete Source Registry, Catalog, Metadata와 Query
adapter를 조립한다. 두 command는 지정한 root의 static filesystem configuration만 읽으며 Runtime
authority selector나 Control DB를 사용하지 않는다. Static RLS manifest는 registry load에서 먼저
거부되고 CLI는 tenant ID를 추가하지 않는다. Verify의 accepted SQL은 `QueryService`를 통과한다.
Production server 조립이나 Control candidate staging을 이 entrypoint로 옮기지 않는다.

### Current `LAUNCH-01-A` acceptance

| 범위 | 현재 필요한 acceptance |
|---|---|
| Static dataset | `development-issues`, `market-voc`와 9개 verified query가 SQL policy v3에서 기존 metadata revision/result hash 그대로 통과 |
| Reader compatibility | PostgreSQL 18 + server/client UTF-8 positive, PG17/19·SQL_ASCII·non-UTF8 client/codec negative; no-SQL, pre-BEGIN, mismatch discard와 no-stale |
| Final result | OID `20, 21, 23, 25, 1082, 1184, 1700` 각각의 nonempty/zero-row positive; bool, JSON, bytea, float, array, record와 그 밖의 final OID는 first fetch 전 negative; base-OID로 평탄화되는 scalar domain은 bootstrap/offline Catalog publication 전 negative이고 managed default 보존도 확인 |
| RLS | Bootstrap manifest는 `RegistryConfigurationError`, injected registry는 composition 실패, managed publish/rotate는 `400 SOURCE_VALIDATION_FAILED`, cold record는 `RUNTIME_VALIDATION_REJECTED`, direct QueryService/executor 우회는 details 없는 `503 QUERY_UNAVAILABLE`; serving success case 없음 |
| External parity | Unsupported result와 RLS quarantine가 details 없는 `503 QUERY_UNAVAILABLE`로 HTTP/MCP에서 동일하고 sensitive driver/source detail을 노출하지 않음 |
| Artifact/container | Compose health가 exact `{"status":"ready"}`, upstream image tag+digest pin과 application VCS revision label을 검증 |

SQL policy v3 identity는 ADR 0025의 exact seven-OID와 reader compatibility material이다. 기존 policy
token 거부, duplicate-column 우선순위, no-fetch/rollback/pool recovery도 provider test와 cross-module
acceptance를 함께 통과해야 한다.

Assurance `query-man-evaluate`와 `query-man-verify`는 static-launch Catalog guard를 명시적으로
조립한다. Domain `type_kind`는 SQL policy material, metadata snapshot/revision이나 expected result
hash에 넣지 않으며 v3 digest `sha256:2e94db36095f11f2e9cc4e804666598f79a2ee956002ffa60dbe26bc6ee81388`을
보존한다.

### Evidence and historical acceptance

[`static first-launch acceptance`](../../verification/2026-08-26-static-first-launch.md)가
`LAUNCH-01-A` 구현 commit의 local·CI 결과를 기록한다.
[`docs/verification`](../../verification/README.md)의 각 문서는 당시 commit, fixture와 command 범위만
증명하는 immutable evidence다. 과거 record를 현재 의미에 맞춰 수정·삭제하지 않고 정정은 새
provenance record로 append한다. `Complete`도 이후 commit이나 다른 환경을 자동으로 증명하지 않는다.

Managed onboarding, CTRL-08 usage projection, CTRL-09 Control recovery, multi-replica soak와 RLS drift
finding의 artifact는 역사적 acceptance/finding으로 보존한다. 이들은 static first launch에서
Control Plane이나 multi-replica serving을 활성화하지 않으며, 현재 RLS success evidence도 아니다.

Repository acceptance가 완료돼도 [LAUNCH-02](../../development-todo.md)의 protected inventory,
TLS/secrets/backups, target access, route, stop condition과 change-record 실행은 증명하지 않는다.
Protected action은 [operations runbook](../../operations.md)에 따라 별도 승인을 받은 후에만 실행하고
그때 새 evidence를 append한다.

### Metadata quality evaluation criteria

- Expected relation은 순서를 포함한 exact tuple로 비교한다.
- Optional answerability status는 exact value로 비교한다.
- Context bytes는 compact UTF-8 JSON response 전체 크기다.
- Relation accuracy, answerability recall과 maximum context bytes가 versioned gate를 만족해야 한다.
- 실패는 non-zero CLI exit와 bounded report이며 success로 출력하지 않는다.

이 결과는 retrieval regression evidence이지 production query correctness 전체나 latency SLO가 아니다.

## 소비 인터페이스와 전제

- [Source Catalog](../source-catalog/README.md)의 immutable `SourceReader`와 reader compatibility verifier
- [Metadata](../metadata/README.md)의 published snapshot/revision, context와 quality-level semantics
- [Guarded Query](../guarded-query/README.md)의 SQL policy descriptor, guarded execution, final-OID gate와
  canonical result encoding

Assurance offline CLI만 concrete adapters를 조립한다. Assurance core는 Delivery transport나 Control DB
private implementation을 통해 validation, authorization 또는 Guarded Query safety path를 우회하지 않는다.

## 불변조건

- Verified artifact는 exact metadata revision과 exact relation set에 묶인다.
- Truncated result, changed column order, row count 또는 hash는 verification success가 아니다.
- Existing expected output, immutable record와 evidence를 자동 수정·삭제하지 않는다.
- Quality/verified config의 unknown version/field/source와 duplicate ID를 fail-closed한다.
- Static 9-query dataset과 managed verified projection을 merge하거나 서로 fallback하지 않는다.
- Verification SQL은 현재 SQL policy, budget, reader compatibility와 final-OID gate를 그대로 통과한다.
- RLS source와 unsupported final OID에는 successful verification path가 없다.
- Question, SQL, expected business value와 driver/database detail을 일반 runtime log에 노출하지 않는다.
- Concrete acceptance composition은 `assurance_cli.py`, Control candidate staging과 Runtime production
  composition의 소유 경계를 침범하지 않는다.
- Repository acceptance를 protected deployment evidence로 표현하지 않는다.

## 모듈 내부 변경

다음은 interface, artifact/CLI format, hash identity와 pass/fail 의미를 보존할 때 독립적으로 바꿀 수 있다.

- Report formatting과 CLI orchestration 정리
- 같은 comparison을 만드는 loop/helper 개선
- 동일 membership을 만드는 registry lookup/data structure 개선
- Hash input과 serialization을 바꾸지 않는 hashing helper 정리
- Acceptance test 내부 fixture 정리와 중복 제거

## 사용자 승인이 필요한 경계 변경

다음은 각 실제 범주를 명시해 별도 승인받는다. 목록 전체를 module interface 변경이라고 부르지 않는다.

- `ExpectedResult`, `VerifiedQuery`, membership/hash capability의 Python shape나 오류 의미 변경
- Quality/verified configuration schema, version, default와 managed persisted identity 변경
- Expected relation, answerability, context-byte, revision, truncation, column/row/hash matching 의미 변경
- Result hash payload, canonical JSON, scalar encoding, prefix 또는 SQL policy identity 변경
- Static/managed authority의 mutual exclusion, filesystem non-read/fallback 의미 변경
- L0/L1/L2 criteria, acceptance case/threshold 또는 verified membership 변경
- CLI command, argument, JSON output나 success/failure exit 의미 변경
- RLS serving, broader final OID, managed launch, multi-replica topology나 protected procedure 추가

승인안은 provider/consumer와 external/persisted/policy/operational 영향을 분리하고 compatibility,
migration, rollback, security와 검증 계획을 함께 제시한다. Protected environment의 실제 실행은
repository 의미 승인과 별개의 operational authorization이다.

## 검증

Assurance focused gate:

```text
uv run pytest tests/test_quality.py tests/test_verified.py tests/test_assurance_cli.py \
  tests/test_reader_policy.py tests/test_result_encoding.py
```

Current launch cross-module gate:

```text
uv run pytest tests/test_registry.py tests/test_catalog.py tests/test_query.py \
  tests/test_http.py tests/test_mcp.py tests/test_managed_mode.py
uv run pytest -m integration tests/test_integration.py tests/test_source_database_corners.py
docker compose config --quiet
./scripts/verify-container.sh
uv run query-man-evaluate
uv run query-man-verify
```

이는 실행해야 할 범위이며 이 문서 자체가 통과 evidence는 아니다. 완료 전 root `AGENTS.md`의
`ruff`, `mypy`, full pytest와 DB integration gate를 실행하고, 실제 결과는 새 verification record에
commit·fixture·command 범위와 함께 남긴다.

## 집중해서 읽을 범위

Assurance 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md), [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
2. 변경 대상 quality/verified core, offline CLI, versioned config와 focused tests
3. Metadata revision/quality interface와 Guarded Query SQL policy, final-OID/hash identity
4. Launch acceptance를 바꾸는 경우 Runtime container/Delivery external surface와 직접 관련된 tests
5. Evidence를 추가할 때 [verification index](../../verification/README.md)와 대상 command의 실제 output
6. Managed recovery fixture를 바꿀 때만 Control Plane persisted rules와 Runtime lifecycle

Provider implementation 내부, protected environment나 parked RLS/encoding/COST/TRACE research는 현재
변경이 그 경계를 실제로 건드리지 않는 한 읽을 필요가 없다.
