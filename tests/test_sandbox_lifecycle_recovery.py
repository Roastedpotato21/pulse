"""Adversarial lifecycle, recovery, and orchestration crash tests for Pulse Sandbox."""

import asyncio
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.docker import DockerBackend
from pulse.sandbox.policy import ActionType, PolicyDecision, SandboxPolicy
from pulse.sandbox.process import ProcessResult
from pulse.sandbox.resources import ResourceLimits


@pytest.mark.anyio
async def test_reconciliation_cleans_orphaned_containers() -> None:
    """Phase 3/5: Startup reconciliation cleans up orphaned managed containers."""
    backend = DockerBackend(container_engine="docker")
    if not await backend.is_available():
        pytest.skip("An operational Docker daemon is required.")
        
    execution_id = str(uuid.uuid4())
    
    # 1. Manually start a sleeping container labeled as managed by Pulse
    proc = await asyncio.create_subprocess_shell(
        f"docker run -d --label pulse.sandbox.managed=true --label pulse.sandbox.execution_id={execution_id} alpine:3.22 sleep 3600",
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
            sandbox = Sandbox(
                Path(directory),
                backend=backend,
            )
            await sandbox.initialize()
            
        # 3. Verify the orphan container is gone
        check_proc2 = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            cid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await check_proc2.communicate()
        assert check_proc2.returncode != 0, "Reconciliation failed to remove orphaned container."

    finally:
        # Cleanup in case test fails
        await asyncio.create_subprocess_shell(f"docker rm -f {cid}", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)


@pytest.mark.anyio
async def test_process_timeout_cleanup() -> None:
    """Phase 4/13: Process timeout transitions to FAILED and cleans up properly."""
    backend = DockerBackend(container_engine="docker")
    if not await backend.is_available():
        pytest.skip("An operational Docker daemon is required.")

    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        audit_path = tmp_path / "audit.jsonl"
        sandbox = Sandbox(
            tmp_path,
            backend=backend,
            limits=ResourceLimits(timeout_seconds=0.5),
            audit_log_path=audit_path,
            policy=SandboxPolicy(
                default_decisions={
                    ActionType.SHELL.value: PolicyDecision.ALLOW,
                    ActionType.NETWORK.value: PolicyDecision.ALLOW,
                    ActionType.SECRETS.value: PolicyDecision.ALLOW,
                }
            ),
        )
        
        result = await sandbox.execute_command("sleep 5")
        
        # Verify the command timed out securely
        assert result.timed_out is True
        assert result.exit_code != 0
        
        # Verify audit logs captured the lifecycle transition
        logs = audit_path.read_text(encoding="utf-8")
        assert "CLEANING" in logs
        assert "FINALIZED" in logs
        assert "FAILED" in logs  # Timeout is a failure state


@pytest.mark.anyio
async def test_timeout_result_transitions_through_failed(tmp_path: Path) -> None:
    class TimeoutBackend(DockerBackend):
        async def reconcile(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

        async def execute(self, **_kwargs: object) -> ProcessResult:
            return ProcessResult(
                command="sleep 5",
                exit_code=-9,
                stdout="",
                stderr="Process execution timed out.",
                duration_ms=500.0,
                timed_out=True,
                termination_reason="timeout",
            )

    audit_path = tmp_path / "audit.jsonl"
    sandbox = Sandbox(
        tmp_path,
        backend=TimeoutBackend(container_engine="docker"),
        audit_log_path=audit_path,
        policy=SandboxPolicy(
            default_decisions={
                ActionType.SHELL.value: PolicyDecision.ALLOW,
                ActionType.NETWORK.value: PolicyDecision.ALLOW,
                ActionType.SECRETS.value: PolicyDecision.ALLOW,
            }
        ),
    )

    result = await sandbox.execute_command("sleep 5")

    assert result.timed_out is True
    logs = audit_path.read_text(encoding="utf-8")
    assert "FAILED" in logs
    assert "CLEANING" in logs
    assert "FINALIZED" in logs
