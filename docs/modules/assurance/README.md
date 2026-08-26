# Assurance Module

Status: Logical boundary; physical package split pending; `LAUNCH-01-A` repository acceptance complete;
protected execution separately gated

## 목적

### 30초 요약

Assurance는 Query Man의 **검사팀**이다. Metadata가 질문에 맞는 정보를 골랐는지, 이미 검토한 SQL의
결과가 달라지지 않았는지, 여러 module을 연결한 실제 실행이 안전 경계를 지키는지 자동 검사한다.

- Verified query는 회귀검사 항목이지 허용 SQL 목록이 아니다.
- 현재 static launch에는 `development-issues` 4개와 `market-voc` 5개, 총 9개 항목이 있다.
- 검사는 실제 Metadata와 Guarded Query 경로를 사용한다. 안전 검사를 우회하지 않는다.
- Repository 검사가 통과해도 production 배포나 protected environment 실행이 끝난 것은 아니다.

현재 launch authority는 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 두 source,
단일 replica, static non-RLS profile이다.

## 소유 책임

- Metadata retrieval 품질 항목과 accuracy, answerability, context-byte gate
- Versioned verified-query artifact와 source별 revision membership
- Revision, relation, column, row count와 result hash를 비교하는 live verification
- `query-man-evaluate`와 `query-man-verify` offline command 조립
- Cross-module integration, HTTP/MCP parity, container와 database-corner acceptance
- 실행 당시 범위를 보존하는 verification evidence와 repository gate

Metadata의 L0/L1/L2 판정 구현은 [Metadata](../metadata/README.md)가 소유한다. Assurance는 그 판정에
필요한 verified revision membership과 실행 증거를 제공한다.

## 소유하지 않는 책임

- HTTP/MCP 요청 처리, 인증과 인가
- Physical catalog/context algorithm, metadata revision과 L0/L1/L2 판정 구현
- SQL AST 정책, reader connection 검사, query 실행과 result scalar encoding
- Control DB table, persistence transaction과 active source pointer
- Source manifest, runtime topology, image build와 protected cutover 실행
- Expected result 자동 생성·승인이나 production 데이터 전체의 정확성 보증
- Service SLO, provider billing, production metric과 alert 집계

RLS attestation, 넓은 result type, cost attribution과 trace는 첫 launch 밖의 parked research다. 관련
문서가 있다는 이유만으로 현재 acceptance가 늘어나지 않는다.

## 현재 코드 위치

| 영역 | 위치 | 역할 |
|---|---|---|
| Metadata 품질 | [`quality.py`](../../../src/query_man/quality.py), [`quality-evaluation.yaml`](../../../config/quality-evaluation.yaml) | 질문별 relation 선택과 answerability 품질 검사 |
| Verified query | [`verified.py`](../../../src/query_man/verified.py), [`verified-queries.yaml`](../../../config/verified-queries.yaml) | DTO, registry, comparison, hash와 현재 9개 항목 |
| Offline 실행 | [`assurance_cli.py`](../../../src/query_man/assurance_cli.py) | 두 command의 유일한 offline composition root |
| Safety corpus | [`security-evaluation.yaml`](../../../config/security-evaluation.yaml) | Parser와 query safety 입력 |
| 검증 조립 | [`ci.yml`](../../../.github/workflows/ci.yml), [`verify-container.sh`](../../../scripts/verify-container.sh), [focused tests](../../../tests/test_verified.py) | `core-static`/`managed-acceptance`, container gate와 Assurance core 검증 |
| Static DB fixture | [`compose.yaml`](../../../compose.yaml), [`apply-db.sh`](../../../scripts/apply-db.sh), [`validate-static-fixtures.sh`](../../../scripts/validate-static-fixtures.sh) | Current 두 source만 준비하며 Control/support/commerce를 포함하지 않음 |
| Managed DB fixture | [`compose.acceptance.yaml`](../../../compose.acceptance.yaml), [`apply-managed-acceptance-fixtures.sh`](../../../scripts/apply-managed-acceptance-fixtures.sh) | 별도 `query-man-managed-acceptance` project/container/volume에서만 Control/support/commerce를 준비 |
| Evidence | [Verification index](../../verification/README.md) | 실행 시점별 immutable record 색인 |

Source fixture SQL은 acceptance infrastructure이지 production source schema authority가 아니다. Base와
managed-acceptance project는 PostgreSQL container/volume을 공유하지 않는다. Control migration과 verified
persistence 의미는 Control Plane이 소유한다.

## 제공 인터페이스와 소유 경계

이 절은 Python module interface와 artifact, hash, CLI, evidence를 구분한다. 중요하다는 이유만으로
모두 module interface라고 부르지 않는다.

### Official module interfaces

현재 다른 module용으로 공개한 Python interface는 다음과 같다.

| Interface | 직접 소비자 | 호출 의미 |
|---|---|---|
| `ExpectedResult` | Control Plane | 기대 column 순서, row count와 result hash를 담는 immutable DTO |
| `VerifiedQuery` | Control Plane | query/source ID, 질문, SQL, metadata revision, relation과 기대 결과를 담는 immutable DTO |
| `VerifiedQueryRegistry.revision_map()` | Metadata, Runtime composition | `source_id -> frozenset[metadata revision]` membership 반환 |
| `create_result_hash(columns, rows) -> str` | Control Plane, acceptance | ordered columns와 canonical rows의 SHA-256 identity 생성 |

위 Python shape와 호출 단위 오류 의미만 module interface다. Hash payload/encoding은 policy identity이고,
Control DB row는 persisted format이다.

### Verified-query artifact schema

Static filesystem artifact는 strict version 1이고 `query_id`는 file 전체에서 유일하다.
각 항목은 source ID, 질문, deterministic read-only SQL, exact metadata revision/relation set과 expected
column 순서, row count, canonical result hash를 저장한다.

Managed Control DB identity는 `(source_id, query_id, metadata_revision)`이다. 이를 filesystem의 global
`query_id`와 합치지 않는다. Static은 file만, managed는 DB projection만 사용하며 import, merge와
fallback은 없다.

### Result hash policy identity

Hash identity는 `{"columns": ordered columns, "rows": canonical encoded rows}`를 compact UTF-8 JSON
(`ensure_ascii=false`, `sort_keys=true`)으로 만든 뒤 `sha256:` prefix를 붙인 SHA-256이다.

검증 순서는 metadata revision → relation set → Guarded Query → `truncated=false` → exact columns → row
count → hash다. Live result로 expected output을 자동 갱신하지 않는다.

[Verified-query 안내](../../verified-queries.md)의 9개 metadata revision, columns, row counts와 result
hashes는 SQL policy v3 전환에서도 유지한다. SQL policy v3 digest는
`sha256:2e94db36095f11f2e9cc4e804666598f79a2ee956002ffa60dbe26bc6ee81388`이다.

### Offline CLI surface and composition boundary

| Command | 목적 | 입력 authority |
|---|---|---|
| `query-man-evaluate` | Metadata retrieval 품질 gate 검사 | 지정한 root의 static configuration |
| `query-man-verify` | 9개 verified SQL 결과 회귀검사 | 지정한 root의 static configuration |

Console-script target은 `query_man.assurance_cli:evaluate_main`과
`query_man.assurance_cli:verify_main`이다. Command 이름, `--root`와 기본값, JSON stdout 및 exit 의미는
external CLI surface다.

`assurance_cli.py`만 offline 검사에 필요한 concrete Source Registry, Catalog, Metadata와 Query adapter를
조립한다. Runtime authority selector나 Control DB를 사용하지 않는다. Static RLS manifest는 registry
load에서 먼저 거부되고 CLI는 tenant ID를 추가하지 않는다. Verify SQL은 `QueryService`를 통과한다.
Production server나 Control candidate staging을 여기서 조립하지 않는다.

### 현재 launch에서 확인하는 것

- 두 static source의 9개 항목이 SQL policy v3에서 기존 metadata revision과 result hash로 통과한다.
- PostgreSQL 18과 server/client UTF-8만 허용한다. PG17/19, SQL_ASCII와 non-UTF8 client/codec은 SQL
  없이 `BEGIN` 전에 거부하고 connection을 폐기하며 stale metadata를 제공하지 않는다.
- Final result OID `20, 21, 23, 25, 1082, 1184, 1700`의 nonempty/zero-row 항목을 허용한다.
  Boolean, JSON, bytea, float, array, record와 다른 final OID는 첫 fetch 전에 거부한다. Base OID로
  평탄화되는 scalar domain은 bootstrap/offline Catalog 발행 전에 거부하고 managed default 보존도 확인한다.
- RLS는 성공 항목이 없다. Bootstrap, injected/managed와 direct-query 우회 모두 metadata, queue나 DB
  접근 전에 fail-closed하고 HTTP/MCP에서 detail 없는 `503 QUERY_UNAVAILABLE`로 끝난다. Unsupported
  result도 같은 비노출 오류 의미를 유지한다.
- Container는 exact `{"status":"ready"}` health, pinned upstream image와 application VCS revision label을
  확인한다.

기존 policy token 거부, duplicate-column 우선순위, no-fetch, rollback과 pool recovery도 검증한다.
Domain `type_kind`는 SQL policy material, metadata revision이나 expected result hash에 추가하지 않는다.

### Evidence가 증명하지 않는 것

[Static first-launch acceptance](../../verification/2026-08-26-static-first-launch.md)와
[verification record](../../verification/README.md)는 각각 적힌 commit, fixture와 command만 증명한다.
이후 변경이나 다른 환경을 자동 보증하지 않는다. 과거 record는 수정·삭제하지 않으며 정정은 원문과
provenance를 남긴 새 record로 추가한다.

과거 managed onboarding, Control recovery, multi-replica soak와 RLS finding은 당시 사실로 보존한다.
현재 Control Plane이나 multi-replica serving을 활성화하거나 RLS success를 증명하지 않는다.

Repository acceptance 뒤에도 [LAUNCH-02](../../development-todo.md)의 access, TLS/secrets/backups,
inventory, route, stop/rollback condition과 change record가 남는다. [Operations
runbook](../../operations.md)에 따른 별도 승인 후에만 실행하고 새 evidence를 append한다.

### Metadata quality evaluation criteria

Expected relation은 순서를 포함한 exact tuple, optional answerability는 exact value로 비교한다. Context
bytes는 compact UTF-8 JSON response 전체 크기다. Relation accuracy, answerability recall과 maximum
context bytes가 versioned gate를 만족해야 하며 실패는 bounded report와 non-zero exit다. 이는 retrieval
회귀검사이지 모든 production query의 정답이나 latency SLO 보증이 아니다.

## 소비 인터페이스와 전제

- [Source Catalog](../source-catalog/README.md)의 immutable `SourceReader`와 reader compatibility verifier
- [Metadata](../metadata/README.md)의 published snapshot/revision, context와 quality-level 의미
- [Guarded Query](../guarded-query/README.md)의 SQL policy descriptor, guarded execution, final-OID gate와
  canonical result encoding

Offline CLI만 concrete adapter를 조립한다. Assurance core는 Delivery transport나 Control DB private
implementation을 통해 validation, authorization 또는 Guarded Query safety path를 우회하지 않는다.

## 불변조건

- Verified artifact는 exact metadata revision과 relation set에 묶인다.
- Truncated result, 달라진 column 순서, row count 또는 hash는 성공이 아니다.
- Expected output, immutable record와 evidence를 자동 수정·삭제하지 않는다.
- Quality/verified config의 unknown version/field/source와 duplicate ID를 fail-closed한다.
- Static 9개 항목과 managed projection을 merge하거나 서로 fallback하지 않는다.
- Verification SQL은 현재 SQL policy, budget, reader compatibility와 final-OID gate를 그대로 통과한다.
- RLS source와 unsupported final OID에는 성공하는 verification path가 없다.
- Question, SQL, expected business value와 driver/DB detail을 일반 runtime log에 남기지 않는다.
- Offline, Control candidate와 production composition 소유 경계를 섞지 않는다.
- Repository acceptance를 protected deployment evidence라고 표현하지 않는다.

## 모듈 내부 변경

Interface, artifact/CLI format, hash identity와 pass/fail 의미가 같다면 report/CLI orchestration,
comparison helper, registry lookup/data structure, hashing helper와 acceptance fixture 내부는 정리할 수
있다. Hash input이나 serialization은 그대로여야 한다.

## 사용자 승인이 필요한 경계 변경

다음 의미를 바꾸려면 실제 범주를 구분해 먼저 승인받는다.

- `ExpectedResult`, `VerifiedQuery`, membership/hash capability의 Python shape나 오류 의미
- Quality/verified config schema, version, default와 managed persisted identity
- Relation, answerability, context-byte, revision, truncation, column/row/hash matching 의미
- Result hash payload, canonical JSON, scalar encoding, prefix와 SQL policy identity
- Static/managed authority 분리와 filesystem non-read/fallback 의미
- L0/L1/L2 criteria, acceptance 항목/threshold와 verified membership
- CLI command, argument, JSON output와 success/failure exit 의미
- RLS serving, final OID 확대, managed launch, multi-replica topology와 protected procedure

승인안은 provider/consumer와 각 변경 범주, compatibility, migration, rollback, security 및 검증
계획을 구분한다. Protected environment 실행은 repository 의미 승인과 별도다.

## 검증

```text
uv run pytest tests/test_quality.py tests/test_verified.py tests/test_assurance_cli.py \
  tests/test_reader_policy.py tests/test_result_encoding.py
```

Launch acceptance 변경은 관련 provider/consumer test, DB integration, container와 두 CLI도 실행한다.
Fixture/CI lane 변경은 base static inventory 부재 검사와 격리 managed inventory 검사를 모두 실행하고
[managed acceptance fixture 절차](../../development-guidelines.md#managed-acceptance-fixture)의
`COMPOSE_FILE` 경계를 유지한다. 명령 목록은 evidence가 아니다. 완료 전
[활성 개발 지침](../../development-guidelines.md#tests)의 전체 gate를 실행하고 실제 결과는 새
verification record에 commit, fixture와 command 범위를 남긴다.

## 집중해서 읽을 범위

Assurance 작업은 기본적으로 다음 순서로 읽는다.

1. 이 문서, [module index](../README.md)와 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
2. 변경하는 `quality.py` 또는 `verified.py`, 대응 config와 focused test
3. CLI 변경이면 `assurance_cli.py`, entrypoint test와 provider interface
4. Hash/revision 변경이면 Metadata, Guarded Query 문서와 직접 consumer
5. Cross-module acceptance 변경이면 해당 Runtime/Delivery/Control 경계와 test
6. Fixture/CI lane 변경이면 base/acceptance Compose, 두 apply script, CI job과 직접 consumer test
7. Evidence 추가이면 verification index와 실제 command output

Provider 내부, protected environment와 parked RLS/encoding/COST/TRACE 연구는 변경이 실제로 그 경계를
건드릴 때만 추가로 읽는다.
