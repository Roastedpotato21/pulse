"""Custom exception hierarchy for Pulse sandbox security failures.

Provides structured, catchable error types for sandbox unavailability,
security boundary violations, and resource limit breaches.
"""

from __future__ import annotations


class SandboxUnavailableError(RuntimeError):
    """No secure execution backend (Docker/Podman) is available.

    Raised when the sandbox cannot find a container engine and the caller
    has NOT explicitly opted into unsafe host execution.

    Security rationale:
        Silent fallback to host execution is a catastrophic isolation failure.
        This exception forces callers to make a conscious, auditable decision
        about running untrusted code directly on the host.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "No secure container backend (Docker/Podman) is available. "
                "Set unsafe_host_execution=True to explicitly allow host execution "
                "(NOT recommended for untrusted code)."
            )
        )


class SandboxUnsupportedPolicyError(SandboxUnavailableError):
    """The requested security policy cannot be strongly enforced by the active backend.
    
    Security rationale:
        Fail-closed behavior is mandatory. If a restrictive network or isolation policy
        is requested but the backend lacks the OS-level capability to enforce it
        (e.g., trying to use ALLOWLIST in rootless Docker without egress filtering,
        or DENY_ALL in HostBackend), execution must be rejected rather than silently
        downgraded to advisory enforcement.
    """


class SandboxSecurityError(RuntimeError):
    """A sandbox security boundary has been violated.

    Raised on TOCTOU detection, symlink escape attempts, path traversal
    after validation, or any operation that breaches isolation invariants.

    Security rationale:
        Hard failure prevents partial-state exploitation. Every security
        violation is terminal for the current operation.
    """

    def __init__(self, message: str, *, operation: str = "", path: str = "") -> None:
        self.operation = operation
        self.path = path
        detail = f" [op={operation}]" if operation else ""
        detail += f" [path={path}]" if path else ""
        super().__init__(f"{message}{detail}")


class SandboxResourceError(RuntimeError):
    """A sandbox resource limit has been exceeded.

    Raised when file size, output size, or memory constraints are breached.
    """

    def __init__(self, message: str, *, limit_name: str = "", limit_value: int = 0) -> None:
        self.limit_name = limit_name
        self.limit_value = limit_value
        super().__init__(message)


class SandboxConcurrentModificationError(RuntimeError):
    """A target file was modified externally during a CoW transaction.

    Raised when ``commit_transaction()`` detects that a file's identity,
    size, mtime, or content has changed since the transaction first staged
    it.  The transaction is NOT destroyed when this error is raised, so
    callers may retry or inspect staged changes.

    Attributes:
        path: Relative workspace path of the conflicting file.
        reason: Human-readable description of the detected change.
    """

    def __init__(self, message: str, *, path: str = "", reason: str = "") -> None:
        self.path = path
        self.reason = reason
        super().__init__(message)


class SandboxRecoveryError(RuntimeError):
    """A fatal error occurred during CoW transaction recovery.
    
    Raised when a WAL replay detects path traversal attempts, malformed data,
    or irreconcilable concurrency conflicts (where a file was modified externally
    while the system was offline).
    """
    
    def __init__(self, message: str, *, path: str = "", reason: str = "") -> None:
        self.path = path
        self.reason = reason
        super().__init__(message)
