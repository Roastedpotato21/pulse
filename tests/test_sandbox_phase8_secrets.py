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


@pytest.mark.docker
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


@pytest.mark.docker
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

@pytest.mark.anyio
async def test_docker_argv_no_leakage(tmp_path: Path):
    """Verify explicitly allowed secrets do not appear in Docker command line arguments."""
    backend = DockerBackend()
    
    env = {"API_KEY": "secret_value_123"}
    secret_policy = SecretPolicy(mode=SecretMode.ALLOW_EXPLICIT, explicit_env=env)
    
    env_file = tmp_path / 'env.txt'
    
    cmd_args = backend.build_docker_cmd(
        command="echo hi",
        workspace_root=tmp_path,
        env=env,
        secret_policy=secret_policy,
        env_file_path=env_file
    )
    
    cmd_str = " ".join(cmd_args)
    assert "API_KEY" not in cmd_str
    assert "secret_value_123" not in cmd_str
    assert "--env-file" in cmd_str

@pytest.mark.docker
@pytest.mark.anyio
async def test_docker_env_file_cleanup(tmp_path: Path):
    """Verify env-file is cleaned up even on failure."""
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
            explicit_env={"TEST_SECRET": "should_be_cleaned_up"}
        )
    )
    
    import tempfile
    tmp_base = Path(tempfile.gettempdir())
    
    await sandbox.execute_command(["invalid_command_that_fails_fast"])
    
    env_files = list(tmp_base.glob("pulse-sandbox-*/container.env"))
    assert len(env_files) == 0

@pytest.mark.anyio
async def test_cow_secret_staging_blocked(tmp_path: Path):
    """Verify that staging a file with explicitly authorized secrets is blocked."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sandbox = Sandbox(
        workspace_root=workspace,
        secrets=["super_secret_value_123"],
        policy=SandboxPolicy(default_decisions={
            ActionType.WRITE.value: PolicyDecision.ALLOW,
        })
    )
    
    tx = sandbox.create_transaction()
    
    from pulse.sandbox.errors import SandboxSecurityError
    with pytest.raises(SandboxSecurityError, match="Commit rejected"):
        sandbox.stage_write(tx, "config.json", '{"key": "super_secret_value_123"}')

@pytest.mark.docker
@pytest.mark.anyio
async def test_process_inheritance_isolation(tmp_path: Path):
    """Verify that child/grandchild processes can read explicit secrets but not host secrets."""
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
    
    result = await sandbox.execute_command([
        "sh", "-c", "sh -c 'env'"
    ])
    
    assert result.exit_code == 0
    assert "DUMMY_HOST_SECRET" not in result.stdout
    assert "ALLOWED_KEY=allowed_value_456" in result.stdout
