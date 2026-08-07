"""Structural protocol for sandbox container execution backends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pulse.sandbox.process import ProcessResult
from pulse.sandbox.resources import ResourceLimits


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
        network_enabled: bool = False,
    ) -> ProcessResult:
        """Run a command inside the isolated backend environment.

        Args:
            command: Command string or argument list to run.
            workspace_root: Absolute host path to the workspace root directory.
            cwd: Working directory (must be inside workspace_root).
            env: Environment variable overrides.
            limits: Process resource limits.
            network_enabled: Whether outbound network access is granted.

        Returns:
            ProcessResult object containing execution status and output.
        """
        ...

    async def cleanup(self) -> None:
        """Reap temporary volumes, containers, or process artifacts."""
        ...
