# ADR 0025: Static Non-RLS First Launch

Status: Accepted

Decision ID: `LAUNCH-01-A`

## Context

첫 protected launch에서 동적 source 관리, RLS tenant routing, 다양한 PostgreSQL/result type과
multi-replica coordination을 함께 열면 검증해야 할 권한·drift·cleanup 경계가 너무 넓어집니다.

## Decision

### Static inventory

Runtime은 process 시작 때 reviewed `config/sources/` package 전체와 budget YAML을 load합니다. Package
형식은 [ADR 0034](0034-source-view-package-and-direct-admission.md), inventory 발견 규칙은
[ADR 0035](0035-reviewed-source-package-inventory.md), budget authority는
[ADR 0030](0030-git-reviewed-yaml-source-authority.md)이 정합니다. 이 ADR은 source 이름이나 별도 등록
목록을 복제하지 않습니다.

Runtime source 추가·reload와 application의 DDL 실행은 지원하지 않습니다. 변경은 Git review, 필요한
DBA apply와 process 재배포로 반영합니다.

### Non-RLS only

Manifest와 live catalog에서 RLS를 사용하거나 tenant isolation을 요구하는 source는 DB query 전에
fail-closed합니다. 현재 access policy는 caller/source authorization만 제공하며 row-level tenant
분리를 대신하지 않습니다.

### PostgreSQL compatibility

- `180000 <= server_version < 190000`
- `server_encoding == "UTF8"`, `client_encoding == "UTF8"`
- DB profile의 `verify-full` client certificate와 exact database/session user
- Minimum-privilege reader, no role switch와 허용 view-only access
- Query transaction의 `REPEATABLE READ READ ONLY`, UTC와 bounded session setting

Reader의 database `TEMP` privilege 자체는 admission 조건이 아니지만 SQL AST가 `SELECT INTO`, write,
`pg_temp`와 temp-object 생성을 거부합니다. 상세 경계는 [ADR 0032](0032-reader-temp-admission-relaxation.md)를
따릅니다.

### Result policy

성공 결과는 다음 exact PostgreSQL OID만 허용합니다.

| Type | OID |
|---|---:|
| `int8` | 20 |
| `int2` | 21 |
| `int4` | 23 |
| `text` | 25 |
| `date` | 1082 |
| `timestamptz` | 1184 |
| `numeric` | 1700 |

`numeric`은 decimal 문자열, `date`와 UTC-normalized `timestamptz`는 canonical ISO 문자열로 반환합니다.
Unsupported OID나 encoding failure는 내부 세부 없는 `QUERY_UNAVAILABLE`로 fail-closed합니다.

### Serving freeze

Metadata revision이 포착하지 못하는 privileged DDL, function/operator/collation, extension이나 semantic
database setting drift가 있으므로 serving 중 다음을 바꾸지 않습니다.

- Source view/function/operator/type/collation/extension
- Reader role membership, grant와 database/role/server setting
- Result OID/canonical encoding과 SQL policy

변경은 신규 admission 차단, traffic drain, review·검증, process 교체와 rollback 준비가 있는 별도
change-set으로 수행합니다.

## Rollout과 rollback

Protected 실행은 repository 승인과 별도입니다. Approved commit/image, source package, DB target, reader,
secret reference, stop condition과 change-record 책임을 먼저 확정합니다. Traffic 밖에서 direct admission,
negative privilege, query limit, cancel·rollback과 redaction을 검증한 뒤 단일 replica로 전환합니다.

RLS, version/encoding, marker, reader privilege, revision, result OID 또는 cleanup 불일치가 있으면 전환하지
않습니다. Rollback은 신규 admission 차단, 활성 query drain/cancel, 직전 image/config/route 복원과 필요한
DBA view/grant 복구 순서입니다.

## Consequences

지원 범위는 작지만 policy를 prompt나 caller 관례에 맡기지 않고 gateway와 PostgreSQL에서 강제할 수
있습니다. RLS, result type 확대, live reload와 multi-replica shared quota는 별도 승인·검증 없이는
추가하지 않습니다.
