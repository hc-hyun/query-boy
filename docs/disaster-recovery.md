# Control-Plane Backup And Disaster Recovery

Status: Retired — historical pointer only

The Control DB-backed managed capability and its repository recovery tooling were removed by
[ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md). Query Man runtime no longer connects to,
backs up, restores or migrates a Control DB. Current source definitions, verified queries and budgets recover
from reviewed Git history and a pinned application artifact; source business data remains under each source DB
owner's backup policy.

This tombstone keeps historical links resolvable. It is not a runbook. The former procedure can be inspected in
Git history at baseline `7b4e717c7775ff262c716d36f6f172aadc162892`, but must not be executed against any
environment without a newly approved procedure.

No live Control DB, schema, table, row, credential, secret or backup was dropped or modified by the repository
change. Retention or decommissioning of an external Control DB is a separate protected operation requiring an
exact inventory, target, access scope, retention/backup decision, rollback and execution approval.

## Scope And Targets

Retired Control DB targets are outside current runtime scope. Git repository recovery does not recover source
business data or external secrets.

## Migration

Retired. The former Control DB migration scripts are removed and must not be invoked.

## Backup

Retired. Query Man defines no current Control DB backup job. Existing external backups remain subject to their
own approved retention and access policy.

## Restore

Retired. Restore the current service from an approved Git commit/artifact and external secret configuration,
then validate the reviewed YAML before traffic. Do not restore a Control DB into the current runtime.

## Recovery Drill

Retired. The former Control DB drill is no longer a supported operational procedure.

## Isolated Control Recovery Fixture Acceptance

Historical evidence applies only to the former managed implementation at its recorded baseline. The fixture and
Compose overlay are removed.

## Master-Key Change Boundary

Retired. Current runtime has no Control DB credential-encryption master key. An existing external key must not be
deleted merely because the consuming repository code was removed.

## Future DB-Backed Authority

Reintroduction requires a new ADR and an approved persisted-format, import/migration, credential, backup/restore,
cutover and rollback design. Git-to-DB migration must be explicit; there is no fallback or compatibility mode.
