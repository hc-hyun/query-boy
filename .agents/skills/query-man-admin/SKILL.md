---
name: query-man-admin
description: Repository-scoped Query Man source/database onboarding and read-only server inspection. Use only when explicitly invoked as $query-man-admin in this repository; do not use for business-data queries or dynamic server inventory mutation.
---

# Query Man Admin

Locate the repository root containing this file at `.agents/skills/query-man-admin/SKILL.md` and run
the commands below from that root, even if the request starts in a subdirectory. Use this skill only
in that repository. Follow the root `AGENTS.md` and the accepted ADRs. This
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

Use `query-cave/config/sources/query-cave/` only as a structural reference; do not copy Gotham identifiers.
Do not invent columns, meanings, PII classifications, grants, or migration evidence. Never add
credentials, DSNs, real data, certificate content, or runtime secret paths to a source package.
Explain that a merged package takes effect after a reviewed restart; do not claim live admission.

Validate the versioned manifest/profile references and desired SQL separately, without reading `.env`,
process credentials, certificate files, or a database:

```bash
uv run python .agents/skills/query-man-admin/scripts/validate_source_packages.py
uv run pytest tests/test_registry.py tests/test_source_view_artifacts.py
```

The helper validates configuration and the two-file layout; the tests validate `views.sql` contents
for both Query Cave and discovered production packages. The repository gate supports the current
empty production inventory and reviewed source additions. The runtime and helper still fail closed
when no source exists. Do not add a source-name list or change tests for each new source.

## Add a database profile

Read `docs/database-certificate-authentication.md`, ADR 0036, and
`docs/source-extension-checklist.md`. A database profile can be registered before any source exists.
For a database-only request, create or modify only `config/database-profiles.yaml` version 1;
do not invent a source, reader, schema, view, or live database creation task.

### Collect non-secret information first

Offer this form before asking for connection information. Reuse facts already supplied and ask only
for missing values; confirm the port rather than assuming the example applies:

```yaml
profile_id: <Query Man profile ID>
host: <PostgreSQL hostname matching its server certificate>
port: 5432
database: <actual PostgreSQL database name>
```

Accept these non-secret fields in chat or a user-designated non-secret file. The profile ID is not
the database name: hyphens are valid in a profile ID, while the current database-name schema requires
`[A-Za-z_][A-Za-z0-9_$]{0,62}`. Do not silently rename an unsupported database name or substitute
placeholder endpoints. Use the supplied literal host/port; add `host_env` or `port_env` only when an
explicitly requested or existing deployment mapping needs them.

If known, collect environment, PostgreSQL version/encoding, and client-certificate readiness as
optional planning context. They are not database-profile fields, and unknown readiness does not
prevent repository-only registration. Later runtime admission requires PostgreSQL 18/UTF8.
Production profiles use `sslmode: verify-full` and `authentication.type: client-certificate`.

No DBA login, password, authenticated session, or certificate file is needed to author this profile.
If live DBA work is requested later, use `query-man-dba-onboarding` and its credential boundary:
the user authenticates through an approved mechanism outside agent-visible input/output and supplies
only a non-secret service alias or approved wrapper invocation, never the credential itself.

Do not store secret values, secret-store identifiers, certificate bodies, private keys, passwords,
or DSNs. Report the external credential-directory work that remains, but do not issue, copy,
inspect, mount, or apply credentials. Stop before any protected action unless the user has approved
the exact environment, target, executor, scope, stop condition, and change-record responsibility.

### Validate a profile before the first source

With no production sources, validate the actual repository profile using the existing inventory test:

```bash
uv run pytest tests/test_registry.py::test_repository_production_source_inventory_uses_discovered_packages
```

This checks the database-profile schema and repository certificate policy even with zero sources.
It does not prove endpoint reachability, environment override resolution, certificate validity, or
live admission. Complete the full repository gate from `docs/development-guidelines.md` as well.
The source-package helper and runtime still reject zero sources by design; do not change that
behavior or add a dummy source to make them succeed. If sources already exist, also run the helper
and SQL tests from the source workflow. Report profile registration and source count separately.

### Inspect or plan with Query Passport

When Passport is installed and the request needs its public configuration check or offline plan,
read [query-passport.md](references/query-passport.md). After the repository validation above, use
`scripts/query_passport.py` for `capabilities`, `inspect`, or `plan`; it checks the supported version
and capability and returns only validated, bounded JSON judgments. Pass public profile fields and
existing aliases, including `source_count: 0` when applicable. Keep unsupported names unchanged.
Admin never calls Passport `verify` or credential/change commands. Offline `validated`/`planned`
results do not establish DB connectivity, certificate validity, source admission, or app readiness.

## Work with Query Cave

Read `query-cave/README.md`. Use it as the synthetic onboarding and CI reference. Status checks
are read-only. Run `./scripts/query-cave.sh up` or `./scripts/query-cave.sh down` only when the user
explicitly asks to change the local Query Cave lifecycle.

## Validate and hand off

Use the database-only validation path above when no sources exist. Otherwise run the repository-local
validator and the tests selected by `docs/source-extension-checklist.md`. Complete the full repository
gate required by `docs/development-guidelines.md`.
Summarize repository changes, remaining external credential or database work, required approvals,
and test results. Never describe a repository-only change as deployed or live. The retired `qm` CLI
must not be recreated.
