---
name: query-man-admin
description: Repository-scoped Query Man source/database onboarding and read-only server inspection. Use only when explicitly invoked as $query-man-admin in this repository; do not use for business-data queries or dynamic server inventory mutation.
---

# Query Man Admin

Use this skill only from the repository root that contains this file at
`.agents/skills/query-man-admin/SKILL.md`. Follow the root `AGENTS.md` and the accepted ADRs. This
skill prepares Git-reviewed repository artifacts and inspects an already-running server. It is not
the source authority, a deployment controller, or a business-data query tool.

Before handling a token, certificate, private key, Kubernetes Secret, credential provider, or
protected environment, read [credential-safety.md](references/credential-safety.md). Never ask the
user to paste a secret.

## Inspect a server

Read `docs/modules/delivery/README.md` and `docs/operations.md`. Use the bundled helper so bearer
tokens do not appear in command arguments:

```bash
python3 .agents/skills/query-man-admin/scripts/query_man_request.py ready
python3 .agents/skills/query-man-admin/scripts/query_man_request.py status
python3 .agents/skills/query-man-admin/scripts/query_man_request.py metrics
python3 .agents/skills/query-man-admin/scripts/query_man_request.py sources
python3 .agents/skills/query-man-admin/scripts/query_man_request.py meta <source-id>
```

Set `QUERY_MAN_SERVER_URL`. `ready` is unauthenticated; every other command requires exactly one
approved operator-token source described in the credential reference. Use only existing read-only
endpoints. Do not dynamically mutate or reload inventory, extract Kubernetes secrets, or connect
directly to PostgreSQL.

## Add a source

Read `docs/source-extension-checklist.md`, ADR 0034, and ADR 0035. Confirm the source ID, existing
database profile, reader, exposed schema/view, public descriptions, owner, environment, migration
reference, view contract, and budget. Add only:

- `config/sources/<source-id>/source.yaml`
- `config/sources/<source-id>/views.sql`

Use `config/sources/query-cave/` only as a structural reference; do not copy Gotham identifiers.
Do not invent columns, meanings, PII classifications, grants, or migration evidence. Never add
credentials, DSNs, real data, certificate content, or runtime secret paths to a source package.
Explain that a merged package takes effect after a reviewed restart; do not claim live admission.

Validate versioned packages without reading `.env`, process credentials, certificate files, or a
database:

```bash
uv run python .agents/skills/query-man-admin/scripts/validate_source_packages.py
```

## Add a database profile

Read `docs/database-certificate-authentication.md`, ADR 0036, and the database-profile section of
`docs/source-extension-checklist.md`. Create or modify `config/database-profiles.yaml` version 1. Production
profiles use `verify-full` and client-certificate authentication.

Do not store secret values, secret-store identifiers, certificate bodies, private keys, passwords,
or DSNs. Report the external credential-directory work that remains, but do not issue, copy,
inspect, mount, or apply credentials. Stop before any protected action unless the user has approved
the exact environment, target, executor, scope, stop condition, and change-record responsibility.

## Work with Query Cave

Read `query-cave/README.md`. Use it as the synthetic onboarding and CI reference. Status checks
are read-only. Run `./scripts/query-cave.sh up` or `./scripts/query-cave.sh down` only when the user
explicitly asks to change the local Query Cave lifecycle.

## Validate and hand off

Run the repository-local validator above and the tests selected by
`docs/source-extension-checklist.md`; use the full repository gate when changing shared behavior.
Summarize repository changes, remaining external credential or database work, required approvals,
and test results. Never describe a repository-only change as deployed or live. The retired `qm` CLI
must not be recreated.
