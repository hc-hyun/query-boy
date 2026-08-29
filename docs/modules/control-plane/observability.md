# Control Plane Managed Observability — Retired

Status: Retired historical pointer; no current writer, projection or storage

[ADR 0030](../../decisions/0030-git-reviewed-yaml-source-authority.md)에 따라 managed replica/resource/gateway
관측 구현과 Control persistence가 제거됐다. 이 파일은 과거 문서 link target만 보존한다. 현재 운영
상태는 Runtime의 bounded health/metrics/safe log를 사용하며 source authority는 Git-reviewed YAML이다.

## 한눈에 보는 세 관측

Retired: replica convergence, DB resource samples와 gateway usage rollup은 current capability가 아니다.

## Public writer interface

Retired. Public observation writer interface는 없다.

## Replica observation (`CTRL-06`)

Historical anchor only. 당시 의미는 관련 ADR/evidence의 baseline commit에서 확인한다.

## Resource observation (`CTRL-07A` + `CTRL-08`)

Historical anchor only. PostgreSQL comment/type/precision-scale metadata 수집은 현재 Metadata module에
남아 있지만 managed resource sample이나 PII authority를 뜻하지 않는다.

## Gateway usage (`CTRL-07A` + `CTRL-08`)

Historical anchor only. Control DB usage rollup/reporter는 current tree에 없다.

## `source_usage` application result (`CTRL-08`)

Historical anchor only. 해당 external/admin projection은 제공하지 않는다.

## Privacy, failure isolation과 non-goal

현재도 credential, token, Authorization header, SQL literal과 내부 DB error를 log에 기록하지 않는다.
그 안전 규칙은 active Runtime/Delivery/Guarded Query 문서가 기준이다.

## 변경 중단 조건과 검증

Managed observability를 다시 도입하는 것은 새 interface, persisted format, privacy policy, lifecycle,
ownership과 운영 절차 변경이다. 과거 문서를 수정해 되살리지 말고 새 승인 ADR/change set을 만든다.
