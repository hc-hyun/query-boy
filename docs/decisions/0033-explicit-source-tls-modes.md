# ADR 0033: Explicit Source TLS Modes

Status: Accepted

Date: 2026-08-31

Decision ID: `QB-SOURCE-TLS-MODES-20260831`

Baseline: `0e488cbaf75f2c8e25080179eb9e381d3f0aeaf4`

This decision supersedes only ADR 0030's compatibility statement that preserved the then-current source manifest
version and reader transport policy. Git-reviewed YAML remains the sole source authority and all other ADR 0030
boundaries remain in force.

## Context

Source manifest version 2 stores `connection.ssl` as a boolean. `false` or an omitted field makes Metadata and
Guarded Query pass `sslmode=disable`; `true` makes both pass `sslmode=verify-full`. This cannot represent a native
PostgreSQL endpoint that rejects non-TLS connections while its CA or requested hostname is not yet compatible with
`verify-full`.

The requester reported a sanitized probe result with the required compatibility shape: `disable` was rejected,
`require` completed reader authentication and a read, and `verify-full` failed at CA/hostname verification. This
report motivates the repository capability but is not protected-environment acceptance evidence. Using libpq's
`prefer` default would not express the requirement because it can fall back to a plaintext connection.

## Decision

- Source manifest version 3 replaces boolean `connection.ssl` with a required `connection.sslmode` whose exact
  allowed values are `disable`, `require` and `verify-full`.
- `prefer`, `allow`, `verify-ca`, omission and unknown values are rejected during Source Catalog loading.
- Source Catalog resolves the reviewed value into the immutable source profile. Metadata and Guarded Query pass
  that exact value to libpq for every source pool; they do not reinterpret it or delegate the choice to ambient
  `PGSSLMODE`.
- Source hosts must identify TCP endpoints; Unix-domain and abstract socket paths are rejected because libpq
  ignores `sslmode` for Unix sockets. Both pools set `gssencmode=disable` so GSS encryption cannot take precedence
  over the reviewed TLS mode. This disables GSS-encrypted transport, not GSSAPI authentication over the reviewed
  TLS or plaintext transport.
- Before any SQL, every pool checkout compares libpq's actual TLS state with the reviewed mode and fails closed if
  `require`/`verify-full` is not using TLS or if `disable` unexpectedly is using TLS.
- `disable` only attempts a non-TLS connection. `require` only attempts a TLS connection and never falls back to
  plaintext, but does not verify the requested server hostname. `verify-full` requires TLS, a trusted CA chain and
  a certificate matching the requested hostname.
- `verify-full` remains the preferred protected-environment posture. Each use of `require` needs reviewed source
  inventory that records the reason, owner, accepted hostname-identity risk and the CA/SAN remediation condition
  for moving to `verify-full`.
- A root CA file or `PGSSLROOTCERT` remains protected deployment inventory. libpq can validate the CA chain in
  `require` mode when a root CA file is present, so acceptance uses the exact application artifact and environment.

The transport mode is not metadata or SQL-policy revision material. It changes connection security and
availability, not the allowed catalog, SQL semantics or canonical query result. HTTP/MCP request and response
formats do not change.

## Compatibility And Migration

Version 2 manifests are not accepted by the new runtime and no dual-schema fallback is added. Repository-owned
source manifests and domain-lab fixtures migrate atomically to version 3. Their explicit `ssl: false` becomes
`sslmode: disable`, preserving their current local non-TLS behavior.

Any separately reviewed source directory must migrate every manifest before deploying the new artifact. A mixed
version directory fails startup validation. Adding a `require` source remains a separate inventory change with its
own source, reader, no-PII view, verification and deployment approval.

An existing source that depends on a Unix socket or GSS-encrypted transport is not compatible with version 3 and
must not be silently translated. No current repository source has either dependency.

## Security And Operational Impact

`require` protects connection credentials and query traffic from passive network observation and forbids
plaintext fallback. It does not authenticate the requested hostname and therefore does not provide the MITM
protection of `verify-full`. It is an explicit compatibility exception, not an alias for a generally verified
connection and not permission to weaken any reader, SQL, budget, cancel, rollback or secret boundary.

Repository acceptance does not authorize a protected connection, credential change, route or deployment. The
actual target probe and cutover require separately approved access, exact target and artifact, TLS/CA/secret
inventory, stop conditions, rollback and append-only environment evidence. Full DSNs, passwords, private keys and
internal database errors are not recorded in repository artifacts or public logs.

This change performs no database write or data migration. Query transactions remain read-only.

## Rollback

Rollback deploys the previous application artifact together with its version 2 manifests. It does not translate a
version 3 `require` source to `ssl: false` or silently choose `prefer`; a source that the previous artifact cannot
represent remains out of service. Any new source manifest is reverted through reviewed Git history before the
previous artifact is started.

## Verification

- Source Catalog accepts exactly `disable`, `require` and `verify-full`, rejects version 2, missing and unsupported
  modes, and does not expose credentials in validation errors.
- Metadata and Guarded Query pool tests prove that each resolved mode is passed unchanged to libpq with
  `gssencmode=disable`; Source Catalog rejects non-TCP socket hosts; checkout tests accept only the actual TLS state
  required by the reviewed mode.
- Current source and domain-lab manifests validate under version 3 without changing their non-TLS fixture behavior.
- Focused module tests, Ruff, mypy and the full pytest gate pass before repository handoff.
- A protected `require` source additionally needs traffic-off connection, reader-policy, metadata and guarded-query
  acceptance using the exact deployment artifact and environment.
