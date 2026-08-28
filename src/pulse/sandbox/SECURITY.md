# Pulse Secure Sandbox — Security Architecture

## Isolation Models

The Pulse Sandbox provides multiple layers of defense to execute untrusted code safely. 
We categorize security controls into Prevention, Detection, and Redaction.

### Prevention (Isolation)
- **Tool Authorization**: Model-produced tool arguments are validated against
  typed schemas before execution. `ToolPolicyEngine` evaluates a per-runtime
  capability allowlist, workspace scope, risk level, and approval requirement.
  Unknown capabilities, malformed arguments, and paths outside the configured
  workspace are rejected before a tool receives them.
- **Containerization**: `DockerBackend` enforces process isolation, filesystem isolation, and network isolation using namespaces and cgroups. 
- **Remote Execution**: `RemoteSandboxBackend` enables untrusted code to run on a dedicated remote host via the `pulse-remote` worker daemon. This physical/network separation prevents any host escape from affecting the local development machine, with communication secured via authenticated WSS.
- **Secret Injection**: Secrets are explicitly injected into the container environment via temporary `--env-file` structures stored outside the workspace. This prevents argv leakage (e.g. `ps aux`) and ensures robust lifecycle cleanup.
- **Copy-on-Write Filesystem (CoW)**: Changes are strictly staged via CoW transactions. If explicitly authorized secrets are detected in staged files, the commit is outright rejected by the security engine to prevent secret persistence in snapshots.
- **Path Validation**: TOCTOU-safe path verification prevents directory traversal and symlink escapes.

### Detection (Auditing)
- **Structured Audit Logging**: All security boundary interactions (reads, writes, network connections, execution) are logged securely in `audit.jsonl` with their resolved `isolation_level`.
- **Policy Decision Logging**: Every central tool-policy decision records the
  subject, capability, risk, and reason. Raw tool argument values are excluded
  so audit records cannot become a second secret store.

### Redaction (Scrubber)
- **SecretScrubber**: In-memory regex and exact-value matching redaction engine scrub sensitive keys from standard output, errors, and audit logs. All regex patterns are optimized to prevent ReDoS.

## Known Limitations & Unsupported Scopes

- **Host Execution Fallback**: Running `HostBackend` is intrinsically unsafe for untrusted code execution. By default, the sandbox requires an explicit opt-in (`unsafe_host_execution=True`) which is audited and warned against.
- **Process Group Termination Escapes**: On POSIX, a child process running directly on the host can escape `killpg()` termination by calling `setsid()`. This underscores the mandatory requirement for container-level isolation boundaries.
- **Scoped Secret Access**: Providing per-network-request secret injection (Scoped Secrets) is **currently unsupported**. Given that network isolation and environment variable capabilities are decoupled, attempting to implement scoped secrets without strong OS/ebpf-level enforcement can lead to a false sense of security. Secrets injected into a container are accessible to any process within that container. 

## Threat Model and Incident Response

Pulse treats provider output, tool arguments, downloaded artifacts, and remote
worker messages as untrusted. The intended deployment boundary is a dedicated
container or remote worker; host execution is development-only and explicitly
unsafe. The system is designed to contain path traversal, shell injection,
archive attacks, unrestricted network egress, credential leakage, stale remote
results, and cross-tenant remote execution access.

If a remote token, client certificate, or provider credential may be exposed:

1. Revoke the affected credential at its issuer and replace the configured
   `PULSE_REMOTE_TOKEN` or TLS material on both worker and clients.
2. Restart remote workers to terminate active sessions; inspect the durable
   execution store and `audit.jsonl` for the affected tenant and execution IDs.
3. Quarantine ambiguous tasks through recovery rather than retrying them with a
   new external ID, then rotate any secrets mounted into the affected worker.
4. Preserve redacted audit logs and the worker version/configuration for
   incident review. Do not copy raw execution output into tickets.

Remote workers must use `wss://` with mTLS outside loopback, have a dedicated
tenant-isolated workspace volume, enforce a finite execution retention period,
and expose health checks only on an authenticated operations network.
