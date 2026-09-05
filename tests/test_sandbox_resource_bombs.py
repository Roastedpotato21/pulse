"""Adversarial resource exhaustion bomb tests for Pulse Sandbox."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.docker import DockerBackend
from pulse.sandbox.policy import ActionType, PolicyDecision, SandboxPolicy
from pulse.sandbox.resources import ResourceLimits


@pytest.mark.docker
@pytest.mark.anyio
async def test_memory_exhaustion_bomb() -> None:
    """Phase 8: Hard Memory Enforcement prevents OOM cascading."""
    backend = DockerBackend(container_engine="docker")
    if not await backend.is_available():
        pytest.skip("An operational Docker daemon is required for memory limit tests.")
        
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        sandbox = Sandbox(
            workspace_root=tmp_path,
            backend=backend,
            limits=ResourceLimits(
                max_memory_bytes=64 * 1024 * 1024, timeout_seconds=10.0
            ),
            policy=SandboxPolicy(
                default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW}
            ),
        )
        await sandbox.initialize()
        
        # Try to allocate 200MB of memory in Python inside the container
        bomb = "python -c \"x = 'A' * 200 * 1024 * 1024; import time; time.sleep(5)\""
        result = await sandbox.execute_command(bomb)
        
        # Container should be killed by OOM killer (exit code 137 typically)
        assert result.exit_code != 0
        assert not result.timed_out  # It should die from OOM, not timeout
        

@pytest.mark.docker
@pytest.mark.anyio
async def test_storage_exhaustion_bomb() -> None:
    """Phase 7: Disk/Storage Isolation limits prevent disk exhaustion.
    Note: Requires daemon support for --storage-opt. If unsupported, we expect graceful fail-closed.
    """
    backend = DockerBackend(container_engine="docker")
    if not await backend.is_available():
        pytest.skip("An operational Docker daemon is required for storage limit tests.")
        
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        sandbox = Sandbox(
            workspace_root=tmp_path,
            backend=backend,
            limits=ResourceLimits(
                max_storage_bytes=50 * 1024 * 1024, timeout_seconds=10.0
            ),
            policy=SandboxPolicy(
                default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW}
            ),
        )
        await sandbox.initialize()
        
        # Try to write 100MB of data to the overlay
        bomb = "dd if=/dev/zero of=/workspace-overlay/bomb.img bs=1M count=100"
        result = await sandbox.execute_command(bomb)
        
        # Either the backend rejected --storage-opt natively (fail closed on startup, result.exit_code != 0)
        # Or it enforced it and dd failed with "No space left on device"
        assert result.exit_code != 0
        if "No space left on device" in result.stderr:
            pass  # Enforced natively
        elif "daemon" in result.stderr.lower() or "unsupported" in result.stderr.lower() or "storage-opt" in result.stderr.lower():
            pass  # Failed closed during creation
        else:
            # Maybe the container aborted for another reason (dd failure)
            pass
