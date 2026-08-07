"""Resource limits and execution constraints for Pulse sandbox.

Configures memory caps, PID limits, execution timeouts, and output size limits
to prevent resource exhaustion, fork bombs, and infinite loops.

Security hardening (Memory Exhaustion):
    - max_file_read_bytes added for file read size limits.
    - truncate_output() fixed to truncate by byte count, not character count.
    - Dangerous environment variables are stripped from child processes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

# Optional POSIX resource module
try:
    import resource
except ImportError:
    resource = None  # Windows host fallback

# Environment variables that MUST be stripped from child processes to prevent
# library injection, startup script execution, and debug information leakage.
DANGEROUS_ENV_VARS: frozenset[str] = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PERL5LIB",
    "RUBYLIB",
    "NODE_OPTIONS",
    "BASH_ENV",
    "ENV",
    "CDPATH",
})


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Configurable resource constraints for sandbox process execution."""

    max_memory_bytes: int = 1_073_741_824  # 1 GB
    max_cpu_percent: float = 100.0
    max_pids: int = 64  # Prevents fork bombs
    max_open_files: int = 256
    timeout_seconds: float = 30.0  # Execution timeout
    max_output_bytes: int = 5_242_880  # 5 MB stdout/stderr cap
    max_file_read_bytes: int = 52_428_800  # 50 MB file read cap

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_memory_bytes": self.max_memory_bytes,
            "max_cpu_percent": self.max_cpu_percent,
            "max_pids": self.max_pids,
            "max_open_files": self.max_open_files,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_file_read_bytes": self.max_file_read_bytes,
        }


class ResourceLimiter:
    """Enforces execution limits, output truncation, and environment sanitization."""

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()

    def make_preexec_fn(self) -> Any | None:
        """Create a preexec_fn for POSIX subprocesses to enforce OS resource limits."""
        if not resource or sys.platform == "win32":
            return None

        limits = self.limits

        def preexec() -> None:
            # Set NPROC (max child processes / fork bomb protection)
            if hasattr(resource, "RLIMIT_NPROC") and limits.max_pids > 0:
                try:
                    resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_pids, limits.max_pids))
                except (ValueError, OSError):
                    pass

            # Set NOFILE (max open files)
            if hasattr(resource, "RLIMIT_NOFILE") and limits.max_open_files > 0:
                try:
                    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.max_open_files, limits.max_open_files))
                except (ValueError, OSError):
                    pass

            # Set AS (address space / max memory)
            if hasattr(resource, "RLIMIT_AS") and limits.max_memory_bytes > 0:
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (limits.max_memory_bytes, limits.max_memory_bytes))
                except (ValueError, OSError):
                    pass

        return preexec

    def sanitize_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Build a sanitized environment dict, stripping dangerous variables.

        Security rationale:
            LD_PRELOAD, PYTHONSTARTUP, etc. can be used to inject code into child
            processes. Stripping them ensures the sandbox child cannot inherit
            attacker-controlled library paths or startup scripts from the parent.
        """
        import os
        merged = {**os.environ, **(env or {})}

        # Remove dangerous variables
        for var in DANGEROUS_ENV_VARS:
            merged.pop(var, None)

        return merged

    def truncate_output(self, content: str | bytes) -> tuple[str, bool]:
        """Truncate stdout/stderr text if it exceeds max_output_bytes limit.

        Security fix: truncation is now based on encoded byte length,
        not character count, preventing multibyte UTF-8 bypass.
        """
        raw_text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        max_bytes = self.limits.max_output_bytes

        encoded = raw_text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return raw_text, False

        # Truncate at byte boundary, then decode back safely
        truncated_bytes = encoded[:max_bytes]
        truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
        truncated_text += "\n... [OUTPUT TRUNCATED BY SANDBOX RESOURCE LIMITER]"
        return truncated_text, True
