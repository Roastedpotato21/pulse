# Security Policy

## Supported versions

Pulse is pre-1.0 alpha software. Only the latest released version receives
security fixes. The local single-user CLI is the sole supported production
boundary; remote and multi-tenant operation are evaluation-only.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private
source, logs, or tenant artifacts. Use GitHub's private vulnerability reporting
for `Roastedpotato21/pulse`. Include the affected version/commit, deployment
mode, reproducible impact, and the smallest redacted proof necessary.

Until a response SLA is formally staffed, no response-time guarantee is made.
Users deploying Pulse must maintain their own containment and credential
revocation procedures.

## Security boundary

Model output, tool arguments, archives, provider responses, and remote-worker
messages are untrusted. Host execution is development-only. Non-loopback
remote workers require mTLS; production tokens must be random and at least 32
characters. Multi-tenant hosting is unsupported until the Phase 5 checkpoints
in `PRODUCTION_CHECKPOINTS.md` are complete and independently reviewed.

Operational containment and rotation steps are in `OPERATIONS.md`. Detailed
sandbox controls and known limitations are in `src/pulse/sandbox/SECURITY.md`.
