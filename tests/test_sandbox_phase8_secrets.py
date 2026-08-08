import os
import sys
from pathlib import Path

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.docker import DockerBackend
from pulse.sandbox.backend.host import HostBackend
from pulse.sandbox.errors import SandboxUnsupportedPolicyError
from pulse.sandbox.policy import ActionType, PolicyDecision, SandboxPolicy
from pulse.sandbox.secrets import SecretMode, SecretPolicy


@pytest.fixture(autouse=True)
def setup_dummy_secret_env():
    """Inject a dummy secret into the host environment for testing."""
    os.environ["DUMMY_HOST_SECRET"] = "super_secret_value_123"
    yield
    if "DUMMY_HOST_SECRET" in os.environ:
        del os.environ["DUMMY_HOST_SECRET"]


@pytest.mark.anyio
async def test_secret_policy_docker_deny_all(tmp_path: Path):
    """Verify Docker backend correctly drops the host environment."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    backend = DockerBackend()
    if not await backend.is_available():
        pytest.skip("Docker unavailable")

    sandbox = Sandbox(
        workspace_root=workspace,
        backend=backend,
        policy=SandboxPolicy(default_decisions={
            ActionType.SHELL.value: PolicyDecision.ALLOW,
            ActionType.NETWORK.value: PolicyDecision.ALLOW,
            ActionType.SECRETS.value: PolicyDecision.DENY,
        })
    )
    await sandbox.initialize()
    
    # We should not be able to read DUMMY_HOST_SECRET
    result = await sandbox.execute_command(["env"])
    assert result.exit_code == 0
    assert "DUMMY_HOST_SECRET" not in result.stdout
    assert "super_secret_value_123" not in result.stdout


@pytest.mark.anyio
async def test_secret_policy_docker_allow_explicit(tmp_path: Path):
    """Verify Docker backend can explicitly pass an environment variable."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    backend = DockerBackend()
    if not await backend.is_available():
        pytest.skip("Docker unavailable")

    sandbox = Sandbox(
        workspace_root=workspace,
        backend=backend,
        policy=SandboxPolicy(default_decisions={
            ActionType.SHELL.value: PolicyDecision.ALLOW,
            ActionType.NETWORK.value: PolicyDecision.ALLOW,
            ActionType.SECRETS.value: PolicyDecision.ALLOW,
        }),
        secret_policy=SecretPolicy(
            mode=SecretMode.ALLOW_EXPLICIT,
            explicit_env={"ALLOWED_KEY": "allowed_value_456"}
        )
    )
    await sandbox.initialize()
    
    # We should not see the host secret, but we should see the allowed key
    result = await sandbox.execute_command(["env"])
    assert result.exit_code == 0
    assert "DUMMY_HOST_SECRET" not in result.stdout
    assert "ALLOWED_KEY=allowed_value_456" in result.stdout


@pytest.mark.anyio
async def test_secret_policy_host_fails_closed_on_deny(tmp_path: Path):
    """Verify HostBackend refuses to execute when asked to strongly enforce secrets."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sandbox = Sandbox(
        workspace_root=workspace,
        backend=HostBackend(),
        unsafe_host_execution=True,
        policy=SandboxPolicy(default_decisions={
            ActionType.SHELL.value: PolicyDecision.ALLOW,
            ActionType.NETWORK.value: PolicyDecision.ALLOW,
            # Explicitly DENY secrets. HostBackend can't enforce this.
            ActionType.SECRETS.value: PolicyDecision.DENY,
        })
    )
    await sandbox.initialize()

    with pytest.raises(SandboxUnsupportedPolicyError, match="cannot strongly enforce secret policy"):
        await sandbox.execute_command([sys.executable, "-c", "print('hello')"])


@pytest.mark.anyio
async def test_secret_policy_host_allow_all(tmp_path: Path):
    """Verify HostBackend successfully executes when ALLOW_ALL is applied."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sandbox = Sandbox(
        workspace_root=workspace,
        backend=HostBackend(),
        unsafe_host_execution=True,
        policy=SandboxPolicy(default_decisions={
            ActionType.SHELL.value: PolicyDecision.ALLOW,
            ActionType.NETWORK.value: PolicyDecision.ALLOW,
            ActionType.SECRETS.value: PolicyDecision.ALLOW,
        })
        # Note: unsafe_host_execution=True will map the default secret policy to ALLOW_ALL
        # since we didn't explicitly override secret_policy.
    )
    await sandbox.initialize()

    # Should execute successfully
    result = await sandbox.execute_command([sys.executable, "-c", "import os; print(os.environ.get('DUMMY_HOST_SECRET', ''))"])
    assert result.exit_code == 0
    assert "super_secret_value_123" in result.stdout
