"""Adversarial lifecycle, recovery, and orchestration crash tests for Pulse Sandbox."""

import asyncio
import shutil
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from pulse.config import SandboxConfig
from pulse.sandbox.api import Sandbox


@pytest.mark.anyio
async def test_reconciliation_cleans_orphaned_containers() -> None:
    """Phase 3/5: Startup reconciliation cleans up orphaned managed containers."""
    if not shutil.which("docker"):
        pytest.skip("Docker is required for container reconciliation tests.")
        
    execution_id = str(uuid.uuid4())
    
    # 1. Manually start a sleeping container labeled as managed by Pulse
    proc = await asyncio.create_subprocess_shell(
        f"docker run -d --label pulse.sandbox.managed=true --label pulse.sandbox.execution_id={execution_id} alpine sleep 3600",
        stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    cid = stdout.decode().strip()
    
    try:
        # Verify it is running
        check_proc = await asyncio.create_subprocess_shell(f"docker inspect --format '{{{{.State.Running}}}}' {cid}", stdout=asyncio.subprocess.PIPE)
        out, _ = await check_proc.communicate()
        assert out.decode().strip() == "true", "Synthetic orphan container did not start."
        
        # 2. Trigger Sandbox initialization which runs reconciliation
        with TemporaryDirectory() as directory:
            from pulse.audit import AuditLog
            audit = AuditLog(Path(directory) / "audit.jsonl")
            config = SandboxConfig(workspace_root=Path(directory))
            sandbox = Sandbox(config, audit_logger=audit)
            await sandbox.initialize()
            
        # 3. Verify the orphan container is gone
        check_proc2 = await asyncio.create_subprocess_shell(f"docker inspect {cid}", stderr=asyncio.subprocess.PIPE)
        _, err = await check_proc2.communicate()
        assert "No such object" in err.decode() or "Error: No such container" in err.decode(), "Reconciliation failed to remove orphaned container."

    finally:
        # Cleanup in case test fails
        await asyncio.create_subprocess_shell(f"docker rm -f {cid}", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)


@pytest.mark.anyio
async def test_process_timeout_cleanup() -> None:
    """Phase 4/13: Process timeout transitions to FAILED and cleans up properly."""
    if not shutil.which("docker"):
        pytest.skip("Docker is required for this test.")

    with TemporaryDirectory() as directory:
        from pulse.audit import AuditLog
        from pulse.sandbox.resources import ResourceLimits
        
        tmp_path = Path(directory)
        audit = AuditLog(tmp_path / "audit.jsonl")
        config = SandboxConfig(workspace_root=tmp_path)
        sandbox = Sandbox(config, audit_logger=audit)
        
        # Override limits directly on the backend to enforce a fast timeout
        sandbox.limits = ResourceLimits(timeout_seconds=0.5)
        
        result = await sandbox.execute_command("sleep 5")
        
        # Verify the command timed out securely
        assert result.timed_out is True
        assert result.exit_code != 0
        
        # Verify audit logs captured the lifecycle transition
        logs = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "CLEANING" in logs
        assert "FINALIZED" in logs
        assert "FAILED" in logs  # Timeout is a failure state
