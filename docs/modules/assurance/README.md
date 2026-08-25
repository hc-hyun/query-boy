# Assurance Module

Status: Logical boundary; physical package split pending

## 목적

Assurance는 “metadata가 실제 질문에 유용한가”와 “검증된 SQL 결과가 그대로인가”를 versioned
case로 확인한다. 쉽게 말하면 runtime 요청을 처리하는 module이 아니라 Source/Metadata/Query
계약이 함께 작동한다는 실행 가능한 품질 증거를 소유한다.

현재 L0/L1/L2 publish 판정 구현은 publish lifecycle과 함께
[Metadata](../metadata/README.md)가 소유한다. Assurance는 그 판정에 필요한 verified revision
membership과 offline 품질 증거를 제공하고 동일 기준을 회귀 검증한다.

## 소유 책임

- Versioned metadata retrieval quality case와 accuracy/answerability/context-byte gate
- Versioned verified query, exact metadata revision/relation/result expectation schema
- Verified query registry와 source별 verified revision membership
- Guarded Query를 통한 live verification 순서와 mismatch reporting
- Ordered columns와 canonical rows를 묶는 verified result hash와 별도 exact row-count 비교
- `query-man-evaluate`와 `query-man-verify` command의 exit/result contract
- Bootstrap/acceptance quality 및 verified configuration
- CTRL-08 resource/gateway projection의 migration, state, cutoff, redaction과 기존 surface 회귀 증거
- CTRL-09 isolated cross-service Control archive, key/LOGIN, logical retention, zero-bootstrap와
  multi-replica recovery fixture acceptance

## 소유하지 않는 책임

- 일반 HTTP/MCP runtime request 처리와 authorization
- Physical catalog/context algorithm, metadata revision과 L0/L1/L2 publish 판정 자체
- SQL AST policy, query execution이나 result scalar encoding
- Control DB table, verified persistence transaction과 active pointer
- Expected result의 자동 생성·승인 또는 business data 전체 정확성 보증
- Service latency/SLO, provider billing과 production metric 집계

## 현재 코드 위치

- [`quality.py`](../../../src/query_man/quality.py): `QualityEvaluation`, cases, gates와 report core
- [`verified.py`](../../../src/query_man/verified.py): `VerifiedQuery`, `ExpectedResult`, registry,
  verification과 `create_result_hash` core
- [`assurance_cli.py`](../../../src/query_man/assurance_cli.py): `query-man-evaluate`와
  `query-man-verify`의 유일한 offline concrete composition root와 entrypoint
- [`quality-evaluation.yaml`](../../../config/quality-evaluation.yaml): versioned retrieval quality cases
- [`verified-queries.yaml`](../../../config/verified-queries.yaml): bootstrap-only verified contracts;
  managed authority가 읽거나 병합하지 않음
- [`security-evaluation.yaml`](../../../config/security-evaluation.yaml): parser/query safety
  allow/deny regression corpus
- `config/onboarding/*-verified-query.yaml`: Assurance-owned verified expectations. 같은 directory의
  base/`*-l2.yaml` manifest는 Source Catalog/Control staging input이다.
- [`ci.yml`](../../../.github/workflows/ci.yml),
  [`mcp-soak.yml`](../../../.github/workflows/mcp-soak.yml): repository gate와 scheduled/manual
  execution evidence
- [`apply-db.sh`](../../../scripts/apply-db.sh): Source fixture와 Control Plane migration을
  조립하는 Assurance-owned shared transition script
- `docker/postgres/init/00-bootstrap.sql`, `01-source-bootstrap.sh`와 source fixture SQL
  `10`~`90`: production authority가 아닌 acceptance infrastructure.
  `05-control-plane.sh`와 `control-migrations/`는 Control Plane 소유라 이 범위에 포함하지 않음
- Focused tests: [`test_quality.py`](../../../tests/test_quality.py),
  [`test_verified.py`](../../../tests/test_verified.py),
  [`test_assurance_cli.py`](../../../tests/test_assurance_cli.py),
  [`test_quality_level.py`](../../../tests/test_quality_level.py),
  [`test_result_encoding.py`](../../../tests/test_result_encoding.py),
  [`test_control_recovery.py`](../../../tests/test_control_recovery.py)

[`quality_level.py`](../../../src/query_man/quality_level.py)는 Metadata owner이고 Assurance가 검증하는
cross-module 계약이다. `verified.py`의 DTO/hash는 Control Plane이 직접 소비하는 shared contract이며
Delivery는 이를 import하지 않는다. CLI 내부 정리라는 이유로 shape나 hash 의미를 바꾸지 않는다.

`assurance_cli.py`만 offline acceptance에 한정된 bounded composition root다. Concrete
`SourceRegistry`, Metadata, Catalog와 Query adapter를 생성할 수 있지만 registry local/application
reference는 `SourceReader`로 좁힌다. `quality.py`와 `verified.py` core는 concrete adapter를 조립하지
않는다. Production HTTP/MCP runtime wiring이나 domain policy를 Assurance로 옮기지 않는다.

## 제공 계약

### Verified query contract

Bootstrap filesystem의 strict version 1 contract는 한 file 안에서 globally unique한 `query_id`와
다음 내용을 고정한다.

```text
unique query_id
source_id and human question
deterministic read-only SQL
exact metadata_revision
exact referenced relation set
exact ordered columns
exact row_count
canonical result_hash
```

Managed Control DB의 immutable contract identity는
`(source_id, query_id, metadata_revision)`다. 따라서 bootstrap file의 `query_id` global uniqueness와
managed persistence의 composite identity를 서로 같은 계약으로 해석하지 않는다.

Verification은 current published metadata revision 확인, SQL AST validation, 실제 relation set 확인,
현재 `QueryService` 실행, `truncated=false`, exact columns/row count/hash 비교 순서로 수행한다.
Guarded Query의 safety path를 우회하지 않는다.

### Result hash contract

```text
{"columns": ordered columns, "rows": canonical encoded rows}
-> compact UTF-8 JSON, ensure_ascii=false, sort_keys=true
-> SHA-256 with "sha256:" prefix
```

Rows는 Guarded Query의 canonical result encoding을 거친 값이어야 한다. Numeric, binary,
date/time, mapping 또는 non-finite value encoding이 바뀌면 같은 SQL의 verified hash도 바뀐다.

### Metadata quality evaluation contract

- Expected relation은 순서를 포함한 exact tuple로 비교한다.
- Optional answerability status는 exact value로 비교한다.
- Context bytes는 compact UTF-8 JSON response 전체 크기다.
- Relation accuracy, answerability recall과 maximum context bytes가 versioned gate를 만족해야 한다.
- 실패하면 성공처럼 출력하지 않고 non-zero CLI exit와 bounded failure report를 제공한다.

이 결과는 metadata retrieval 회귀 증거이지 production query correctness 전체나 latency SLO가 아니다.

### Offline CLI composition contract

`query-man-evaluate`와 `query-man-verify`의 console-script target은 각각
`query_man.assurance_cli:evaluate_main`과 `query_man.assurance_cli:verify_main`이다. 외부 command 이름,
`--root` 인자와 기본값, JSON stdout, exit 의미는 기존 계약을 유지한다.

두 entrypoint는 runtime authority selector나 Control DB를 사용하지 않고 지정한 root의 `.env`,
`config/sources`, budget, quality와 verified file만 읽는 bootstrap-only workflow다. Evaluate는 catalog를,
verify는 executor와 catalog를 기존 순서로 정리한다. Verify는 `VerifiedQueryRegistry.verify_all`에
`QueryService`를 넘겨 모든 SQL이 Guarded Query validation/execution path를 지나게 하며 tenant ID를
추가하지 않는다.

### Verified membership contract

Assurance는 Metadata가 소유한 inbound shape인
`source_id -> immutable metadata revision set`에 맞는 값을 제공한다. Runtime composition과
Control Plane이 이를 L2 판단 입력으로 주입한다. Membership은 exact revision에 묶이며 다른
revision으로 자동 승계하지 않는다. Bootstrap mode는 filesystem verified contract만 load하고,
managed mode는 empty map에서 시작해 Control DB verified contract만 반영한다. 두 authority를
합치거나 managed failure 때 filesystem으로 fallback하지 않는다.
Control Plane은 public administration input을 Assurance의 Verified DTO로 변환하고 그 DTO/hash를
소비해 immutable contract를 publish한다. Delivery는 Assurance DTO를 직접 만들지 않으며 Assurance
core는 Control DB implementation을 import하지 않는다.

### Usage projection acceptance contract (`CTRL-08`)

Assurance는 Control Plane/Runtime/Delivery가 승인된 CTRL-08 계약을 함께 만족하는지 검증하되 public
status를 다시 계산하는 production helper나 별도 DTO를 소유하지 않는다. Acceptance는 migration 5
upgrade/least privilege, current-generation success/failure fencing, fresh last-success 보존,
`not_configured|pending|available|stale|unavailable`, exact freshness boundary, gateway live/current-cursor
aggregate, inclusive 31일 cutoff와 최대 1,000행, missing-to-zero 금지와 response redaction을 포함한다.

기존 admin/MCP/health response가 바뀌지 않는지와 code rollback이 migration ledger/table/data를
보존하는지도 함께 확인한다. 이 evidence는 아래 CTRL-09 recovery fixture acceptance를 대신하지
않는다.

### Control recovery acceptance contract (`CTRL-09`)

Assurance는 production schema나 Runtime 복구 의미를 다시 정의하지 않고 Control Plane과 Runtime의
기존 계약을 하나의 runnable scenario로 조립한다. 격리 PostgreSQL 18.4 Control DB의 custom archive를
현재 18.6 fresh DB에 복원하고 migration runner를 두 번 적용한다. Restore 전후 비교 대상은
migration ledger 1개, core authority 6개와 bounded operational projection 6개를 합친 13개 table의
UTC/C canonical count와 SHA-256 fingerprint다.

원래 key는 DB archive와 분리된 test input으로 재주입하고 모든 immutable generation decrypt,
wrong-key 거부와 기존 mutation receipt exact replay를 확인한다. Source/verified directory가 없는
managed mode에서 원래 stable replica ID 두 개가 새 incarnation으로 `available`, `drift=[]`에
수렴하고 양쪽 metadata/guarded verified query가 같아야 한다. 31일 밖 under-cap rollup은 DB에
남지만 `/usage`에는 보이지 않아야 한다. Test 전용 recovery service, archive, finite writer LOGIN,
database와 connection은 성공·실패와 무관하게 정리한다.

이 acceptance는 actual production backup age/RPO, archive storage access, secret-manager provider,
TLS/IAM, source business DB recovery나 60분 RTO 측정이 아니다. 이를 test fixture 결과로 대체하지
않는다. Key rotation/versioning, physical age deletion이나 supported PostgreSQL major 범위를 바꾸지
않는다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 `SourceReader`로 얻는 recursively immutable known
  source와 semantic definition
- [Metadata](../metadata/README.md)의 plain JSON context, immutable published snapshot/revision과
  L0/L1/L2 gate semantics
- [Guarded Query](../guarded-query/README.md)의 SQL policy, guarded execution과 canonical result encoding

Assurance는 Delivery transport나 Control DB concrete adapter를 통해 검증 규칙을 우회하지 않는다.

## 불변조건

- Verified contract는 exact metadata revision과 relation set에 묶인다.
- Source/metadata Python collection representation 전환만으로 metadata revision, canonical result
  encoding 또는 verified result hash를 바꾸지 않는다.
- Truncated result, changed column order, row count 또는 hash는 verification 성공이 아니다.
- Expected output을 live result에서 자동 갱신하거나 승인하지 않는다.
- Quality/verified config의 unknown version/field/source와 duplicate ID를 fail-closed한다.
- Verification SQL은 현재 Guarded Query policy, budget과 reader safety를 그대로 통과한다. Standalone
  `query-man-verify`는 tenant ID를 공급하지 않으므로 RLS source를 fail-closed한다. Authenticated
  operator tenant를 전달하는 Control verified-publish path와 지원 범위를 혼동하지 않는다.
- Question, SQL과 expected business values를 일반 runtime log에 노출하지 않는다.
- Quality fixture를 source별 production Python branch로 바꾸지 않는다.
- Concrete source/catalog/metadata/query 조립을 `assurance_cli.py` 밖의 Assurance core로 되돌리지
  않는다.

## 모듈 내부 변경

다음은 config schema, 계산식과 pass/fail 결과를 보존할 때 독립적으로 변경할 수 있다.

- Report formatting과 CLI orchestration 정리
- 같은 comparison을 만드는 loop/helper 개선
- 동일 membership을 만드는 registry lookup/data structure 개선
- Hash input/serialization을 바꾸지 않는 hashing helper 정리

## 사용자 승인이 필요한 계약 변경

- Quality 또는 verified configuration schema/version/default 변경
- Versioned quality/verified case를 추가·보강해 acceptance gate, expected result 또는
  verified revision membership을 바꾸는 변경
- Expected relation ordering, answerability, accuracy/recall/context-byte 계산이나 threshold 의미 변경
- Verified query의 revision/relation/truncation/column/row-count matching 의미 변경
- Result hash algorithm, payload, canonical JSON 또는 prefix 변경
- Guarded Query result scalar encoding에 따른 existing hash migration
- Verified revision membership 또는 bootstrap/managed authority의 mutual exclusion, filesystem
  non-read/fallback 변경
- L0/L1/L2 criteria 또는 publishable comparison 변경
- Expected result 자동 생성/승인과 QueryService 우회 경로 추가
- CLI success/failure exit와 report contract 변경

승인 요청에는 Metadata, Guarded Query와 Control Plane 영향, existing verified data migration 및
acceptance evidence 갱신 계획을 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_quality.py tests/test_verified.py \
  tests/test_assurance_cli.py tests/test_quality_level.py tests/test_result_encoding.py
```

Control recovery fixture는 격리된 두 PostgreSQL service가 필요하므로 별도 integration gate로
실행한다.

```text
uv run pytest -m integration tests/test_control_recovery.py
```

Configured live sources가 필요한 acceptance는 별도로 실행한다.

```text
uv run query-man-evaluate
uv run query-man-verify
```

Control publish/L2 경계를 바꾸면 source-admin tests를, live database verification 경계를 바꾸면
`uv run pytest -m integration`도 실행한다. 완료 전 root `AGENTS.md`의 전체 gate를 실행한다.
Control public input에서 Verified DTO로 가는 mapping을 바꾸면 `tests/test_source_admin.py`도 함께
실행한다.

## 집중해서 읽을 범위

Assurance 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 quality/verified core 또는 Assurance CLI composition code, config와 focused tests
3. Metadata context/quality와 Guarded Query result/hash 소비 계약
4. [ADR 0006](../../decisions/0006-mcp-transport-and-workflow.md),
   [ADR 0007](../../decisions/0007-immutable-metadata-publishing.md),
   [ADR 0011](../../decisions/0011-metadata-quality-level-publish-gate.md)과
   [ADR 0013](../../decisions/0013-control-plane-verified-query-publishing.md) 중 변경과 직접 관련된 결정
5. Persistence를 바꾸거나 recovery acceptance를 조립하는 경우 Control Plane/Runtime contract와
   [disaster recovery runbook](../../disaster-recovery.md)

HTTP/MCP middleware, source store transaction과 query pool 내부는 계약을 바꾸지 않는 한 읽을
필요가 없다.
