"""Remote Sandbox Worker.

Wraps the DockerBackend to securely execute commands on behalf of the RemoteServer.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from pathlib import Path
from typing import Any

from pulse.sandbox.backend.docker import DockerBackend
from pulse.sandbox.network import NetworkPolicy
from pulse.sandbox.process import ProcessResult
from pulse.sandbox.remote.models import (
    ExecutionResultModel,
    SubmitExecutionRequest,
    validate_execution_id,
)
from pulse.sandbox.resources import ResourcePolicy
from pulse.sandbox.secrets import SecretPolicy, SecretScrubber

logger = logging.getLogger(__name__)


class RemoteWorker:
    """Executes untrusted code inside a hardened Docker/Podman container.

    This worker acts as the server-side counterpart to the local DockerBackend,
    enforcing identical security semantics.
    """

    def __init__(self, workspace_base_path: Path | None = None) -> None:
        self.backend = DockerBackend()
        # Default isolation base path where tenant workspaces will be unpacked
        self.workspace_base_path = workspace_base_path or Path(
            "/tmp/pulse_remote_workspaces"
        )
        self.workspace_base_path.mkdir(parents=True, exist_ok=True)
        self.overlays: dict[str, Path] = {}
        self._active_executions: dict[str, asyncio.Task[Any]] = {}

    async def initialize(self) -> None:
        """Initialize the worker backend."""
        await self.backend.reconcile()

    async def execute_request(
        self,
        req: SubmitExecutionRequest,
        tenant_id: str = "unknown",
        output_callback: typing.Callable[[str, bytes], typing.Awaitable[None]]
        | None = None,
    ) -> ExecutionResultModel:
        """Execute a remote request securely."""

        # Deserialize policies
        res_policy = (
            ResourcePolicy.from_dict(req.resource_policy_dict)
            if req.resource_policy_dict
            else ResourcePolicy(wall_time_seconds=600.0)
        )
        net_policy = (
            NetworkPolicy.from_dict(req.network_policy_dict)
            if req.network_policy_dict
            else None
        )
        sec_policy = (
            SecretPolicy.from_dict(req.secret_policy_dict)
            if req.secret_policy_dict
            else None
        )
        secret_values = list((req.env or {}).values())
        if sec_policy:
            secret_values.extend(sec_policy.explicit_env.values())
        scrubber = SecretScrubber(secret_values)

        async def safe_output_callback(stream: str, data: bytes) -> None:
            if output_callback is None:
                return
            text = data.decode("utf-8", errors="replace")
            redacted = scrubber.redact(text).encode("utf-8", errors="replace")
            await output_callback(stream, redacted)

        # Resolve paths for the execution
        tenant_workspace = self.workspace_base_path / tenant_id / req.execution_id
        tenant_workspace.mkdir(parents=True, exist_ok=True)

        cwd = (
            tenant_workspace / req.working_directory
            if req.working_directory
            else tenant_workspace
        )

        # Keep track of the current asyncio task so we can cancel it via cancel()
        import asyncio

        self._active_executions[req.execution_id] = asyncio.current_task()

        try:
            # We delegate to DockerBackend for strict isolation (--cap-drop=ALL, etc)
            result: ProcessResult = await self.backend.execute(
                command=req.command,
                workspace_root=tenant_workspace,
                cwd=cwd,
                env=req.env,
                limits=res_policy,
                network_policy=net_policy,
                secret_policy=sec_policy,
                execution_id=req.execution_id,
                output_callback=safe_output_callback if output_callback else None,
            )

            # Store the overlay path for retrieval (R6)
            if result.overlay_path and result.overlay_path.exists():
                self.overlays[req.execution_id] = result.overlay_path

            return ExecutionResultModel(
                execution_id=req.execution_id,
                command=scrubber.redact(result.command),
                exit_code=result.exit_code,
                stdout=scrubber.redact(result.stdout),
                stderr=scrubber.redact(result.stderr),
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                truncated=result.truncated,
                termination_reason=result.termination_reason,
            )

        except (OSError, RuntimeError, asyncio.CancelledError):
            logger.error("Worker execution failed with an internal error.")
            return ExecutionResultModel(
                execution_id=req.execution_id,
                command=scrubber.redact(str(req.command)),
                exit_code=-1,
                stdout="",
                stderr="Worker execution failed with an internal error.",
                duration_ms=0.0,
                termination_reason="worker_crash",
            )
        finally:
            self._active_executions.pop(req.execution_id, None)

    def get_overlay_path(self, execution_id: str) -> Path | None:
        """Get the stored overlay path for an execution."""
        return self.overlays.get(execution_id)

    def cleanup_overlay(self, execution_id: str) -> None:
        """Clean up the stored overlay."""
        overlay = self.overlays.pop(execution_id, None)
        if overlay and overlay.exists():
            import shutil

            shutil.rmtree(overlay, ignore_errors=True)
            try:
                overlay.parent.rmdir()
            except OSError:
                pass

    def cleanup_workspace(self, tenant_id: str, execution_id: str) -> None:
        """Clean up the tenant workspace."""
        validate_execution_id(execution_id)
        workspace = self.workspace_base_path / tenant_id / execution_id
        if workspace.exists():
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)
            self.cleanup_overlay(execution_id)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution on this worker."""
        task = self._active_executions.get(execution_id)
        if task and not task.done():
            task.cancel()
