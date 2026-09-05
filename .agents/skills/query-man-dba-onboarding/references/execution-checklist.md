# Protected DBA execution checklist

Use this checklist to prepare a change packet. It preserves the repository's accepted procedure; the
canonical details remain in `docs/source-extension-checklist.md` and
`docs/database-certificate-authentication.md`.

## Authorization record

Do not execute while any field is absent or ambiguous:

```text
approved commit/image:
environment and exact cluster/database:
source ID and database profile:
DB/data owner sign-off and no-PII confirmation:
DBA/PKI/deployment executors:
approved credential mechanism name (no value):
traffic-off window:
scope, including database creation and PostgreSQL config:
backup and rollback owners/artifacts:
stop conditions:
append-only change-record location/owner:
```

Treat database creation, cluster role creation, certificate issuance/trust, HBA/ident editing and
reload, database transaction work, secret mounting, application startup, and traffic cutover as
distinct scopes. Approval for one is not approval for another.

## Repository preflight

- Pin the approved commit; reject local/unreviewed edits for the apply input.
- Validate the complete discovered inventory with
  `uv run python .agents/skills/query-man-admin/scripts/validate_source_packages.py`.
- Validate desired SQL with `uv run pytest tests/test_registry.py tests/test_source_view_artifacts.py`;
  the configuration helper alone does not inspect the SQL body. Complete the repository gate in
  `docs/source-extension-checklist.md` before applying the approved commit.
- Confirm manifest version 6 and database-profile version 1.
- Confirm production `sslmode: verify-full` and `authentication.type: client-certificate`.
- Confirm exact source reader, allowed schema, view-only relation kind, contract version, migration
  reference, and budget.
- Confirm `views.sql` is the reviewed artifact and contains no database/role creation, password, base
  DDL/seed, broad/default grant, or destructive cleanup.
- Obtain DB/data-owner approval of exact output, row meaning, base dependencies, and no-PII status.

## Read-only target preflight

Use the approved session and bounded catalog queries only. Confirm:

- cluster and database identity match the approved profile;
- PostgreSQL major version 18 and server encoding UTF8;
- whether the target database, reader, view owner, curated schema, and target views already exist;
- existing role attributes, ownership, memberships, grants, default privileges, and connection limits;
- source/base dependencies and zero RLS exposure to the reader;
- server TLS hostname/CA expectations and the approved client-certificate DN/fingerprint;
- proposed `pg_ident.conf` mapping and `pg_hba.conf` database, reader, egress CIDR, method, order, and
  absence of a weaker fallback.

Stop on unexpected existing objects or privileges. Do not use an unconditional `ALTER ROLE`, broad
revoke, drop, or overwrite to force the target into shape.

## Change phases

1. **Optional database creation:** execute only under its cluster-level approval. Verify locale,
   encoding, owner, connection policy, backup, and failure cleanup before the non-transactional step.
   Never automatically drop a created database.
2. **Role and schema bootstrap:** create the reader with `LOGIN`, no password, a positive reviewed
   connection limit, and `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`.
   Create the dedicated view owner as `NOLOGIN` with the same negative attributes. Follow the reader
   setup in `docs/database-certificate-authentication.md`, including `temp_file_limit` parameter SET
   privilege and database-local read-only/time/resource defaults derived from the reviewed budget.
   Create only the approved curated schema if absent.
3. **Reviewed view transaction:** while traffic is off, run the exact approved `views.sql` with
   `ON_ERROR_STOP`. It owns exact base dependencies, view ownership, PUBLIC revocation, reader schema
   `USAGE`, view `SELECT`, and source/version comments. Roll back on any error or drift; do not patch at
   the console.
4. **Certificate trust and role mapping:** the PKI/PostgreSQL configuration owners establish the
   approved client chain and exact RFC 2253 DN mapping. HBA rules name the exact database, reader set,
   Query Man egress CIDR, `cert` method, map, and `clientname=DN`. Reject non-TLS and unmatched traffic;
   add no password fallback. Validate `pg_hba_file_rules` and ordering before reload.
5. **Credential delivery:** the deployment owner mounts one database-profile certificate set read-only
   for Query Man. Verify ownership and mode without printing file content.

## Acceptance

Keep traffic off and use the canonical acceptance procedures in full:

- `docs/source-extension-checklist.md`: exact view output/marker/ownership, reader privileges, and
  source admission.
- `docs/database-certificate-authentication.md`: reader parameter privileges and session settings,
  positive/negative certificate and reader probes, and credential delivery.
- `docs/operations.md`, section `2. Traffic-off acceptance`: bounded API query with matching
  revisions, rejected writes/invalid inputs, resource limits, timeout/cancel/rollback, disconnect and
  shutdown cleanup, and connection reuse after recovery. Readiness and operator health are only part
  of this acceptance.

Use bounded, owner-approved probes without printing business rows. Record DBA preparation and full
application acceptance separately; if application checks are outside the execution approval, report
them as pending. Any mismatch blocks traffic.

## Rollback and handoff

- Roll back a failing view/grant transaction immediately.
- Keep new admission and traffic disabled.
- Restore prior view definitions/grants, application revision, certificate trust, and routing only by
  the approved owner in the approved reverse order.
- Do not automatically drop databases/roles, revoke certificates, remove trust, or edit prior evidence.
- Append secret-free actual outcomes and rollback disposition to the approved change record. A repo
  validation pass is not protected activation evidence.
