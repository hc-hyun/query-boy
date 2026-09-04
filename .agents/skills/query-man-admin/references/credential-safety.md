# Credential safety

Apply this checklist before touching authentication or a protected environment.

## Classify the environment first

- Local/private: disposable local credentials may be supplied through an environment variable only
  when the user explicitly identifies the environment as non-corporate and non-protected.
- Corporate: use only the organization's approved credential broker or a read-only mounted token
  file. If neither exists, stop and ask for the approved mechanism, never for the secret value.
- Protected: repository or procedure approval is insufficient. Confirm the exact environment,
  target, executor, scope, stop condition, and append-only change-record owner before acting.

## Keep secrets out of durable and observable surfaces

Git may contain host, port, database name, profile name, authentication requirements, and desired
SQL object names. It must not contain bearer tokens, passwords, DSNs with credentials, private keys,
certificate bodies, secret-store identifiers, or copied Kubernetes Secret values.

Treat `client.key` and bearer tokens as secrets. Treat certificate metadata as restricted operational
information unless the organization says otherwise. Do not ask anyone to paste a secret into chat.

Do not inspect `.env` files, process environments, shell history, credential directories, pod
environments, or Secret data to discover credentials. Avoid broad commands such as `env`,
`printenv`, `set`, `docker inspect`, `kubectl get secret -o ...`, or `kubectl exec ... printenv`.

Never place a token in a URL, command argument, inline HTTP header, temporary file, shell expansion,
or traced shell. Do not disable TLS verification, send credentials over plaintext to a non-loopback
host, or silently fall back to password authentication.

## Authenticate server inspection

The bundled request helper accepts exactly one of these sources:

1. `QUERY_MAN_OPERATOR_TOKEN_FILE` (preferred). An approved provider must create a regular,
   non-symlink file outside the repository and shared temporary directories. It must be owned by the
   current user or root, have no group/other permissions, contain 32-512 visible ASCII bytes, and may
   end with one newline.
2. `QUERY_MAN_OPERATOR_TOKEN`, only for an explicitly local, disposable, non-corporate,
   non-protected environment.

Set `QUERY_MAN_SERVER_URL`. HTTPS is required except for loopback HTTP. If a private CA is needed,
set `QUERY_MAN_SERVER_CA_FILE` to its approved trust-bundle path. The helper builds the Authorization
header in memory, caps response size, and redacts an exact reflected token.

On an authentication or TLS failure, report the normalized failure once. Do not dump request
headers, retry with weaker settings, or reveal credential content.

## Provision database client certificates outside Git

The runtime contract is:

```text
/run/secrets/query-man/databases/<profile>/ca.crt
/run/secrets/query-man/databases/<profile>/client.crt
/run/secrets/query-man/databases/<profile>/client.key
```

The private key must be mode `0600`, or `0640` with a deliberately restricted group. Credentials do
not belong in the repository, image layers, `.env` files, shared temporary directories, CI logs, or
chat. Source/database-profile preparation never opens `client.key`; certificate metadata and PKI
verification are separate, explicitly approved operational checks.

For Kubernetes, resolve the exact context, namespace, and workload before any command. A
user-approved port-forward may be used for read-only server inspection, and must be terminated when
done. Do not retrieve Secret data. Secret creation/mounting, rollout, PostgreSQL HBA changes,
certificate issuance, and DDL are separate protected actions requiring approval.

Never delete or overwrite real credentials. `./scripts/query-cave.sh down` applies only to the
repository's synthetic Query Cave environment and only after an explicit request.
