"""Restricted host process execution backend.

Fallback backend that executes processes directly on the host using ProcessManager
and PathValidator when container engines are unavailable.

Security hardening (unsafe host fallback):
    - Marked as is_unsafe=True to distinguish from container execution.
    - Every execution logs an UNSAFE_HOST isolation level warning.
    - network_enabled parameter is now respected (blocks via policy, not enforcement).
    - This backend should ONLY be used when the caller explicitly opts in
      via unsafe_host_execution=True on the Sandbox constructor.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from pulse.sandbox.network import NetworkEnforcementLevel, NetworkMode, NetworkPolicy
from pulse.sandbox.path_validator import PathValidator
from pulse.sandbox.process import ProcessEnforcementLevel, ProcessManager, ProcessResult
from pulse.sandbox.resources import ResourceLimits
from pulse.sandbox.secrets import (
    SecretEnforcementLevel,
    SecretMode,
    SecretPolicy,
    build_isolated_environment,
)

# SecurityWarning is not available in all Python builds; define a fallback.
try:
    _SecurityWarning = SecurityWarning  # type: ignore[name-defined]
except NameError:
    class _SecurityWarning(UserWarning):  # type: ignore[no-redef]
        """Fallback warning class for security-sensitive operations."""


class HostBackend:
    """Restricted host execution fallback engine.

    WARNING: This backend provides NO container isolation. All commands
    execute directly on the host machine. It exists only as an explicitly
    opt-in fallback for development environments where Docker/Podman
    is unavailable.

    Security properties:
        - is_unsafe=True: always reports itself as unsafe.
        - PathValidator enforces workspace directory containment for cwd.
        - ResourceLimiter enforces POSIX rlimits (memory, PIDs, files) where available.
        - Environment is sanitized (dangerous vars stripped).
        - NO filesystem isolation, NO network isolation, NO capability dropping.
    """

    name = "host"
    is_unsafe: bool = True

    def __init__(self, process_manager: ProcessManager | None = None) -> None:
        self.process_manager = process_manager or ProcessManager()

    async def reconcile(self) -> None:
        """HostBackend processes are reaped by the OS; no orphan reconciliation needed."""

    async def is_available(self) -> bool:
        """Host backend is always available."""
        return True

    def get_network_enforcement_capability(self, policy: NetworkPolicy) -> NetworkEnforcementLevel:
        """Determine what level of security this backend can enforce for the policy.
        
        HostBackend has NO network isolation capabilities. It cannot strongly enforce
        ANY restrictive policy mode.
        """
        if not policy or policy.mode == NetworkMode.ALLOW_ALL:
            return NetworkEnforcementLevel.STRONGLY_ENFORCED
        return NetworkEnforcementLevel.UNSUPPORTED

    def get_secret_enforcement_capability(self, policy: SecretPolicy) -> SecretEnforcementLevel:
        """Determine what level of security this backend can enforce for the policy.
        
        HostBackend has NO filesystem/environment isolation capabilities from the host user.
        It cannot strongly enforce DENY_ALL or ALLOW_EXPLICIT because arbitrary code
        can read ~/.ssh or ~/.aws.
        """
        if not policy or policy.mode == SecretMode.ALLOW_ALL:
            return SecretEnforcementLevel.STRONGLY_ENFORCED
        return SecretEnforcementLevel.UNSUPPORTED

    def get_process_containment_capability(self) -> ProcessEnforcementLevel:
        """Determine if this backend provides strong process containment.
        
        HostBackend relies on POSIX process groups or Windows Job objects,
        which can be escaped by descendants daemonizing (e.g. setsid).
        """
        return ProcessEnforcementLevel.BEST_EFFORT

    async def execute(
        self,
        command: str | list[str],
        workspace_root: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | None = None,
        network_policy: NetworkPolicy | None = None,
        secret_policy: SecretPolicy | None = None,
        execution_id: str | None = None,
    ) -> ProcessResult:
        """Execute command directly on host — NO CONTAINER ISOLATION.

        Security warning:
            This method executes arbitrary commands on the host machine.
            It should only be reachable when the Sandbox was constructed
            with unsafe_host_execution=True.
        """
        warnings.warn(
            "HostBackend.execute(): Running untrusted code directly on host "
            "without container isolation. This is NOT safe for production use.",
            _SecurityWarning,
            stacklevel=2,
        )

        validator = PathValidator(workspace_root)
        target_dir = validator.validate_path(cwd or workspace_root)

        env = env or {}
        # Note: PROXY and ALLOWLIST are UNSUPPORTED and fail closed in api.py,
        # so no fake proxy environment variable injection is done here.
        
        # Build pristine environment to prevent accidental leak, though HostBackend 
        # cannot prevent code from actively reading host credential files.
        safe_env = build_isolated_environment(secret_policy, extra_env=env)

        return await self.process_manager.execute(
            command=command,
            cwd=target_dir,
            env=safe_env,
            limits=limits,
        )

    async def cleanup(self) -> None:
        await self.process_manager.terminate_all()
