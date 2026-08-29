# Source Management Plane

Status: Retired — historical pointer only

The Control DB-backed managed source management plane was removed by
[ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md). Git-reviewed
`config/sources/*.yaml`, `config/verified-queries.yaml` and budget configuration are now the only runtime
authorities. There is no supported managed mode, source-admin API, hot reload, replica convergence or usage
reporter.

This file keeps commonly cited historical anchors resolvable. It is not an executable procedure and must not
be used to reconstruct or operate the retired control plane. The implementation and its former documentation
remain available in Git history at baseline
`7b4e717c7775ff262c716d36f6f172aadc162892` and in immutable historical ADR/evidence.

No live Control DB was dropped or mutated as part of the repository removal. Any external database retention or
decommissioning requires a separate protected operation with an exact inventory, target, access scope, backup,
rollback and execution approval. A future DB-backed authority requires a new ADR and migration/cutover plan.

## Current Management Operations

Retired. `qm source list/show/validate` reads local reviewed YAML and does not expose an admin mutation surface.

## Mutation Request And Receipt Wire Format And Semantics

Retired. The former admin wire contract and receipts are no longer supported interfaces.

## Timeout Reconciliation

Retired. There is no managed mutation to reconcile. A YAML change is reconciled through Git review, deployment
verification and a reviewed Git revert when rollback is needed.

## Storage Shape

Retired. Runtime does not connect to or recover a Control DB. Historical persisted-format details remain in Git
history only and do not authorize live database access or deletion.

## Rollout Checklist

Use the active source onboarding and operations documents for YAML review, traffic-off validation, deployment
and rollback. Do not execute retired Control DB scripts or endpoints.

## Release Acceptance

Historical acceptance proved only the former implementation at its recorded commit and environment. It does
not describe the current serving surface.

## Explicitly Deferred

A DB-backed authority is not an inactive switch. It can return only through a separately approved ADR covering
authority, data migration, secrets, compatibility, recovery, cutover and rollback.
