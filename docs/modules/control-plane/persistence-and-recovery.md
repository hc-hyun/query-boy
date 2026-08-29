# Control Plane Persistence And Recovery — Retired

Status: Retired historical pointer; not a current runbook

[ADR 0030](../../decisions/0030-git-reviewed-yaml-source-authority.md)에 따라 Control DB, migration, source
generation/receipt/metadata store와 recovery tooling이 current tree에서 제거됐다. 이 문서는 immutable
ledger와 과거 evidence의 link/anchor만 보존한다. 현재 source authority와 rollback은 Git revision과
reviewed deployment artifact이며 [active module index](../README.md)를 따른다.

## Authority와 저장 범위

Current authority는 `config/sources/*.yaml`, `config/budget-profiles.yaml`,
`config/verified-queries.yaml`의 Git-reviewed version이다. Secret은 Git 밖 environment/secret store에
둔다. 이 repository change는 기존 Control DB를 drop하거나 data를 mutate하지 않는다.

## Persisted state model

Retired. Immutable generation, active pointer와 metadata snapshot row는 current persisted format이 아니다.

## Application state transition

Retired. Source 변경은 pull request review, validation, deploy/restart와 Git rollback으로 수행한다.

## Transaction과 migration 불변조건

Historical anchor only. Control migration/application transaction은 current tree에 없다.

## Mutation receipt와 timeout reconciliation

Historical anchor only. Mutation receipt와 timeout reconciliation API는 지원하지 않는다.

## Desired-state 적용과 convergence

Historical anchor only. Replica generation convergence와 hot reload는 지원하지 않는다.

## Credential persisted format

Retired. Source password는 YAML이나 application database에 저장하지 않고 external secret/environment에서
resolve한다. 실제 credential 설치·회전·폐기는 별도 protected operation이다.

## Recovery와 rollback

Application source definition은 이전 검증된 Git revision으로 되돌리고 다시 배포한다. 남아 있는 과거
Control DB의 backup, export, drop 또는 disposal은 이 문서를 실행 runbook으로 사용하지 말고 exact
target/scope/retention/rollback과 change-record 책임을 확인한 별도 승인을 받는다.

## 변경 중단 조건과 검증

DB-backed authority나 Control recovery를 다시 도입하려면 persisted schema, migration, secret, API,
lifecycle, privacy, compatibility와 rollback을 새로 설계하고 사용자 승인과 runnable acceptance를
받는다. 과거 baseline은 관련 ADR/evidence commit에서 읽는다.
