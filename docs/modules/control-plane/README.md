# Control Plane Module — Retired

Status: Retired historical pointer; not an active module

Control Plane과 `query_man.managed` package는
[ADR 0030](../../decisions/0030-git-reviewed-yaml-source-authority.md)에 따라 제거됐다. 현재 source,
budget과 verified-query authority는 Git-reviewed YAML이며 active topology는
[여섯 모듈 index](../README.md)를 따른다.

이 파일은 immutable ADR, implementation ledger와 과거 evidence의 link target을 보존하기 위한
tombstone이다. 아래 절은 현재 interface, route, storage, recovery runbook 또는 재활성화 승인이 아니다.
당시 구현의 정확한 내용은 해당 기록의 baseline commit에서 확인한다.

## Source administration application interface

Retired. 과거 source generation, mutation receipt, metadata/verified publish와 rollback application
interface는 current tree에 존재하지 않는다. 새 source와 변경은 `config/sources/*.yaml`,
`config/budget-profiles.yaml`, `config/verified-queries.yaml`을 하나의 Git change set으로 review하고
검증·배포/재시작해 반영한다. `qm source list|show|validate`는 local read-only YAML 명령이며 admin API가
아니다.

## Runtime composition에서 사용하는 reload lifecycle

Retired. Runtime hot reload, Control DB fallback과 managed composition은 없다. 잘못된 YAML과 retired
managed environment는 startup에서 fail-closed한다.

## Runtime이 소비하는 observation writer interface

Retired. Replica/resource/gateway writer와 Control persistence는 current tree에 없다. 일반 process health,
metrics와 safe log는 Runtime operations가 소유한다.

## Persisted format과 external API

Retired. Control schema, encrypted generation, mutation receipt와 source-admin HTTP route는 지원하지 않는다.
과거 database를 drop하거나 mutate하는 작업은 이 repository change에 포함되지 않았으며, 남은 data의
보존·폐기는 별도 target/scope/rollback 승인을 받아야 한다.

## 역사 기록 안내

- [과거 persistence/recovery link target](persistence-and-recovery.md)
- [과거 observability link target](observability.md)
- [ADR 0030](../../decisions/0030-git-reviewed-yaml-source-authority.md)
- [현재 Source Catalog](../source-catalog/README.md)
- [현재 Runtime](../runtime/README.md)

Control Plane을 다시 도입하려면 새 요구, authority·persisted format·external API·lifecycle·ownership과
migration/rollback 영향을 제시하고 사용자 승인을 받은 새 ADR/change set이 필요하다. 이 tombstone을
구현 specification으로 사용하지 않는다.
