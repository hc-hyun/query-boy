# Active Development TODO

이 문서는 현재 baseline과 아직 완료되지 않은 작업만 기록합니다. Repository 변경 완료와 protected
environment 실행 완료는 서로 다른 상태이며, protected 완료 사실은 repository가 아니라 승인된 환경의
append-only/immutable change record로 증명합니다.

## 현재 baseline

- Production `config/sources/` package가 없습니다.
- Production `config/database-profiles.yaml`이 없습니다.
- Versioned budget, access-policy example과 security corpus는 있습니다.
- Query Cave는 certificate-authenticated production 경로를 검증하는 disposable assurance 환경입니다.
- 따라서 첫 production source가 review되기 전의 Runtime과 `qm source validate`는 의도적으로
  fail-closed합니다.

Repository에는 protected target이나 실행 evidence를 저장하지 않으므로 아래 protected 작업은 완료로
간주하지 않습니다.

## 작업 순서

```text
SOURCE-01 ──> DBENV-01 ──┐
                         ├──> LAUNCH-02
AUTHENV-01 ──────────────┘
```

| 작업 | 종류 | 현재 상태 | 완료 기준과 owner 절차 |
|---|---|---|---|
| `SOURCE-01` | Repository | 미착수 | 첫 production source package와 database profile이 review되고 repository gate를 통과함. [Source onboarding](source-extension-checklist.md) |
| `DBENV-01` | Protected | `SOURCE-01` 대기 | Exact DB/source/reader, privilege, marker와 certificate admission evidence가 승인된 환경 기록에 남음. [Source onboarding](source-extension-checklist.md), [Certificate guide](database-certificate-authentication.md) |
| `AUTHENV-01` | Protected | 미실행 | 하나의 token authority, secret 전달 경로, query/operator·source authorization evidence가 승인된 환경 기록에 남음. [Operations](operations.md) |
| `LAUNCH-02` | Protected | `DBENV-01`, `AUTHENV-01` 대기 | Approved image/config의 traffic-off acceptance, 제한적 cutover와 rollback readiness가 기록됨. [Operations](operations.md) |

## SOURCE-01

Source owner가 실제 source ID, public 설명, provenance, reader, allowed schema와 budget을 정하고 DB/data
owner가 curated view의 exact output과 no-PII 경계를 review합니다. 첫 physical DB이므로 다음 versioned
artifact가 필요합니다.

```text
config/database-profiles.yaml
config/sources/<source-id>/source.yaml
config/sources/<source-id>/views.sql
```

이 repository 작업은 certificate를 발급하거나 DB에 연결하고 DDL을 실행하지 않습니다. Exact target,
credential과 DBA apply는 `DBENV-01`에서 별도 승인합니다.

## Protected 작업

`DBENV-01`은 `SOURCE-01`의 approved revision을 고정한 뒤 source와 certificate guide의 positive/negative
probe를 수행합니다. `AUTHENV-01`은 non-loopback 배포에 단일 API token 또는 access-policy 중 하나만
선택하고 실제 secret 전달과 capability 분리를 검증합니다. 두 evidence가 모두 준비된 뒤에만
`LAUNCH-02`를 [Operations](operations.md)의 traffic-off acceptance와 cutover 순서로 수행합니다.

Target, approved revision, 실행자, stop condition, rollback 또는 change-record 위치가 불명확하면 protected
작업을 시작하지 않습니다. 상세 중단 조건을 이 문서에 복제하지 않고 각 owner procedure를 따릅니다.

현재 범위 밖의 후보는 [별도 결정이 필요한 변경](decisions/README.md#별도-결정이-필요한-변경)에 있습니다.
