# Query Passport consumer

Use an operator-installed, approved `query-passport` executable. These helpers support tool version
`0.2.0`, contract major `1`, and the advertised `profile.inspect.v1`, `plan.offline.v1`, and
`connection.verify.v1` capabilities. They are repository skill resources; the Query Man application
does not import or require Passport. Select an already approved installation with `--passport PATH`
when it is not on `PATH`. Do not inspect credential wrappers or install an unreviewed executable to
work around a failure.

The Admin helper supports only `capabilities`, `inspect`, and `plan`. The DBA helper supports only
`verify`, within the DBA skill's existing Execute authorization. Neither helper creates executor
bindings or accepts an approval-bypass flag. An alias is context, not evidence of authority.
Passport 0.2.0 supports live checks only in its advertised `local-synthetic` and `local` environments;
`protected` remains an offline planning label. A protected Execute approval does not add a backend.

## Public request

First run the existing Query Man profile/inventory validation in the Admin skill. Project the selected
profile's public literal fields into a user-designated public JSON request; do not read runtime
environment values, credential paths, certificate files, or bindings to fill it. Passport has a narrower
supported name subset: if the validated Query Man profile is unsupported, report that limitation and
preserve the original profile. Do not rename it to make the tool accept it.

This synthetic example is not a target binding or execution approval:

```json
{
  "contract_version": "1",
  "profile_version": 1,
  "scope": "database-only",
  "environment": "local-synthetic",
  "deployment_alias": "sample-deployment",
  "target_alias": "sample-db",
  "profile": {
    "id": "sample-db",
    "host": "sample-db",
    "port": 5432,
    "database": "query_man",
    "sslmode": "verify-full",
    "authentication": {"type": "client-certificate"}
  },
  "source_count": 0
}
```

Use aliases and environment facts already established for the task. `source_count` is caller context,
including zero; it does not assert a verified inventory. This projection does not change database
profile v1 or source v6. An unresolved environment-backed endpoint must remain unresolved until its
authorized owner supplies public facts; the skill must not discover credentials or process settings.

Run from the Query Man repository root, passing a public `.json` relative to `--workspace`:

```bash
python3 .agents/skills/query-man-admin/scripts/query_passport.py capabilities
python3 .agents/skills/query-man-admin/scripts/query_passport.py inspect --workspace /approved/public/workspace --request request.json
python3 .agents/skills/query-man-admin/scripts/query_passport.py plan --workspace /approved/public/workspace --request request.json
```

`--request -` accepts the same bounded public JSON through stdin. The helper does not open request
files itself; Passport enforces its workspace/file boundary. It checks capabilities before forwarding
the request, bounds stdout to 16 KiB and request stdin to 64 KiB, and rejects raw stderr, invalid JSON,
unknown fields/checks, mismatched command/status/exit, and unsupported versions. Failures expose only
classified codes. No free-form provider error or internal path is copied to the result.

## Authorized live verification

After the DBA skill's existing Execute approval covers the exact target, executor, access method,
read-only verification scope, stop/rollback conditions, and environment record responsibility:

```bash
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py --workspace /approved/public/workspace --request request.json
```

The operator must independently provision the approved Passport executor binding and credential-aware
execution environment. `AUTHORIZATION_REQUIRED`/exit 6 means that binding is unavailable or invalid.
Stop and report the missing mechanism; do not write a binding, request a secret value, change aliases,
switch endpoints, or relax authentication to obtain a successful result. The helper itself is not an
authorization service. Issuance, HBA/ident changes, credential delivery, and rotation remain separate
actions under their existing owners and scopes.

## Interpret the result

- `validated` means public input validation. It does not replace Query Man's repository validation.
- `planned` means an offline plan with `executable: false`. DB/TLS/identity observations remain
  `not_checked`; a plan is not an execution approval or a live target snapshot.
- `succeeded` for `verify` requires every recognized check to pass: runtime identity, credential
  permissions and read-only mount, TLS/client identity/target, read-only transaction, timeout/recovery,
  cancellation/recovery, and reconnect. Any missing, unknown, failed, or pending check prevents success.
- Even successful database verification leaves `source_inventory`, `query_man_validation`,
  `reader_permissions`, `source_admission`, `deployment`, and `application_readiness` as `not_checked`.
  Source zero remains valid database-only context; source admission, HTTP authentication, application
  startup, and business queries are separate Query Man acceptance work.

Report the fixed failure code once and stop the requested operation on failure. Do not describe
partial checks, a helper exit failure, or a capability response as database/application readiness.
Record actual approved live outcomes only in the existing environment record, not as repository
activation evidence. Removing or replacing prior tools/credentials requires the separate verified
cutover and rollback conditions; this consumer does not perform those actions.
