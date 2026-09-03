# ADR 0035: Reviewed Source Packages Define Startup Inventory

Status: Accepted — policy and test-local fixture decoupling active

Date: 2026-09-03

Decision ID: `SOURCE-INVENTORY-01`

## Context

ADR 0025 fixed the first repository launch evidence to two named sources. ADR 0034 later made one source an
exact two-file package, but tests and current documents continued to copy the first-launch source count and names.
That duplication makes an otherwise valid source package require unrelated test and documentation edits.

Source review must remain strict without turning tests into another inventory authority. The package itself already
contains the connection, allowlist, semantic and desired-view material that Runtime consumes. A separate source
name list, source-specific expected-result artifact or documentation snapshot adds no enforcement capability.

## Decision

### 1. Reviewed packages are the startup inventory

Every immediate child directory under `config/sources/` is an active source candidate. A reviewed change that adds
`config/sources/<source-id>/source.yaml` and the sibling `views.sql` is the repository inventory review for that
source. No third registration file, source-name list, Python branch, test case or documentation entry is required.

`SourceRegistry.load` continues to fail closed unless every child is an exact ADR 0034 package. Runtime loads the
reviewed directory once at process start; merge does not hot-reload a running process, apply database DDL or grant
protected deployment authority.

### 2. Existing safety and compatibility limits remain

This decision changes inventory identity only. It does not change the manifest v4 format, budget format,
`view_contract_version`, metadata or SQL revision material, external response shapes, reader policy, result
encoding, or shutdown behavior.

Every package must still satisfy all current enforcement:

- RLS sources are rejected.
- PostgreSQL 18/UTF-8 and the reviewed `disable`, `require` or `verify-full` transport are required.
- Only curated views in allowed schemas are published, with marker/source/version and direct semantic admission.
- The source-scoped reader secret, minimum privilege, read-only transaction, resource limits, SQL AST and all
  allowlists, seven result OIDs, cancel and rollback remain unchanged.
- DB/data owner confirmation of the exact no-PII output and the privileged DDL inventory/freeze remain required.

The HTTP and MCP source-list schemas do not change. Their values reflect the packages loaded by the process. Under
the current authorization model every authenticated query principal sees and may query the same active inventory
with each source's reviewed budget. A source that requires caller-specific grants is outside this decision.

### 3. Tests verify behavior, not a duplicate inventory

Repository tests may use named sources as local behavior fixtures, but generic Registry, HTTP, MCP and operator
tests do not assert the production source count or copy the complete source ID list. Package discovery, ordering,
public projection and secret redaction are tested as behavior.

All discovered packages remain subject to strict layout, manifest and `views.sql` AST/marker/owner/ACL validation.
Metadata, revision and SQL safety tests also remain generic. A new source-specific business question, expected
result, database schema or seed is not an onboarding artifact.

The required PostgreSQL, container, load and soak gates use one explicitly selected test-local source package and
one tiny synthetic database. They prove the common PostgreSQL boundary: reader/session policy, view-only
privilege, live catalog admission and drift rejection, result OID/byte limits, cancel, rollback and connection
reuse. They do not apply every production `views.sql` against a repository copy of that source's business schema
or validate business row counts. Actual dependency, output and grant compatibility is checked by the DB owner/DBA
during the separately authorized traffic-off apply and again by Runtime admission.

The test-only source keeps the production password naming policy unchanged. Local/CI wiring derives its internal
fixture password variable from an existing local fixture secret; it does not add a production credential or
source registration requirement.

### 4. Protected activation remains separately authorized

Repository review is not authorization to install a secret, execute `views.sql`, deploy or route traffic. Before a
new package is served, the DB/data owner and DBA still approve the exact target, output, no-PII boundary, reader and
view-owner privileges, TLS/CA, rollback artifact, traffic freeze, stop conditions and append-only change-record
owner. Runtime readiness must probe every package in the deployed inventory successfully before route activation.

## Compatibility And Supersession

- This decision supersedes only ADR 0025's exact two-name source inventory. ADR 0025's single replica,
  PostgreSQL/encoding, RLS quarantine, result OID, SQL/revision/reader safety, privileged DDL freeze and protected
  launch requirements remain current.
- ADR 0034 remains the source-package, desired-view and direct-admission authority.
- ADR 0030 remains the budget authority and retired-managed-capability boundary.
- Existing clients see the same response schemas. Adding a reviewed package adds one advertised/queryable source
  after deployment and restart.
- There is no compatibility mode, fallback authority or runtime mutation path.

## Migration And Rollback

The repository migration removes duplicated exact-count/name assertions, the two source-specific business
databases and their large seeds, and the per-test disposable-database characterization suite. It replaces them
with the shared test-local PostgreSQL safety kernel. Production reader-password handling is unchanged.

Rollback is a normal reviewed Git revert followed by deployment of the previous artifact. Protected rollback also
keeps traffic blocked, drains queries and connections, and restores the previously captured view definition,
comment, owner and ACL when the database change must be reverted. It never automatically deletes base tables,
business rows, secrets or roles.

## Verification

- `qm source validate` accepts every reviewed package and reports the discovered inventory.
- Registry, HTTP, MCP and operator tests prove projection, ordering and redaction without a duplicated source list.
- Every production package passes strict layout/manifest validation and the dynamic desired-view SQL safety gate.
- The test-local source passes the real PostgreSQL safety kernel, container, bounded load and scheduled soak.
- Ruff, mypy and the full pytest gate pass.
- Source-specific database apply and protected deployment evidence remain separate from repository PASS.
