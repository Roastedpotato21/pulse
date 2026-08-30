"""Backend-independent resource governance for sandbox executions.

Policies describe limits without referring to a backend.  Backends translate
them to native controls (cgroups for containers and ``resource`` on POSIX),
while :class:`ResourceController` owns the portable execution lifecycle.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no resource module
    resource = None

_linux_prctl: Any | None = None
if sys.platform.startswith("linux"):
    try:  # Resolve libc before forking; importing or loading it in preexec can deadlock.
        import ctypes

        _linux_prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (ImportError, OSError, AttributeError):  # pragma: no cover - unusual libc
        _linux_prctl = None

DANGEROUS_ENV_VARS: frozenset[str] = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "PYTHONSTARTUP", "PYTHONPATH", "PERL5LIB",
    "RUBYLIB", "NODE_OPTIONS", "BASH_ENV", "ENV", "CDPATH",
})


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Immutable, serializable limits applied to one sandbox execution.

    A zero or ``None`` limit means that the corresponding native limit is not
    requested.  Not every operating system exposes every limit; callers can
    inspect backend capabilities without changing their execution code.
    """

    cpu_quota_percent: float = 100.0
    cpu_time_seconds: float | None = None
    memory_bytes: int | None = 1_073_741_824
    swap_bytes: int | None = 1_073_741_824
    disk_bytes: int | None = None
    max_processes: int | None = 64
    max_open_files: int | None = 256
    max_output_bytes: int = 5_242_880
    wall_time_seconds: float = 30.0
    termination_grace_seconds: float = 1.5
    working_directory_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("cpu_quota_percent", "max_output_bytes", "wall_time_seconds", "termination_grace_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("cpu_time_seconds", "memory_bytes", "swap_bytes", "disk_bytes", "max_processes", "max_open_files", "working_directory_bytes"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when set")

    def compose(self, **overrides: Any) -> ResourcePolicy:
        """Return a derived policy, preserving immutability and validation."""
        return replace(self, **overrides)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourcePolicy:
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    """Structured observations collected for a completed execution."""

    elapsed_ms: float
    cpu_time_ms: float | None = None
    peak_memory_bytes: int | None = None
    process_count: int | None = None
    output_bytes: int = 0
    exit_status: int | None = None
    termination_reason: str | None = None


class ResourceLimitExceeded(RuntimeError):
    """A named execution resource limit was reached."""

    def __init__(self, limit_name: str, limit_value: float) -> None:
        self.limit_name = limit_name
        self.limit_value = limit_value
        super().__init__(f"Sandbox resource limit exceeded: {limit_name}={limit_value}")


class TimeoutExceeded(ResourceLimitExceeded):
    """The wall-clock execution deadline elapsed."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__("wall_time_seconds", timeout_seconds)


class ResourceMonitor:
    """Collect portable elapsed/output metrics and POSIX child resource usage."""

    def __init__(self) -> None:
        self._started_at = 0.0
        self._rusage_before: Any | None = None

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._rusage_before = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None

    def finish(self, *, output_bytes: int, exit_status: int, termination_reason: str | None, process_count: int | None = 1) -> ExecutionMetrics:
        cpu_time_ms: float | None = None
        peak_memory_bytes: int | None = None
        if resource and self._rusage_before is not None:
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_time_ms = ((after.ru_utime + after.ru_stime) - (self._rusage_before.ru_utime + self._rusage_before.ru_stime)) * 1000
            # ru_maxrss is KiB on Linux and bytes on macOS.
            peak_memory_bytes = int(after.ru_maxrss * (1 if sys.platform == "darwin" else 1024))
        return ExecutionMetrics((time.monotonic() - self._started_at) * 1000, cpu_time_ms, peak_memory_bytes, process_count, output_bytes, exit_status, termination_reason)


class ResourceController:
    """Coordinates portable timeout, cancellation, output and metrics behavior."""

    def __init__(self, policy: ResourcePolicy | None = None, monitor: ResourceMonitor | None = None) -> None:
        self.policy = policy or ResourcePolicy()
        self.monitor = monitor or ResourceMonitor()

    def sanitize_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        # An explicit environment is authoritative. Merging it with os.environ
        # silently reintroduces host credentials into supposedly isolated jobs.
        merged = dict(os.environ if env is None else env)
        for variable in DANGEROUS_ENV_VARS:
            merged.pop(variable, None)
        return merged

    def make_preexec_fn(self) -> Any | None:
        """Create POSIX native enforcement for host processes."""
        if not resource or sys.platform == "win32":
            return None
        policy = self.policy

        def set_limit(limit: int, value: int | None) -> None:
            if value is not None:
                try:
                    resource.setrlimit(limit, (value, value))
                except (OSError, ValueError):
                    pass

        def preexec() -> None:
            # Keep this callback async-signal-safe in the post-fork child: all
            # imports and dynamic-library loading happen at module import time.
            # PR_SET_PDEATHSIG is 1.
            if _linux_prctl is not None:
                _linux_prctl(1, signal.SIGKILL)

            try:
                if hasattr(resource, "RLIMIT_NPROC"):
                    set_limit(resource.RLIMIT_NPROC, policy.max_processes)
                if hasattr(resource, "RLIMIT_NOFILE"):
                    set_limit(resource.RLIMIT_NOFILE, policy.max_open_files)
                if hasattr(resource, "RLIMIT_AS"):
                    set_limit(resource.RLIMIT_AS, policy.memory_bytes)
                if (
                    hasattr(resource, "RLIMIT_CPU")
                    and policy.cpu_time_seconds is not None
                ):
                    set_limit(
                        resource.RLIMIT_CPU, max(1, int(policy.cpu_time_seconds))
                    )
                if hasattr(resource, "RLIMIT_FSIZE"):
                    set_limit(resource.RLIMIT_FSIZE, policy.disk_bytes)
            except Exception:  # noqa: BLE001 - fail closed at the subprocess boundary
                # subprocess cannot safely propagate rich exceptions from a
                # preexec callback. Exit closed if an unexpected limit setup
                # failure escapes the individual setrlimit guards.
                os._exit(126)

        return preexec


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Legacy-compatible resource limits adapter.

    New integrations should accept :class:`ResourcePolicy`; this type remains
    supported by public APIs introduced in earlier sandbox phases.
    """
    max_memory_bytes: int = 1_073_741_824
    max_cpu_percent: float = 100.0
    max_pids: int = 64
    max_open_files: int = 256
    timeout_seconds: float = 30.0
    max_output_bytes: int = 5_242_880
    max_file_read_bytes: int = 52_428_800
    max_storage_bytes: int | None = None

    def to_policy(self) -> ResourcePolicy:
        return ResourcePolicy(cpu_quota_percent=self.max_cpu_percent, memory_bytes=self.max_memory_bytes, swap_bytes=self.max_memory_bytes, max_processes=self.max_pids, max_open_files=self.max_open_files, max_output_bytes=self.max_output_bytes, wall_time_seconds=self.timeout_seconds, disk_bytes=self.max_storage_bytes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceLimiter:
    """Backward-compatible adapter around :class:`ResourceController`."""
    def __init__(self, limits: ResourceLimits | ResourcePolicy | None = None) -> None:
        self.policy = limits if isinstance(limits, ResourcePolicy) else (limits or ResourceLimits()).to_policy()
        self.limits = limits if isinstance(limits, ResourceLimits) else ResourceLimits(
            max_memory_bytes=self.policy.memory_bytes or 0, max_cpu_percent=self.policy.cpu_quota_percent,
            max_pids=self.policy.max_processes or 0, max_open_files=self.policy.max_open_files or 0,
            timeout_seconds=self.policy.wall_time_seconds, max_output_bytes=self.policy.max_output_bytes,
            max_storage_bytes=self.policy.disk_bytes)
        self.controller = ResourceController(self.policy)

    def make_preexec_fn(self) -> Any | None:
        return self.controller.make_preexec_fn()

    def sanitize_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        return self.controller.sanitize_env(env)

    def truncate_output(self, content: str | bytes) -> tuple[str, bool]:
        raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        encoded = raw.encode("utf-8")
        if len(encoded) <= self.policy.max_output_bytes:
            return raw, False
        return encoded[:self.policy.max_output_bytes].decode("utf-8", errors="ignore") + "\n... [OUTPUT TRUNCATED BY SANDBOX RESOURCE LIMITER]", True
