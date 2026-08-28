# Pulse Operations Runbook

## Supported release boundary

Version 0.1.0 is an alpha, local single-user CLI release. The supported
artifact is the `pulse-coding-agent` Python distribution and its loopback
JSON-RPC server. The remote worker is available for controlled evaluation but
is not approved for multi-tenant or unattended production use.

The VS Code extension is source-only in this release. It compiles in CI but is
not published, and its legacy stdio client is not a supported transport.

## Preflight

From the target project, run:

```text
pulse doctor --production --target local
```

Exit code `0` means every blocking local check passed. Exit code `2` means the
table contains at least one release blocker. Automation should use
`--json` and inspect the top-level `passed` field.

For a controlled remote-worker evaluation, configure absolute durable paths,
a cryptographically random token, and mTLS, then run:

```text
pulse doctor --production --target remote --json
pulse-remote --host 0.0.0.0
```

The remote doctor requires Docker or Podman, a token of at least 32
non-placeholder characters, an absolute tenant workspace root, an absolute
SQLite path, bounded concurrency/retention, and all three mTLS files for a
non-loopback host. `pulse-remote` enforces the same token policy unless
`--development` is explicitly used on loopback.

## Health and readiness

The remote worker exposes HTTP responses on its WebSocket listener:

- `/healthz` reports process liveness.
- `/readyz` returns `503` until the Docker worker and durable execution store
  have initialized, then returns `200`.

On non-loopback deployments the TLS handshake requires a trusted client
certificate before these endpoints are reachable. Do not expose them directly
to the public internet.

## State inventory

| State | Default location | Backup requirement |
| --- | --- | --- |
| Tasks | `.pulse/tasks.sqlite3` or task-manager store | Back up before upgrade |
| Conversations and memory | `.agent/*.sqlite3` | Back up before upgrade |
| Sessions | `.pulse/sessions/` | Back up before upgrade |
| Mutation/audit/telemetry logs | `.agent/logs/` | Retain according to project policy; logs can contain source snapshots |
| Remote execution store | `PULSE_REMOTE_DB` | Durable volume and periodic SQLite backup |
| Remote tenant workspaces | `PULSE_REMOTE_WORKSPACE_ROOT` | Ephemeral; do not treat as the source of record |

Never copy a live SQLite database file without coordinating its WAL. Prefer
the SQLite online backup command:

```text
sqlite3 SOURCE.sqlite3 ".backup 'BACKUP.sqlite3'"
```

Verify backups with `PRAGMA integrity_check;`, encrypt them, and test restore
on a separate path. Pulse does not yet provide a unified backup command, so
operators own scheduling and retention.

## Upgrade and migration

1. Stop Pulse and remote workers; allow active executions to finish or record
   them for reconciliation.
2. Back up every store in the state inventory and record the running Pulse
   version and artifact SHA-256.
3. Install the new wheel into a new environment; do not overwrite the known
   good environment.
4. Run `pulse doctor --production`, then the artifact entry-point smoke tests.
5. Start one canary workspace. Confirm health/readiness, task recovery, audit
   output, and provider access before broader rollout.

The remote execution store migration is idempotent and records SQLite
`user_version=2`. Other stores do not yet share a unified migration contract;
that remains a hosted-deployment blocker in `PRODUCTION_CHECKPOINTS.md`.

Pulse is an application distribution, so its direct runtime dependencies are
pinned to the versions exercised by CI and recorded in the release SBOM.
Upgrades to those pins require a new Pulse release and the complete release
gate; do not loosen them in a production environment.

## Rollback

1. Stop the failing process. Do not automatically replay executions with an
   unknown external outcome.
2. Reinstall the previous wheel by exact version or SHA-256-verified artifact.
3. Restore the pre-upgrade database backup only if the older version cannot
   read the migrated store. Preserve the failed database for investigation.
4. Restart on loopback, run the production doctor, and reconcile
   `RECOVERY_PENDING` or remote `UNKNOWN` executions manually.

PyPI releases are immutable. A bad public release must be yanked, not replaced;
publish a new patch version after verification.

## Credential rotation

1. Generate a new random remote token and distribute it through the secret
   manager, never through Git or command-line history.
2. During a bounded overlap window, configure old and new tokens as a
   comma-separated set, restart the worker, and move clients to the new token.
3. Remove the old token and restart again. Check redacted audit logs for failed
   uses of the retired credential.
4. Rotate mTLS certificates and provider/OAuth credentials at their issuers if
   exposure is possible.

## Incident response

Contain the worker, revoke credentials, preserve the execution database and
redacted logs, and quarantine ambiguous tasks. Do not paste raw model output,
source snapshots, access tokens, or tenant artifacts into tickets. Record the
release SHA, correlation ID, task ID, remote execution ID, and mutation
transaction ID. See `SECURITY.md` and `src/pulse/sandbox/SECURITY.md` for the
threat boundary.

## Release procedure

1. Complete the release checklist in `PRODUCTION_CHECKPOINTS.md`.
2. Update `CHANGELOG.md`; make version values agree in `pyproject.toml` and
   `pulse.__version__`.
3. Run the full local release verification and push the commits.
4. Wait for required CI checks on the release commit, including live Docker
   security tests.
5. Create tag `vX.Y.Z`, then publish a GitHub Release for that tag.
6. The release workflow rebuilds and verifies the same source, uploads the
   tested artifact between jobs, and publishes to PyPI through OIDC trusted
   publishing. It also attaches SHA-256 checksums, a source-bound manifest, and
   a CycloneDX SBOM to the GitHub Release.

Before step 5, configure the `pypi` GitHub environment and a PyPI trusted
publisher for `.github/workflows/release.yml`. Public publishing also requires
the owner to choose and add a project license; this repository currently does
not grant one.
