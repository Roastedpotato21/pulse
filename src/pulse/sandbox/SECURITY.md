# Pulse Secure Sandbox — Security Architecture

## Isolation Models

The Pulse Sandbox provides multiple layers of defense to execute untrusted code safely. 
We categorize security controls into Prevention, Detection, and Redaction.

### Prevention (Isolation)
- **Containerization**: `DockerBackend` enforces process isolation, filesystem isolation, and network isolation using namespaces and cgroups. 
- **Remote Execution**: `RemoteSandboxBackend` enables untrusted code to run on a dedicated remote host via the `pulse-remote` worker daemon. This physical/network separation prevents any host escape from affecting the local development machine, with communication secured via authenticated WSS.
- **Secret Injection**: Secrets are explicitly injected into the container environment via temporary `--env-file` structures stored outside the workspace. This prevents argv leakage (e.g. `ps aux`) and ensures robust lifecycle cleanup.
- **Copy-on-Write Filesystem (CoW)**: Changes are strictly staged via CoW transactions. If explicitly authorized secrets are detected in staged files, the commit is outright rejected by the security engine to prevent secret persistence in snapshots.
- **Path Validation**: TOCTOU-safe path verification prevents directory traversal and symlink escapes.

### Detection (Auditing)
- **Structured Audit Logging**: All security boundary interactions (reads, writes, network connections, execution) are logged securely in `audit.jsonl` with their resolved `isolation_level`.

### Redaction (Scrubber)
- **SecretScrubber**: In-memory regex and exact-value matching redaction engine scrub sensitive keys from standard output, errors, and audit logs. All regex patterns are optimized to prevent ReDoS.

## Known Limitations & Unsupported Scopes

- **Host Execution Fallback**: Running `HostBackend` is intrinsically unsafe for untrusted code execution. By default, the sandbox requires an explicit opt-in (`unsafe_host_execution=True`) which is audited and warned against.
- **Process Group Termination Escapes**: On POSIX, a child process running directly on the host can escape `killpg()` termination by calling `setsid()`. This underscores the mandatory requirement for container-level isolation boundaries.
- **Scoped Secret Access**: Providing per-network-request secret injection (Scoped Secrets) is **currently unsupported**. Given that network isolation and environment variable capabilities are decoupled, attempting to implement scoped secrets without strong OS/ebpf-level enforcement can lead to a false sense of security. Secrets injected into a container are accessible to any process within that container. 
