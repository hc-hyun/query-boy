# ADR 0036: Database-Scoped Client Certificate Profiles

Status: Accepted

Decision ID: `DATABASE-CERTIFICATE-PROFILE-01`

## Context

Source manifest마다 같은 physical database endpoint, TLS mode와 credential reference를 반복하면 한 DB의
source 수만큼 secret과 Compose mapping을 관리하게 됩니다. 동일 Query Man process에 source별 private
key를 모두 mount해도 process 침해 경계는 나뉘지 않으므로 operational cost에 비해 격리 효과가 작습니다.

## Decision

`config/database-profiles.yaml` version 1이 physical database endpoint와 authentication의 Git-reviewed
authority입니다. Production profile은 `sslmode: verify-full`과 `authentication.type:
client-certificate`만 사용합니다. Runtime credential root 아래의 profile-ID directory는 exact
`ca.crt`, `client.crt`, `client.key`를 제공하며 private material은 Git/YAML/image에 넣지 않습니다.

Source package는 exact two-file layout을 유지하되 manifest version 6에서 connection detail과
`password_env`를 제거하고 `database_profile`, `reader_user`만 참조합니다. 여러 source가 profile 하나와
client certificate 하나를 공유할 수 있고, PostgreSQL certificate DN mapping과 source별 reader grant가
reader identity를 제한합니다. Source inventory authority는 계속 `config/sources/`의 immediate child
package 전체입니다. Database profile은 source registration 목록이 아닙니다.

Metadata와 Guarded Query pool은 동일한 resolved certificate parameters를 사용합니다. Certificate
profile은 CA/hostname 검증을 약화할 수 없으며 연결 실패는 startup admission을 fail-closed합니다.
Disposable test fixture만 명시적인 password profile과 `disable` TLS를 사용할 수 있습니다.

## Consequences

기존 physical DB에 source를 추가할 때 repository 변경은 source package 두 파일이고 protected 변경은
reader/view grant와 DN mapping입니다. Compose, database profile과 certificate는 수정하지 않습니다. 새
physical DB를 추가할 때만 database profile, certificate directory, PostgreSQL trust/HBA와 rotation
책임을 추가합니다.

Pool은 계속 source별이므로 하나의 physical DB를 공유해도 query/metadata connection budget은 source
budget의 합으로 검토합니다. DB profile 단위 pool sharing은 현재 승인 범위가 아닙니다.

## Migration과 rollback

Version 5 source manifest는 version 6으로 원자적으로 변환하며 mixed inventory를 허용하지 않습니다.
Protected 환경은 certificate trust/DN mapping과 credential mount를 traffic 밖에서 먼저 준비하고 새
image 전체 inventory admission을 확인한 뒤 전환합니다. 실패하면 traffic을 연결하지 않고 직전
version-5 image/config와 password credential delivery로 rollback합니다. Repository merge는 certificate
발급, DB HBA reload 또는 protected cutover 권한이 아닙니다.

발급, mount, negative probe, rotation과 rollback 절차는
[Database client certificate guide](../database-certificate-authentication.md)를 따릅니다.
