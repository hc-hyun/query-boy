# ADR 0035: Reviewed Source Packages Define Startup Inventory

Status: Accepted

Decision ID: `SOURCE-INVENTORY-01`

## Context

Reviewed package 외에 source ID 목록, registration manifest나 source별 acceptance corpus를 두면 source
추가마다 unrelated file을 수정해야 하고 서로 다른 authority가 생깁니다.

## Decision

Every immediate child directory under `config/sources/` is a requested startup source package. 각 directory는
[ADR 0034](0034-source-view-package-and-direct-admission.md)의 `source.yaml`과 `views.sql` 두 파일만 가져야
합니다. No third source-registration file exists. ADR 0036의 database profile은 여러 source가 공유하는
physical connection/authentication authority이며 source inventory가 아닙니다.

Runtime은 package 전체를 strict load하고 하나라도 malformed, unresolved secret, duplicate source ID,
RLS, reader-policy 또는 live direct-admission failure이면 ready가 되지 않습니다. 모든 인증된 query
principal은 현재 reviewed source 전체를 사용할 수 있고 access policy는 query/operator capability를
구분합니다.

Tests verify behavior, not a duplicate inventory. Generic registry, view-SQL, HTTP와 startup test는 discovered
package의 completeness, ordering, public projection과 redaction을 검사하며 production source 이름·개수를
별도 expected list로 복제하지 않습니다. Real-DB safety kernel은 one tiny synthetic database만 사용합니다.

## 변경과 rollback

새 package merge는 requested inventory 변경이며 protected activation은 separately authorized입니다.
DB/data owner와 DBA apply, exact image/source revision, traffic-off admission과 rollback을 확인한 뒤 process를
교체합니다. Package 제거도 caller 영향과 DB view retention을 별도로 승인합니다.

Startup failure나 live DB 불일치에서는 일부 source만 제공하지 않고 직전 package/image revision으로
rollback합니다. Git history는 rewrite하지 않습니다.
