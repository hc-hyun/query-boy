# Query Passport consumer

Use an operator-installed, approved `query-passport` executable. The helpers require tool version
`0.4.0`, contract major `1`, policy `mvp-local-lifecycle-2`, and the selected advertised capability:
`profile.inspect.v1`, `plan.offline.v1`, `connection.verify.v2`, `lifecycle.local.v2`, or
`credential.rotate.local.v2`. They are repository skill resources; the Query Man application has no
Passport runtime dependency. Select an approved installation with `--passport PATH` when needed.
Do not inspect credential wrappers or install an unreviewed executable to work around a failure.

Admin exposes only `capabilities`, `inspect`, and `plan`. DBA exposes `verify`, `prepare`, `issue`,
`apply`, `deliver`, `rotate`, and `status` within its existing Execute authorization.
Neither helper creates executor bindings or accepts an approval flag. An alias or operation ID is
not execution authority. Passport's live backends support only advertised `local-synthetic` and
`local` environments; `protected` remains an offline planning label. Approval does not add a backend.

Version 0.4.0 has no rollback or automatic compensation. Its operation plan v2 and delivery owner v2
do not migrate or resume 0.3.0 records/stores. Use a new operator-managed delivery path and preserve
existing credentials, backups and records. A successful connection alone does not authorize deleting them.

## Public request

First run the Query Man profile/inventory validation in the Admin skill. Project only public literal
fields into a user-designated public JSON request. Do not read environment values, credential paths,
certificate files or bindings to fill it. Preserve profile names that Passport cannot support and
report the limitation; do not rename a profile or substitute an endpoint to make it pass.

This synthetic base request is not a target binding or execution approval:

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

Use aliases and facts already established for the task. `source_count`, including zero, is caller
context and does not assert a verified inventory. This projection does not change database profile
v1 or source v6. Unresolved environment-backed endpoints remain unresolved until their authorized
owner supplies public facts.

Run from the Query Man repository root. Public filenames are relative to `--workspace`:

```bash
python3 .agents/skills/query-man-admin/scripts/query_passport.py capabilities
python3 .agents/skills/query-man-admin/scripts/query_passport.py inspect --workspace /approved/public/workspace --request request.json
python3 .agents/skills/query-man-admin/scripts/query_passport.py plan --workspace /approved/public/workspace --request request.json
```

`--request -` accepts the same bounded public JSON through stdin. The helper does not open request
files itself; Passport enforces its file boundary. It checks capabilities before forwarding a request,
bounds stdout to 16 KiB and stdin to 64 KiB, and rejects stderr, invalid JSON, unknown fields/checks,
mismatched command/status/exit and unsupported versions. Subprocess deadlines are 10 seconds for
offline commands, 70 for verification and 190 for lifecycle commands, including cleanup margin beyond
Passport's advertised 5/60/180-second limits. Only validated metadata and fixed codes are returned;
provider messages, credential contents and internal paths are discarded.

## Authorized verification and preparation

The DBA skill's existing Execute approval must cover the exact target, executor, access method, scope,
stop/rollback conditions and environment record responsibility. Within that scope:

```bash
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py verify --workspace /approved/public/workspace --request request.json
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py prepare --workspace /approved/public/workspace --request prepare.json
```

The original invocation without a command still means `verify`. `prepare.json` contains the base
request plus optional `"intent": "provision"` or `"intent": "rotate"`; provision is the default.
`prepare` reads the bound live target and creates a local operation record. It is not an offline plan
and does not issue credentials or authorize subsequent changes. The default DBA Plan mode must not
call it without live access authorization.

Review `prepare`'s `operation_id`, `plan_digest`, `intent`, `actions`, `preserves`, `account`, `client_dn`
and `certificate_lifetime_days`. The lifetime is the requested maximum and can be bounded by the CA.
Provision plans cover client issuance, a restricted check role, appended client trust, owned auth
rules and verified credential publication. Rotation plans cover new client issuance, fresh connection
verification and credential publication. These operations do not create business sources or readers.

The operator independently provides the valid binding and credential-aware executor. An unavailable or
invalid binding returns `AUTHORIZATION_REQUIRED`/exit 6. Stop and report the missing mechanism; do not
write bindings, request secret values, switch endpoints or weaken authentication to obtain success.

## Execute the reviewed local operation

After approval covers the exact prepared change, copy the returned opaque identifiers into the base
request as the following `operation` object. Do not include `intent` in execution requests:

```json
{
  "operation": {
    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "plan_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
```

The values above illustrate the shape only. Use the actual returned identifiers without editing the
plan digest. `operation.json` below contains the complete base request plus that object:

```bash
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py issue --workspace /approved/public/workspace --request operation.json
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py apply --workspace /approved/public/workspace --request operation.json
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py deliver --workspace /approved/public/workspace --request operation.json
```

Call each step only within its approved scope. `issue` creates client material in the PKI boundary;
`apply` changes the owned local authentication resources; `deliver` publishes only after runtime
permission checks, positive TLS/client-identity checks, certificate-free/plaintext server refusals and
a fresh positive connection pass. This does not test timeout or cancellation recovery.
Credentials and CA private keys never pass through this helper.

For an approved rotation, first `prepare` with `intent: rotate`, then use its operation object:

```bash
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py rotate --workspace /approved/public/workspace --request operation.json
```

Rotation reuses the owned CA identity and issues a new client key/certificate. Old credential
generations remain preserved; rotation does not revoke old certificates or remove DB authorization.

After a failure or interruption, stop the mutation and inspect the last recorded progress:

```bash
python3 .agents/skills/query-man-dba-onboarding/scripts/query_passport_verify.py status --workspace /approved/public/workspace --request operation.json
```

`status` reads recorded progress and is not a fresh connection check. A start record does not prove
that external changes completed or were undone. Passport provides no rollback command. After fixing
the cause, an operator may authorize repeating the same command with the same operation reference;
partial files, conflicts and target drift still stop execution. Do not automatically retry a mutation
after a timeout or delete partial files to force it through. Preserve previous credentials, backups
and logs, and use the environment owner's recovery procedure when needed.

## Interpret the result

- `inspect`/`validated` confirms public projection validation. Query Man repository validation is
  still required. `plan`/`planned` is offline with `executable: false` and DB/TLS facts `not_checked`.
- `prepare`/`planned` means a bound target was inspected and a local plan recorded. Connectivity,
  authentication and certificate validation remain `not_checked`; approval is a separate fact.
- `verify`/`succeeded` requires its seven recognized checks to pass: runtime identity, credential
  permissions, read-only credential mount, TLS, client identity, target and read-only transaction.
  Statement-timeout, timeout-recovery, cancellation, cancellation-recovery and reconnect diagnostics
  from 0.3.0 are not performed. Query Man application acceptance must still run its own required checks.
- `issue`, `apply`, `deliver` and `rotate` report only their exact successful phase.
  `deliver`/`rotate` phase `verified` carries fresh DB/TLS/authentication success. Other steps do not.
- `status`/`validated` returns the last recorded `phase`, with
  DB/TLS/authentication `not_checked`, even when the stored phase is `verified`.
- Failure after a valid operation request preserves only opaque operation identifiers, a fixed error,
  and `outcome: not_confirmed`. It does not provide a recovery action or last confirmed external state.
  Never infer the last committed phase or readiness from a failed, timed-out or malformed response.

Source count zero remains valid database-only context. Source inventory, reader rights, source
admission, deployment and application readiness remain `not_checked`; application startup, HTTP
authentication and business-query acceptance remain separate Query Man work. Record approved live
outcomes in the existing environment record. Passport's local operation log is not protected immutable
evidence, and repository test success is not activation evidence.
