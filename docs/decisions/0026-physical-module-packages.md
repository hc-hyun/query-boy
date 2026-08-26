# ADR 0026: Physical Module Packages

Status: Accepted

Date: 2026-08-27

Decision ID: `PHYSICAL-MODULES-20260827`

Baseline: `cecc215aa31f658a8cd51948f1c32fd062a23727`

## Context

Query Man은 하나의 repository, wheel과 process로 배포되는 modular monolith다. 논리 module owner와
허용 dependency는 이미 정해져 있었지만 static Python 구현이 root의 flat module에 흩어져 있어,
한 owner의 private 구현을 바꿀 때도 관련 없는 파일과 symbol을 함께 탐색해야 했다. Managed-only
구현은 이미 `query_man.managed` package에 격리돼 있었다.

사용자는 AI context와 사람의 탐색 범위를 줄이기 위한 ownership/composition refactor를 승인했다.
이 결정은 파일과 import 경계를 물리적으로 드러내는 변경이며 interface, format, policy 또는 serving
범위를 새로 선택하는 기능 변경이 아니다.

## Decision

Static 구현을 `src/query_man` 아래 여섯 owner package로 나눈다.

| Package | Primary owner |
|---|---|
| `source_catalog` | Source Catalog |
| `metadata` | Metadata |
| `guarded_query` | Guarded Query |
| `delivery` | Delivery |
| `runtime` | Runtime |
| `assurance` | Assurance |

기존 `managed` package는 같은 위치에 유지한다. 그 안의 Control Plane implementation,
`managed/source_admin_routes.py` Delivery adapter와 `managed/runtime.py` Runtime composition의 owner도
그대로다.

각 module package의 `__init__.py`는 marker-only이며 symbol을 재-export하지 않는다. Consumer는 provider
문서와 dependency map이 공개한 leaf module에서 직접 import한다. 이전 flat import path를 유지하는
forwarding shim은 만들지 않는다.

Owner별 model은 `source_catalog/models.py`와 `metadata/models.py`로 나눈다. 이전 `app.py`의 Delivery
surface와 static Runtime composition도 각각 `delivery/app.py`와 `runtime/composition.py`로 나눈다.
Root `errors.py`는 여러 owner의 official domain error와 Delivery의 `AppError` carrier를 보존하는
interface-only shared artifact로 유지한다. Private implementation이나 helper를 이 file에 추가하지 않는다.

Test file과 공통 fixture는 기존처럼 repository root의 `tests/`에 둔다. Physical package 경계는 test
tree, Python distribution, process 또는 deployment unit을 추가로 만드는 근거가 아니다.

## Preserved Meanings

이 refactor는 다음 의미를 바꾸지 않는다.

- Official module interface의 Python shape, signature와 호출 단위 input/output/domain-error semantics
- HTTP, MCP와 CLI external wire format
- Control DB, config, metadata codec와 다른 persisted/versioned format
- Metadata/SQL policy revision, canonical encoding/hash, allowlist와 reader/resource policy
- Authorize, validate, transaction, cancel, rollback, startup, shutdown과 cleanup lifecycle outcome
- Static/managed authority 선택, current two-source launch inventory와 protected execution procedure

[ADR 0025](0025-static-non-rls-first-launch.md)가 계속 현재 launch authority다. 이 결정은 managed
capability를 current serving에 추가하거나 parked research를 활성화하지 않는다.

## Consequences

- 개발자는 module README에서 owner package의 leaf와 직접 consumer test로 바로 이동할 수 있다.
- Private implementation을 owner package 안에서 바꾸는 작업은 consumer import 수정 없이 유지하기
  쉬워진다. Official interface 의미를 바꾸는 경우에는 기존 승인 절차가 그대로 적용된다.
- Package path 자체는 이동했으므로 repository 내부 import와 test import는 새 leaf path를 사용한다.
  지원 대상으로 선언된 별도 external Python consumer 증거는 없으며 compatibility shim debt를 만들지
  않는다.
- Metadata와 Guarded Query의 existing official interface dependency, Runtime operations sink와 managed
  multi-owner package는 그대로 남는다. 이번 이동을 dependency 방향 변경이나 별도 interface package로
  해석하지 않는다.
- 모든 코드는 같은 repository, wheel과 process에 남는다. 별도 repository, package distribution,
  service, deployment 또는 network boundary는 생기지 않는다.

## Validation

변경 완료 gate는 다음을 함께 확인한다.

- 모든 production/test import가 새 owner leaf를 가리키고 old flat import가 남지 않음
- Package `__init__.py`가 marker-only이고 forwarding re-export가 없음
- Production import graph에 새 cycle이 없음
- Module index가 현재 Python file의 owner를 모두 매핑함
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`
- Documentation link와 immutable completion-ledger gate

기존 interface, wire/persisted golden, policy revision과 lifecycle test 결과가 달라지면 path-only refactor로
간주하지 않고 중단해 별도 승인을 받는다.

## Rollback

Rollback은 `PHYSICAL-MODULES-20260827` change-set 전체를 baseline으로 되돌리는 것이다. DB migration,
data rewrite나 operational rollback은 없다. 일부 package만 되돌리거나 old flat forwarding shim을
추가하지 않는다. 원자적 revert로 import graph와 문서를 함께 이전 상태로 복구할 수 없으면 배포를
중단하고 새 승인 범위를 정한다.
