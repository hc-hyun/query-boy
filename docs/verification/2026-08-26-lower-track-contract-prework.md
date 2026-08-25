# Lower-Track Contract Prework Verification

Date: 2026-08-26

Baseline: `ff100735c5d3b6057c7861f85c248a55da13b4a1`

## Scope

이 기록은 아직 우선순위 gate와 exact 사용자 승인을 통과하지 않은 COST/TRACE 계약 초안을 구현 전에
검증한 read-only prework다. Repository production code, numbered migration, source role/function, public wire,
runtime task와 config는 바꾸지 않았다. Proposed ADR의 literal SQL은 고정 이름의 disposable database에만
적용했고 trap으로 database와 role을 제거했다.

## Findings Closed In The Proposal

- COST-01의 database당 source 하나와 source-owner 고정 reader literal은 별도 mapping table 없이 성립한다.
  대문자 reader를 `to_regrole(text)`로 찾던 오류는 exact `pg_roles.rolname` lookup으로 바꿨다.
- Function owner에 `pg_read_all_stats`만 주면 fixture의 revoked view ACL을 통과하지 못했다. Exact 21+2
  column SELECT, `public` USAGE, raw non-reset pgss/info function과 `pg_control_system()` EXECUTE 및 canonical
  `prosrc` attestation을 계약에 추가했다. Stock PUBLIC ACL에서는 ordinary reader도 cluster의 다른
  connectable database, target database TEMPORARY, 두 view와 control
  function에 접근했고, raw pgss function EXECUTE를 revoke하면 column-granted owner의 view SELECT도
  실패했다. 따라서 source DBA의 targeted PUBLIC/object revoke, monitor의 direct denial과 unavoidable raw
  read를 가진 function owner의 NOLOGIN/exact outgoing-readstats/inbound-zero membership containment와
  monitor의 zero-membership containment를 명시했다.
- Migration 6 초안의 non-null lease는 initial pending row에 가짜 owner를 요구했고 일부 CHECK는 SQL NULL을
  허용했다. Epoch 0/null-owner seed, all-or-none lease, monitoring/config fence와 독립 active-metadata FK,
  due index 및 null-safe state matrix로 교정했다. Raw database/reader OID는 baseline에서 제거했다.
- 새 mutation receipt는 과거 strict decoder가 읽지 못하므로 compatibility-reader release → migration →
  writer/route 순서와 rollback floor를 명시했다.
- COST-04의 `accepted_samples>=10`은 whole-hour coverage가 아니라 sample-count heuristic이다. Policy/base
  activation epoch, A→B→A, transition-hour exclusion, gap/no-backfill과 이 한계를 계약에 명시했다.
- Alert event는 exact firing type/policy activation/base epoch를 composite FK로 참조하고 한 firing당 terminal
  event 하나만 허용한다. State/event NULL matrix와 policy transition/wire nullability도 고정했다.
- TRACE-01-A는 policy-admitted route/auth, ASGI-visible header와 wire OWS 한계, nested scope, MCP call/query
  연결, audit/counter와 all-zero omission을 owner 문서까지 맞췄다.

## Disposable PostgreSQL Evidence

### Source projection

PostgreSQL 18.6 disposable source에서 대문자 application reader `ProbeReader`, NOLOGIN function owner와
NOINHERIT monitor를 만들고 proposed info/statement SQL body를 실제 `LANGUAGE SQL SECURITY DEFINER STABLE
PARALLEL RESTRICTED` function으로 생성했다.

- Info는 exact reader OID로 1행을 반환했다.
- Statement projection은 bounded rowset으로 실행됐다.
- Hardened fixture에서 monitor의 direct extension view/raw function access는 없었다.
- Function owner의 relation-level/`query` column direct SELECT는 없고 exact `queryid` column SELECT는
  있었으며, view 실행에 필요한 raw non-reset function EXECUTE는 NOLOGIN owner에만 있었다.
- 두 placeholder body template SHA-256과 canonical definition revision은 documentation test가 재계산한다.

별도 PostgreSQL 18.6 two-database container에서 target/other database를 먼저
`ALLOW_CONNECTIONS=false`로 만들고 PUBLIC CONNECT/TEMP와 relevant object ACL을 harden한 뒤 legitimate
role과 monitor를 grant option 없이 재grant하고 마지막에만 connection을 열었다.

- Monitor의 target wrapper 두 개는 성공했고 target direct view/raw/control access와 other-database
  connection은 실패했다. Legitimate role의 other-database connection은 성공했다.
- Target-only aggregate는 `all=1, other=0, exact_target_only=true`, target TEMP는 false였고 monitor는 target
  owner가 아니었다.
- Monitor의 outgoing/inbound membership edge는 모두 0이었다. Function owner의 outgoing edge는 exact
  `pg_read_all_stats(inherit=true,set=false,admin=false)` 하나, inbound edge는 0이었다.
- Owner의 21+2 column/raw non-reset/control positive와 relation/query/reset negative, relevant direct grant의
  grant-option-false를 확인했다. 임시 `query-man-acl-probe-final-0021` container 제거 뒤 inspect absence도
  통과했다.

### Control migration 6 and 7 proposal

기존 numbered migration 1~5를 새 disposable Control DB에 적용한 뒤 ADR 0021/0023의 literal SQL만 추출해
순서대로 실행했다.

| Probe | Result |
|---|---|
| Base 13 + COST 5 + alert 4 | 22 tables |
| Proposed named `db_*` constraints | 51 |
| Base migration-6 recovery shape | 18 tables, 25 foreign keys, 5 user triggers, 31 named `db_*` constraints |
| Epoch 0, null lease-owner initial usage seed | accepted |
| Complete lease claim and normal owner clear | accepted |
| Same source generation에서 active metadata revision A→B usage-state seed | accepted |
| Accepted attempt without success/baseline | rejected |
| Partial observation identity, lease or freshness | rejected |
| Deferred self-referencing firing event | accepted |
| Resolved event referencing the exact firing epoch | accepted |
| Second terminal event for one firing | rejected |
| Cross-policy activation terminal reference | rejected |
| Firing state with null baseline, normal state with recovery streak, partial ready value | rejected |
| Firing/pending state carrying `COOLDOWN_ACTIVE` outside normal future-cooldown matrix | rejected |

각 실행 뒤 `query_man_cost_contract_probe`, `query_man_monitor_contract_probe` database와
`ProbeReader|probe_monitor|probe_function_owner` role의 잔여 count는 모두 0이었다.

## Runnable Repository Evidence

- `uv run pytest tests/test_documentation.py -q` → `16 passed`
- `uv run pytest -m integration tests/test_source_database_corners.py -k custom_function_body_revision_gap -q`
  → `1 passed, 25 deselected`
- `uv run ruff check .` → pass; `uv run mypy src` → 29 source files, issue 0
- `uv run pytest` → `645 passed, 92 deselected`
- `uv run pytest -m integration` → `80 passed, 657 deselected`
- Same-OID/name/signature function body `false→true`에서 metadata snapshot/revision은 같고 public result/hash만
  달라지는 ENC residual을 고정했다.

## Remaining Gates

- 이 문서는 exact contract 승인이나 production implementation evidence가 아니다.
- ENC-01/ENC-02의 정확한 선택과 final encoding baseline이 먼저다.
- TIME-03은 protected environment inventory, backup, key/artifact, migration reference, fleet drain/route와
  rollback 권한·증거 없이는 수행할 수 없다.
- COST-01-A, COST-04-A와 TRACE-01-A는 각각 ADR의 전체 approval text를 사용자가 정확히 승인하고 priority
  gate가 열린 뒤에만 provider-first로 구현한다.
