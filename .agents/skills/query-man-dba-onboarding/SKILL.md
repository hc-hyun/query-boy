---
name: query-man-dba-onboarding
description: Plan and, with separate execution authorization, guide protected PostgreSQL onboarding for Query Man databases and sources. Use only when explicitly invoked as $query-man-dba-onboarding in this repository; do not use for repository-only source authoring or unapproved live database changes.
---

# Query Man DBA Onboarding

Use this skill only from the repository containing it at
`.agents/skills/query-man-dba-onboarding/SKILL.md`. Treat the repository's `AGENTS.md`, accepted
ADRs, `docs/source-extension-checklist.md`, `docs/database-certificate-authentication.md`, and
`docs/operations.md` as the authorities. If they disagree, stop and report the conflict.

This skill has two modes:

- **Plan** is the default. Inspect only versioned repository artifacts and produce a secret-free
  execution packet. Do not connect to PostgreSQL, PKI, Kubernetes, or a secret provider.
- **Execute** is available only after the user separately approves the exact protected environment,
  target, executor, access method, scope, traffic-off window, stop condition, rollback owner, and
  append-only change-record location. Repository approval or creation of this skill is not execution
  approval.

Before any credential or live-environment work, read
[credential-boundary.md](references/credential-boundary.md). Before preparing or running a DBA
change, read [execution-checklist.md](references/execution-checklist.md). Never ask the user to paste
a password, token, certificate, private key, Secret value, or connection string.

## Prepare the execution packet

1. Pin the approved commit and identify the exact source package and database profile.
2. Run the `$query-man-admin` repository-local source validator and inspect the source's
   `source.yaml` and reviewed `views.sql`.
3. Resolve only non-secret facts: target database, source reader, dedicated `NOLOGIN` view owner,
   curated schema/views, approved client-certificate DN/fingerprint, Query Man egress CIDR, and
   source budget limits.
4. Separate cluster-wide work (database creation, certificate trust, `pg_hba.conf`, `pg_ident.conf`)
   from transactional database work (roles, curated schema, reviewed view, revoke/grant).
5. Produce positive and negative acceptance checks plus a rollback plan. Do not store protected
   environment facts or execution evidence in this repository unless an accepted authority explicitly
   requires that exact artifact.

Do not copy Query Cave names or broad development rules into a real target. Query Cave is a
disposable structural reference only.

## Execute after authorization

Use only the user's approved credential wrapper, broker session, bastion, or non-secret PostgreSQL
service alias. Let the human operator enter an unavoidable interactive credential outside chat and
agent-visible output. Never discover credentials or convert them into environment variables,
arguments, files, or shell text.

Perform a read-only identity preflight first. Confirm the exact PostgreSQL 18/UTF-8 target and stop
on any target, dependency, existing-role, privilege, certificate, HBA, or output mismatch.

- Create a database only when its non-transactional cluster-level action was separately approved.
  Never auto-drop it as rollback.
- Create new roles without passwords. The reader is `LOGIN` with a positive approved connection
  limit and no elevated attributes; the view owner is `NOLOGIN`. If either role already exists with
  unexpected ownership or attributes, stop instead of altering it into compliance.
- Apply the exact approved `views.sql` with `ON_ERROR_STOP` while traffic is off. Do not patch SQL at
  the console and continue.
- Have the authorized PostgreSQL configuration owner apply exact DN-to-reader and narrow
  database/reader/egress-CIDR HBA rules. Validate rule parsing and order before reload. Never add
  plaintext or password fallback.
- Run the approved positive and negative probes. Do not select or print business rows merely to prove
  access.

If any step fails, stop admission, rollback the current transaction where possible, and follow the
approved owner-specific rollback. Do not delete a database, role, certificate, trust rule, or prior
evidence automatically.

## Hand off

Record only secret-free evidence in the approved append-only environment record: commit, target
identity, certificate fingerprint/expiry, applied artifact digest, role/grant and HBA outcomes,
positive/negative probe results, executor, timestamps, and rollback disposition. Never claim runtime
admission until the separately approved Query Man traffic-off startup and readiness checks pass.
