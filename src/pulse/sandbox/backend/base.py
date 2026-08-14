"""Structural protocol for sandbox container execution backends."""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Protocol, runtime_checkable

from pulse.sandbox.network import NetworkEnforcementLevel, NetworkPolicy
from pulse.sandbox.process import ProcessEnforcementLevel, ProcessResult
from pulse.sandbox.resources import ResourceLimits
from pulse.sandbox.secrets import SecretEnforcementLevel, SecretPolicy


@runtime_checkable
class ContainerBackend(Protocol):
    """Abstract interface satisfied by Docker, Podman, and Host backends."""

    name: str

    async def is_available(self) -> bool:
        """Return True if this container backend engine is installed and operational."""
        ...

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
        output_callback: typing.Callable[[str, bytes], typing.Awaitable[None]] | None = None,
    ) -> ProcessResult:
        """Run a command inside the isolated backend environment.

        Args:
            command: Command string or argument list to run.
            workspace_root: Absolute host path to the workspace root directory.
            cwd: Working directory (must be inside workspace_root).
            env: Environment variable overrides.
            limits: Process resource limits.
            network_policy: Execution network policy configuration.
            secret_policy: Execution secret policy configuration.
            execution_id: Optional UUID identifying this lifecycle execution.

        Returns:
            ProcessResult object containing execution status and output.
        """
        ...

    def get_network_enforcement_capability(self, policy: NetworkPolicy) -> NetworkEnforcementLevel:
        """Determine if this backend can strongly enforce the requested network policy."""
        ...

    def get_secret_enforcement_capability(self, policy: SecretPolicy) -> SecretEnforcementLevel:
        """Determine if this backend can strongly enforce the requested secret isolation policy."""
        ...

    def get_process_containment_capability(self) -> ProcessEnforcementLevel:
        """Determine if this backend provides strong process containment."""
        ...

    async def cleanup(self) -> None:
        """Reap temporary volumes, containers, or process artifacts."""
        ...

    async def reconcile(self) -> None:
        """Clean up orphaned backend resources (e.g., leaked containers)."""
        ...
