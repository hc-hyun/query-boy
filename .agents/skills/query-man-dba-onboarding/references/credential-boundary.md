# Credential boundary

Read this before any live database, PKI, Kubernetes, or secret-provider step.

## Credential classes

The workflow has separate authorities; one credential must not be reused as another:

- **DBA session credential:** authenticates the authorized human/operator for database, role, grant,
  and PostgreSQL configuration work. It is not a Query Man runtime credential.
- **PKI issuer authority:** issues or approves the database-scoped Query Man client certificate. The
  skill must not handle the CA private key.
- **Query Man database identity:** `ca.crt`, `client.crt`, and `client.key` mounted read-only under the
  database profile directory. PostgreSQL maps its approved certificate DN to source reader roles.
- **Query Man HTTP tokens:** query/operator API credentials. They are unrelated to DBA login and are
  outside this skill's database execution boundary.

Reader and view-owner roles have no password. The reader authenticates through client-certificate DN
mapping; the view owner is `NOLOGIN`.

## Safe DBA session

Prefer an organization-approved broker, SSO flow, bastion, short-lived admin certificate, or
credential-aware wrapper. The agent may receive a non-secret service alias or wrapper name after the
user confirms it is approved. Do not inspect the wrapper, credential cache, `.pgpass`, service file,
wallet, environment, process table, shell history, or secret store to discover credentials.

Never use or request:

- a password, token, private key, certificate body, Secret value, or credential-bearing DSN in chat;
- `PGPASSWORD`, a password in a connection URI, an inline environment assignment, a command argument,
  a generated shell script, or a temporary plaintext credential file;
- broad `env`, `printenv`, `set`, `docker inspect`, `kubectl get secret -o ...`, or
  `kubectl exec ... printenv` output;
- shell tracing, verbose clients that print headers/connection strings, TLS verification disablement,
  or password fallback.

If an interactive password is unavoidable, the human runs the approved client outside agent-visible
input and output. The agent may continue only from a confirmed authenticated session or a non-secret
approved invocation pattern.

## Certificate handling

The runtime layout is:

```text
/run/secrets/query-man/databases/<profile>/ca.crt
/run/secrets/query-man/databases/<profile>/client.crt
/run/secrets/query-man/databases/<profile>/client.key
```

The skill never reads `client.key`. An explicitly approved read-only certificate-metadata check may
extract only subject DN, issuer, serial, fingerprint, and validity dates from `client.crt`; never emit
the PEM body. Private keys stay outside Git, image layers, environment variables, command arguments,
shared temporary directories, CI logs, and chat. Use mode `0600`, or root ownership with a deliberately
restricted Query Man group and mode `0640`.

Certificate issuance, CA-key access, Secret creation/mounting, and rotation are separately authorized
PKI/deployment actions. If the approved mechanism is unavailable or company controls reject it, stop
and request the mechanism—not the credential value.

## Evidence and failure

Evidence may contain approved non-secret target identity, certificate fingerprint/expiry, role and
grant outcomes, rule-validation results, artifact digests, and timestamps. It must not contain raw
credentials, connection strings, certificate bodies, database rows, or unrestricted command output.

On authentication or TLS failure, report a normalized failure once. Do not dump diagnostics that can
contain secrets, retry with weaker authentication, or move credentials to a more observable channel.
