# Assurance Module

Status: Physical package boundary active

## 목적

### 30초 요약

Assurance는 Git-reviewed quality/verified/security artifact를 strict validation하고 source metadata와 query
결과가 기대와 일치하는지 offline 및 CI에서 검증한다. Production request를 serve하거나 source를
변경하지 않는다.

## 소유 책임

- Quality case/gate configuration과 evaluation
- Verified query/expected result DTO, configuration와 comparison
- Ordered column/row canonical result hash
- `query-man-evaluate`, `query-man-verify` offline concrete composition
- Fixture, integration, container, load/soak와 documentation acceptance ownership
- Repository gate의 exact commit/CI provenance와 protected evidence 경계

## 소유하지 않는 책임

- Source YAML schema와 reader secret, PostgreSQL production DDL
- Metadata revision/SQL execution policy 자체
- HTTP/MCP production transport와 Runtime authority/lifecycle
- Control DB verified persistence, managed onboarding/recovery/multi-replica serving

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`assurance/quality.py`](../../../src/query_man/assurance/quality.py) | Quality config, cases/gates와 report |
| [`assurance/verified.py`](../../../src/query_man/assurance/verified.py) | Verified config, DTO, comparison와 result hash |
| [`assurance/cli.py`](../../../src/query_man/assurance/cli.py) | Offline evaluate/verify composition root |
| [`config/quality-evaluation.yaml`](../../../config/quality-evaluation.yaml) | Metadata answerability cases |
| [`config/verified-queries.yaml`](../../../config/verified-queries.yaml) | Git-reviewed verified query authority |
| [`config/security-evaluation.yaml`](../../../config/security-evaluation.yaml) | Security acceptance corpus |
| [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml), [`scripts/verify-container.sh`](../../../scripts/verify-container.sh) | Repository/container gates |
| [`test_quality.py`](../../../tests/test_quality.py), [`test_verified.py`](../../../tests/test_verified.py), [`test_assurance_cli.py`](../../../tests/test_assurance_cli.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`QualityCase`, `QualityGates`, `QualityReport`, `QualityEvaluation`과 `ExpectedResult`, `VerifiedQuery`,
`VerifiedQueryRegistry`, `create_result_hash`가 public offline/acceptance interface다.
`assurance.verified`의 strict parser와 `VerifiedQueryConfigurationError`는 Runtime의 startup L2 revision
확인과 local `qm source validate`도 소비한다. Assurance가 production request를 serve한다는 뜻은 아니다.

`config/verified-queries.yaml`의 version, query/source ID, SQL, metadata revision, relation membership, ordered
columns, row count와 hash는 persisted/versioned acceptance format이다. Source membership은
`config/sources/*.yaml`과 정확히 일치해야 하며 unknown source/version/field와 duplicate ID를
fail-closed한다.

Result hash는 ordered column names와 canonical result values를 SHA-256한다. Column order, numeric/date/time
encoding과 row ordering 의미를 바꾸면 compatibility identity 변경이다.

Offline CLI만 `SourceRegistry`, PostgreSQL catalog/query와 Metadata service concrete implementation을
조립한다. Runtime authority selector나 Control DB는 사용하지 않는다. Cleanup은 production과 같은
reader/cancel/rollback invariant를 보존한다.

`query-man-evaluate`와 `query-man-verify`는 Runtime logging 설정에 의존하지 않고 실행 중에만
PostgreSQL client logger 경계를 설치한다. `psycopg`/`psycopg_pool` warning은 message, argument와
exception text를 렌더링하지 않고 stderr의 고정 JSON
`{"event": "database_dependency_log"}`로 기록하며 command가 끝나면 기존 logger 설정을 복원한다.
`psycopg.Error`, pool timeout 또는 reader session policy failure가 exception chain에 있으면 결과를
stdout에 쓰지 않고 stderr에
`{"error_code": "DATABASE_UNAVAILABLE", "status": "failed"}`를 쓴 뒤 exit 1로 종료한다. Config
validation, quality gate와 verified-result mismatch처럼 DB dependency가 아닌 실패의 기존 output/exit
의미는 바꾸지 않는다.

## Verified Query 회귀검사

Verified query는 검토한 질문과 SQL을 다시 실행해 metadata·relation·결과가 달라졌는지 찾는
회귀검사이며 실행 허용 SQL 목록이 아니다. `config/verified-queries.yaml`에 없는 SQL도 일반 safety와
권한·resource policy를 통과하면 실행할 수 있고, 등록된 SQL도 그 검사를 우회하지 않는다.

현재 first-launch set은 `development-issues` 4개와 `market-voc` 5개, 총 9개입니다. 각 entry는 query/source
ID, 질문, 결정적인 read-only SQL, exact metadata revision과 relation set, ordered columns, row count와
SHA-256 result hash를 보존합니다. RLS source는 현재 전면 quarantine하므로 성공 entry가 없습니다.

```bash
uv run query-man-verify
```

Command는 각 entry에 대해 다음을 확인합니다.

1. 현재 metadata revision이 recorded revision과 같은지 검사합니다.
2. SQL을 다시 검증하고 실제 참조 relation set을 비교합니다.
3. Production과 같은 Guarded Query 경로로 실행합니다.
4. 결과가 잘리지 않았는지 확인하고 ordered columns, row count와 canonical result hash를 비교합니다.
5. 하나라도 다르면 non-zero로 실패하며 config를 쓰거나 다른 authority로 import하지 않습니다.

실패는 새 결과가 틀렸다는 자동 판정이 아니라 schema·fixture·metadata·SQL·policy 변경을 조사하라는
신호입니다. 의도한 변경도 expected hash를 자동 갱신하지 않고 새 revision과 9개 전체 결과를
review합니다. 통과는 모든 질문이나 production data의 정확성을 보증하지 않습니다.

현재 final result는 PostgreSQL base OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용합니다. Hash는 ordered
columns/rows와 canonical JSON scalar를 사용하고 numeric scale, date ISO와 aware datetime UTC `+00:00`
표현을 보존합니다. 이 identity를 바꾸려면 compatibility 승인과 full reissue가 필요합니다.

Rollback은 reviewed Git revert 또는 이전 pinned artifact입니다. Runtime fallback, merge 또는
write-back은 rollback이 아닙니다. Serving 범위는
[ADR 0025](../../decisions/0025-static-non-rls-first-launch.md), 저장 authority는
[ADR 0030](../../decisions/0030-git-reviewed-yaml-source-authority.md)을 따릅니다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | Git YAML inventory, `SourceReader`, budget/reader policy | Unknown/missing source를 거부 |
| Metadata | Prepared revision/context/quality data | Revision을 Assurance가 재계산하지 않음 |
| Guarded Query | Validation/execution/result encoding | Safety policy를 test-only shortcut으로 우회하지 않음 |

## 불변조건

- Quality/verified/security config의 unknown version/field/source와 duplicate ID를 거부한다.
- Verified SQL은 current metadata revision과 exact expected result를 모두 만족해야 한다.
- Hash는 ordered columns/rows와 canonical encoding을 사용한다.
- Offline CLI도 production reader, SQL, budget, OID, cancel/rollback 정책을 우회하지 않는다.
- Repository 결과는 exact commit과 실행한 command/CI run에 연결하고 오래된 PASS 서술을 현재 증거로
  사용하지 않는다.
- Protected environment evidence는 승인된 change-record system에 append-only/immutable하게 남기며
  repository test 결과로 대신하지 않는다.

## 모듈 내부 변경

Artifact schema/hash/CLI exit와 provider policy를 보존하는 private report formatting, fixture data와 test
helper 정리는 module 내부 변경이다.

## 사용자 승인이 필요한 경계 변경

- Quality/verified YAML schema/version/default와 compatibility 의미
- Verified identity, canonical encoding/result hash와 comparison semantics
- Offline CLI command/output/exit와 concrete composition/cleanup
- Required gate, fixture topology와 evidence procedure
- Source authority와 verified artifact의 관계 또는 managed persistence 재도입

## 검증

```bash
uv run pytest tests/test_quality.py tests/test_verified.py tests/test_assurance_cli.py \
  tests/test_security_evaluation.py tests/test_documentation.py
```

Fixture/DB boundary 변경은 integration과 container gate도 실행한다. Evidence는 실제 command가 성공한
뒤 exact commit/PR/CI provenance에 연결합니다. Protected action의 evidence는 별도 실행 승인 뒤 환경
change record에만 append합니다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Quality | `assurance/quality.py`, quality YAML, `test_quality.py` |
| Verified/hash | `assurance/verified.py`, verified YAML, result encoding, `test_verified.py` |
| Offline CLI | `assurance/cli.py`, provider composition, `test_assurance_cli.py` |
| CI/fixtures | workflow/script/fixture와 integration/container tests |
| Documentation/verification | `test_documentation.py`, current docs, Git/CI와 protected evidence rules |
