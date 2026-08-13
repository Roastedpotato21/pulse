"""Remote execution backend for Sandbox."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from pulse.sandbox.network import NetworkEnforcementLevel, NetworkPolicy
from pulse.sandbox.process import ProcessResult
from pulse.sandbox.remote.client import RemoteClient
from pulse.sandbox.remote.models import SubmitExecutionRequest
from pulse.sandbox.resources import ResourceLimits
from pulse.sandbox.secrets import SecretEnforcementLevel, SecretPolicy
from pulse.sandbox.errors import SandboxUnavailableError
import typing


class RemoteSandboxBackend:
    """Production-grade remote container execution backend.
    
    Communicates securely with a remote worker that enforces
    strong container isolation identical to the local DockerBackend.
    """

    def __init__(self, endpoint_url: str | None = None, auth_token: str | None = None) -> None:
        self.endpoint_url = endpoint_url
        self.auth_token = auth_token
        self._client: RemoteClient | None = None

    @property
    def client(self) -> RemoteClient:
        if not self._client:
            if not self.endpoint_url or not self.auth_token:
                raise SandboxUnavailableError("Remote backend is not fully configured (missing endpoint or token).")
            self._client = RemoteClient(self.endpoint_url, self.auth_token)
        return self._client

    @property
    def name(self) -> str:
        return "remote"

    async def is_available(self) -> bool:
        """Return True if remote endpoint is configured and reachable."""
        if not self.endpoint_url or not self.auth_token:
            return False
            
        # Try to establish a connection to check health
        try:
            client = self.client
            await client.connect()
            await client.disconnect()
            return True
        except Exception:
            return False

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
        
        exec_id = execution_id or str(uuid.uuid4())
        
        # Calculate relative working directory
        rel_cwd = None
        if cwd:
            try:
                rel_cwd = cwd.relative_to(workspace_root).as_posix()
            except ValueError:
                pass

        # Convert limits to policy format
        from pulse.sandbox.resources import ResourcePolicy
        res_policy = limits if isinstance(limits, ResourcePolicy) else (limits.to_policy() if limits else None)
        
        req = SubmitExecutionRequest(
            protocol_version="1.0",
            execution_id=exec_id,
            idempotency_key=str(uuid.uuid4()),
            command=command,
            working_directory=rel_cwd,
            env=env,
            resource_policy_dict=res_policy.to_dict() if res_policy else None,
            network_policy_dict=network_policy.to_dict() if network_policy else None,
            secret_policy_dict=secret_policy.to_dict() if secret_policy else None,
        )
        
        client = self.client
        
        import io
        import tarfile
        import tempfile
        
        # Phase 6: Upload artifacts
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            if workspace_root.exists():
                for item in workspace_root.iterdir():
                    # Skip internal sandbox tracking directories to save space
                    if item.name == ".agent":
                        continue
                    tar.add(item, arcname=item.name)
        
        await client.upload_artifact(exec_id, bio.getvalue())
        
        # Submit execution
        await client.submit(req)
        
        # Process streaming output in the background
        async def handle_stream() -> None:
            if output_callback:
                async for stream_type, chunk in client.stream_output(exec_id):
                    await output_callback(stream_type, chunk.encode("utf-8", errors="replace"))
            else:
                # Still consume it so the queue doesn't back up
                async for _ in client.stream_output(exec_id):
                    pass
                    
        stream_task = asyncio.create_task(handle_stream())
        
        try:
            # Wait for completion
            res_model = await client.get_result(exec_id)
        except asyncio.CancelledError:
            await client.cancel(exec_id)
            raise
        finally:
            await stream_task
            
        # Phase 6: Download artifacts
        overlay_bytes = await client.download_artifact(exec_id)
        local_overlay_path = None
        if overlay_bytes:
            local_overlay_path = Path(tempfile.gettempdir()) / f"pulse_remote_overlay_{exec_id}"
            local_overlay_path.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(fileobj=io.BytesIO(overlay_bytes), mode="r:gz") as tar:
                    import os
                    max_size = 50 * 1024 * 1024
                    current_size = 0
                    for member in tar.getmembers():
                        if member.issym() or member.islnk():
                            raise Exception("Symlinks are not allowed in remote artifacts")
                        current_size += member.size
                        if current_size > max_size:
                            raise Exception(f"Artifact size exceeded limit of {max_size} bytes")
                        
                        member_path = os.path.join(str(local_overlay_path), member.name)
                        if not os.path.abspath(member_path).startswith(os.path.abspath(str(local_overlay_path))):
                            raise Exception("Path traversal detected in download_artifact")
                    if hasattr(tarfile, 'data_filter'):
                        tar.extractall(path=local_overlay_path, filter='data')
                    else:
                        tar.extractall(path=local_overlay_path)
            except Exception as e:
                from pulse.sandbox.errors import SandboxSecurityError
                # Raise an explicit security error on path traversal
                raise SandboxSecurityError(
                    f"Remote execution compromised: Malformed or malicious workspace overlay. Details: {e}",
                    operation="download_artifact",
                    path=str(local_overlay_path),
                )
            
        return ProcessResult(
            command=res_model.command,
            exit_code=res_model.exit_code,
            stdout=res_model.stdout,
            stderr=res_model.stderr,
            duration_ms=res_model.duration_ms,
            timed_out=res_model.timed_out,
            truncated=res_model.truncated,
            pid=None, # Remote PID not exposed securely
            overlay_path=local_overlay_path,
            termination_reason=res_model.termination_reason,
        )

    def get_network_enforcement_capability(self, policy: NetworkPolicy) -> NetworkEnforcementLevel:
        """Determine if this backend can strongly enforce the requested network policy."""
        from pulse.sandbox.network import NetworkMode
        if not policy or policy.mode == NetworkMode.ALLOW_ALL:
            return NetworkEnforcementLevel.STRONGLY_ENFORCED
            
        if policy.mode in (NetworkMode.DENY_ALL, NetworkMode.LOCALHOST_ONLY):
            return NetworkEnforcementLevel.STRONGLY_ENFORCED
            
        return NetworkEnforcementLevel.UNSUPPORTED

    def get_secret_enforcement_capability(self, policy: SecretPolicy) -> SecretEnforcementLevel:
        """Determine if this backend can strongly enforce the requested secret isolation policy."""
        from pulse.sandbox.secrets import SecretMode
        if not policy or policy.mode == SecretMode.ALLOW_ALL:
            return SecretEnforcementLevel.STRONGLY_ENFORCED
            
        if policy.mode in (SecretMode.DENY_ALL, SecretMode.ALLOW_EXPLICIT):
            return SecretEnforcementLevel.STRONGLY_ENFORCED
            
        return SecretEnforcementLevel.UNSUPPORTED

    async def cleanup(self) -> None:
        """Reap temporary remote execution artifacts."""
        if self._client:
            await self._client.disconnect()

    async def reconcile(self) -> None:
        """Clean up orphaned remote backend resources."""
        if self._client:
            await self._client.reconcile()
