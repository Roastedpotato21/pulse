"""Integration tests for Phase 5 of Pulse Sandbox: Public Sandbox API, Safe Tools, and Subsystem Integration."""

import sys
from pathlib import Path

import pytest

from pulse.sandbox import (
    PolicyDecision,
    PolicyRule,
    SafeGit,
    SafePython,
    Sandbox,
    SandboxPolicy,
)
from pulse.sandbox.backend.host import HostBackend


@pytest.mark.anyio
async def test_sandbox_facade_read_and_policy(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    secret_file = workspace / "secrets.env"
    secret_file.write_text("MY_KEY = sk-proj-1234567890abcdef1234567890abcdef", encoding="utf-8")

    policy = SandboxPolicy()
    policy.add_rule(PolicyRule(action="read", target_pattern="forbidden/*", decision=PolicyDecision.DENY))

    sandbox = Sandbox(workspace, policy=policy, secrets=["sk-proj-1234567890abcdef1234567890abcdef"])

    # Valid read with secret scrubbing
    content = sandbox.read_file("secrets.env")
    assert "sk-proj-1234567890abcdef1234567890abcdef" not in content
    assert "[REDACTED_SECRET]" in content

    # Policy deny check
    with pytest.raises(PermissionError):
        sandbox.read_file("forbidden/data.json")


@pytest.mark.anyio
async def test_sandbox_facade_cow_workflow(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app_file = workspace / "main.py"
    app_file.write_text("print('v1')", encoding="utf-8")

    # Requires explicit write-allow policy (default is now DENY)
    policy = SandboxPolicy(default_decisions={"write": PolicyDecision.ALLOW})
    sandbox = Sandbox(workspace, policy=policy)
    tx = sandbox.create_transaction()

    sandbox.stage_write(tx, "main.py", "print('v2')")
    diff = sandbox.preview_changes(tx)
    assert "-print('v1')" in diff
    assert "+print('v2')" in diff

    # Confirm original file unchanged before commit
    assert app_file.read_text(encoding="utf-8") == "print('v1')"

    modified = sandbox.commit_transaction(tx)
    assert "main.py" in modified
    assert app_file.read_text(encoding="utf-8") == "print('v2')"


@pytest.mark.anyio
async def test_sandbox_facade_execute_command(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Requires explicit shell-allow and network-allow (for HostBackend)
    policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    sandbox = Sandbox(workspace, secrets=["MY_SECRET_VAL"], backend=HostBackend(), policy=policy, unsafe_host_execution=True)
    result = await sandbox.execute_command([sys.executable, "-c", "print('SECRET: MY_SECRET_VAL')"])

    assert result.exit_code == 0
    assert "MY_SECRET_VAL" not in result.stdout
    assert "[REDACTED_SECRET]" in result.stdout


@pytest.mark.anyio
async def test_safe_git_and_safe_python(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Requires explicit shell-allow and network-allow and host backend for test env
    policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    policy.add_rule(PolicyRule(action="git", target_pattern="*", decision=PolicyDecision.ALLOW))
    policy.add_rule(PolicyRule(action="python", target_pattern="*", decision=PolicyDecision.ALLOW))

    sandbox = Sandbox(workspace, policy=policy, backend=HostBackend(), unsafe_host_execution=True)
    SafeGit(sandbox)
    py = SafePython(sandbox)

    # Safe python execution
    script = workspace / "hello.py"
    script.write_text("print('SAFE_PYTHON_OK')", encoding="utf-8")

    result = await py.run_script("hello.py")
    assert result.exit_code == 0
    assert "SAFE_PYTHON_OK" in result.stdout
