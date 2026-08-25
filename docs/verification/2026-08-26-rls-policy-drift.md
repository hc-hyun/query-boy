# RLS Policy Drift Security Finding — 2026-08-26

Status: Open — contract decision and fail-closed implementation required

## Why This Is Not Accepted Current Behavior

[ADR 0014](../decisions/0014-trusted-rls-tenant-context.md)는 authenticated tenant를 transaction-local
`query_man.tenant_id`로 전달하고, `row_security=on`, restricted `NOBYPASSRLS` reader와
`security_invoker=true` view를 확인해 다른 tenant의 행이 보이지 않도록 결정했다. 실제 코드는 이
공개 view와 reader/session 표면은 확인하지만, view가 읽는 private base relation의 RLS enablement와
`pg_policy` 의미를 metadata revision 또는 query-time admission에서 attest하지 않는다.

따라서 아래 결과는 호환성을 위해 보존할 current contract가 아니라 accepted tenant-isolation
contract를 위반하는 보안 결함이다. 누출 결과를 통과 기대값으로 고정하지 않는다.

## Independent PostgreSQL 18.6 Reproduction

두 개의 UUID disposable database에서 독립적으로 다음 경계를 확인했다.

- Reader: `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOINHERIT`, 필요한 schema `USAGE`와
  base/view `SELECT`만 보유
- Source: `tenant_isolation=rls`, `allowed_schemas=(analytics,)`, relation kind `view`
- Public view: `security_invoker=true`
- Base table: tenant column, RLS enabled, reader 대상 policy가
  `tenant_id = current_setting('query_man.tenant_id', true)`를 강제
- Query: 실제 `QueryService.query(..., tenant_id=<authenticated tenant>)` 경로
- Baseline: 요청 tenant의 행만 반환

다음 두 mutation을 각각 실행했다.

```sql
ALTER POLICY tenant_filter ON private.tenant_records USING (true);
```

```sql
ALTER TABLE private.tenant_records DISABLE ROW LEVEL SECURITY;
```

두 경우 모두 public view definition, `security_invoker`, reader privilege, `row_security=on` session
probe, `CatalogSnapshot`과 metadata revision은 그대로였다. 그러나 같은 authenticated tenant query가
다른 tenant 행까지 성공 응답으로 반환했다.

한-row/two-row 최소 corpus의 exact hash는 다음과 같다.

```text
tenant-only: sha256:e31870f73f15d65fbe67fa0b6e57f9b36d130e8dba5340c0fd2e3cad1b455270
cross-tenant: sha256:6432f5ac9d37fc70b8d0b5caa12a95340df62491a9e5b994cb36d16014b17f70
```

각 probe 종료 뒤 connection/database/role residue는 모두 `0`이었다.

## Runnable Sentinel

[`test_rls_source_requires_base_policy_drift_to_preserve_isolation`](../../tests/test_source_database_corners.py)은
policy 완화와 RLS disable을 각각 별도 disposable DB parameter로 실행한다. 두 parameter 모두 다른
tenant 행이 성공 결과가 되어서는 안 된다는 안전 기대값을 `strict=True` xfail로 남기며, 알려진
cross-tenant 결과를 확인한 전용 `_RlsIsolationViolationError`만 XFAIL로 인정한다. Setup/query/cleanup의
다른 예외는 숨기지 않는다. 현재 suite에서 두 case는 모두 `XFAIL`이어야 하며 `PASS`로 세지 않는다.
정확한 provider, fingerprint/admission 위치와 public error는 아직 승인되지 않았으므로 임의의
`METADATA_UNAVAILABLE` 또는 `QUERY_UNAVAILABLE` 계약을 테스트에 선결정하지 않았다.

## Required Decision Before Implementation

`RLS-01`은 다음을 하나의 exact proposal로 확정해야 한다.

1. Published security-invoker view에서 private/nested dependency를 어디까지 재귀적으로 추적할지
2. Base relation의 `relrowsecurity`, `relforcerowsecurity`, owner와 reader-applicable policy의 command,
   permissive/restrictive composition, target role, `USING`/`WITH CHECK`를 어떤 canonical identity로
   admission할지
3. Custom function/operator와 nested owner-rights view를 거부할지 또는 transitive하게 attest할지
4. Metadata publish/revision과 같은 read-only transaction의 query-time drift check를 어떻게 연결하고
   concurrent DDL race를 어떤 lock/order로 닫을지
5. Drift의 public error, cache/stale 금지, managed snapshot migration, current/rollback reissue와 rollback

이 범위는 Metadata snapshot/revision, Guarded Query admission/error와 Control Plane publish/cutover에
영향을 주는 module contract 변경이다. 사용자가 정확한 proposal을 승인하기 전 제품 코드, schema,
snapshot codec 또는 production route를 변경하지 않는다.

Protected environment의 RLS source inventory, mutation freeze와 unverified route drain은 안전상 우선
조치지만 이 repository 세션에는 production DSN, source 목록, 권한, route/drain 또는 change record가
제공되지 않았다. 따라서 production이 안전하다고 주장하거나 외부 상태를 변경하지 않았다.
