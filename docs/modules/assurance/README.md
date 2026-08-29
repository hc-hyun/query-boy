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
- 실행 시점 evidence의 append-only provenance 규칙

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

`config/verified-queries.yaml`의 version, query/source ID, SQL, metadata revision, relation membership, ordered
columns, row count와 hash는 persisted/versioned acceptance format이다. Source membership은
`config/sources/*.yaml`과 정확히 일치해야 하며 unknown source/version/field와 duplicate ID를
fail-closed한다.

Result hash는 ordered column names와 canonical result values를 SHA-256한다. Column order, numeric/date/time
encoding과 row ordering 의미를 바꾸면 compatibility identity 변경이다.

Offline CLI만 `SourceRegistry`, PostgreSQL catalog/query와 Metadata service concrete implementation을
조립한다. Runtime authority selector나 Control DB는 사용하지 않는다. Cleanup은 production과 같은
reader/cancel/rollback invariant를 보존한다.

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
- Evidence는 commit/environment/time/command/result를 보존하며 과거 record를 소급 수정하지 않는다.
- 과거 managed evidence는 당시 사실일 뿐 현재 Control Plane capability를 증명하지 않는다.

## 모듈 내부 변경

Artifact schema/hash/CLI exit와 provider policy를 보존하는 private report formatting, fixture data와 test
helper 정리는 module 내부 변경이다.

## 사용자 승인이 필요한 경계 변경

- Quality/verified DTO와 YAML schema/version/default
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
뒤에만 새 날짜 문서로 append한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Quality | `assurance/quality.py`, quality YAML, `test_quality.py` |
| Verified/hash | `assurance/verified.py`, verified YAML, result encoding, `test_verified.py` |
| Offline CLI | `assurance/cli.py`, provider composition, `test_assurance_cli.py` |
| CI/fixtures | workflow/script/fixture와 integration/container tests |
| Documentation/evidence | `test_documentation.py`, current indexes, immutable evidence rules |
