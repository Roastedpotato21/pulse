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
