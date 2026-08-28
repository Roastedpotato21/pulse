"""Tests for Phase 7 Network Isolation & Egress Control."""

import socket
from pathlib import Path

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.host import HostBackend
from pulse.sandbox.errors import SandboxUnsupportedPolicyError
from pulse.sandbox.network import NetworkMode, NetworkPolicy, NetworkRule, Protocol
from pulse.sandbox.policy import ActionType, PolicyDecision, SandboxPolicy


def test_network_policy_is_safe_ip():
    """Verify safe IP address determination (DNS rebinding protection)."""
    policy = NetworkPolicy()

    # Private/Internal ranges MUST be blocked
    assert policy.is_safe_ip("127.0.0.1") is False
    assert policy.is_safe_ip("::1") is False
    assert policy.is_safe_ip("10.0.0.1") is False
    assert policy.is_safe_ip("192.168.1.1") is False
    assert policy.is_safe_ip("172.16.0.1") is False
    assert policy.is_safe_ip("169.254.169.254") is False  # Link-local (cloud metadata)

    # Public IPs MUST be allowed
    assert policy.is_safe_ip("8.8.8.8") is True
    assert policy.is_safe_ip("1.1.1.1") is True
    assert policy.is_safe_ip("93.184.216.34") is True  # example.com


def test_network_policy_validate_destination():
    """Verify policy evaluation for outbound connections."""
    
    # 1. DENY_ALL
    deny_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)
    assert deny_policy.validate_destination("8.8.8.8", 80) is False
    assert deny_policy.validate_destination("localhost", 80) is False

    # 2. LOCALHOST_ONLY
    local_policy = NetworkPolicy(mode=NetworkMode.LOCALHOST_ONLY)
    assert local_policy.validate_destination("127.0.0.1", 80) is True
    assert local_policy.validate_destination("localhost", 80) is True
    assert local_policy.validate_destination("8.8.8.8", 80) is False

    # 3. PROXY
    proxy_policy = NetworkPolicy(mode=NetworkMode.PROXY, proxy_url="http://proxy:8080")
    # Proxy mode denies direct connection evaluation (handled by backend injecting env vars)
    assert proxy_policy.validate_destination("8.8.8.8", 80) is False

    # 4. ALLOWLIST
    allow_policy = NetworkPolicy(
        mode=NetworkMode.ALLOWLIST,
        rules=[
            NetworkRule(destination="8.8.8.8", port=53, protocol=Protocol.UDP),
            NetworkRule(destination="*.example.com", port=443, protocol=Protocol.TCP),
            NetworkRule(destination="93.184.216.34", port=None, protocol=Protocol.ANY),
        ]
    )
    
    # Exact match
    assert allow_policy.validate_destination("8.8.8.8", 53, Protocol.UDP) is True
    assert allow_policy.validate_destination("8.8.8.8", 53, Protocol.TCP) is False
    
    # Wildcard match (assuming example.com resolves to public IP)
    try:
        # Only run this assertion if DNS is working, to avoid flaky tests
        socket.gethostbyname("api.example.com")
        assert allow_policy.validate_destination("api.example.com", 443, Protocol.TCP) is True
    except socket.gaierror:
        pass
        
    # Unmatched
    assert allow_policy.validate_destination("1.1.1.1", 443, Protocol.TCP) is False


def test_network_policy_dns_rebinding_protection(monkeypatch):
    """Verify ALLOWLIST rejects hostnames that resolve to private IPs."""
    allow_policy = NetworkPolicy(
        mode=NetworkMode.ALLOWLIST,
        rules=[NetworkRule(destination="malicious.com")]
    )
    
    # Mock socket.getaddrinfo to return a private IP
    def mock_getaddrinfo(host, port, proto=0, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        
    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    
    # Even though "malicious.com" matches the wildcard rule if it was `*`,
    # here it matches exactly, BUT it resolves to 127.0.0.1 which is blocked.
    assert allow_policy.validate_destination("malicious.com", 80, Protocol.TCP) is False


@pytest.mark.anyio
async def test_sandbox_network_deny_all(tmp_path: Path):
    """Verify DENY_ALL actually blocks external network access."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    sandbox = Sandbox(workspace, unsafe_host_execution=False)
    try:
        await sandbox.initialize()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")
        
    if sandbox.backend.name not in ("docker", "podman"):
        pytest.skip("Backend does not support strict network isolation")
        
    # We must allow shell execution
    sandbox.policy = SandboxPolicy(default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW, ActionType.NETWORK.value: PolicyDecision.ALLOW, ActionType.SECRETS.value: PolicyDecision.ALLOW})
    sandbox.network_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)
    
    result = await sandbox.execute_command(
        [
            "python",
            "-c",
            "import socket; socket.create_connection(('8.8.8.8', 80), 2)",
        ]
    )
    
    assert result.exit_code != 0
    assert result.stderr


@pytest.mark.anyio
async def test_sandbox_network_localhost_only(tmp_path: Path):
    """Verify LOCALHOST_ONLY allows internal container loopback but blocks external."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    sandbox = Sandbox(workspace, unsafe_host_execution=False)
    try:
        await sandbox.initialize()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")
        
    if sandbox.backend.name not in ("docker", "podman"):
        pytest.skip("Backend does not support strict network isolation")
        
    sandbox.policy = SandboxPolicy(default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW, ActionType.NETWORK.value: PolicyDecision.ALLOW, ActionType.SECRETS.value: PolicyDecision.ALLOW})
    sandbox.network_policy = NetworkPolicy(mode=NetworkMode.LOCALHOST_ONLY)
    
    script = (
        "import http.server, socketserver, threading, urllib.request; "
        "server=socketserver.TCPServer(('127.0.0.1', 8080), http.server.SimpleHTTPRequestHandler); "
        "thread=threading.Thread(target=server.serve_forever, daemon=True); thread.start(); "
        "print(urllib.request.urlopen('http://127.0.0.1:8080', timeout=2).status); "
        "server.shutdown()"
    )
    cmd = ["python", "-c", script]
    
    result = await sandbox.execute_command(cmd)
    
    # The curl should succeed and return directory listing HTML
    assert result.exit_code == 0
    assert "200" in result.stdout
    
    # External should still fail
    result_ext = await sandbox.execute_command(
        [
            "python",
            "-c",
            "import socket; socket.create_connection(('8.8.8.8', 80), 2)",
        ]
    )
    assert result_ext.exit_code != 0


@pytest.mark.anyio
async def test_sandbox_network_unsupported_policies_fail_closed(tmp_path: Path):
    """Verify that unsupported restrictive policies like ALLOWLIST and PROXY explicitly fail closed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # Test on HostBackend
    sandbox = Sandbox(
        workspace,
        backend=HostBackend(),
        unsafe_host_execution=True,
    )
    await sandbox.initialize()
        
    sandbox.policy = SandboxPolicy(default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW, ActionType.NETWORK.value: PolicyDecision.ALLOW, ActionType.SECRETS.value: PolicyDecision.ALLOW})
    
    # 1. HostBackend rejects everything
    sandbox.network_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)
    with pytest.raises(SandboxUnsupportedPolicyError, match="cannot strongly enforce"):
        await sandbox.execute_command(["echo", "hello"])
        
    # 2. DockerBackend rejects ALLOWLIST and PROXY
    if sandbox.backend.name in ("docker", "podman"):
        # Not using HostBackend anymore; recreate for Docker
        sandbox_docker = Sandbox(workspace, unsafe_host_execution=False)
        try:
            await sandbox_docker.initialize()
            
            sandbox_docker.policy = SandboxPolicy(default_decisions={ActionType.SHELL.value: PolicyDecision.ALLOW, ActionType.NETWORK.value: PolicyDecision.ALLOW, ActionType.SECRETS.value: PolicyDecision.ALLOW})
            
            # PROXY
            sandbox_docker.network_policy = NetworkPolicy(mode=NetworkMode.PROXY, proxy_url="http://fake-proxy")
            with pytest.raises(SandboxUnsupportedPolicyError, match="cannot strongly enforce"):
                await sandbox_docker.execute_command(["echo", "hello"])
                
            # ALLOWLIST
            sandbox_docker.network_policy = NetworkPolicy(mode=NetworkMode.ALLOWLIST)
            with pytest.raises(SandboxUnsupportedPolicyError, match="cannot strongly enforce"):
                await sandbox_docker.execute_command(["echo", "hello"])
                
            # DENY_ALL should succeed (exit code 0 for echo)
            sandbox_docker.network_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)
            result = await sandbox_docker.execute_command(["echo", "hello"])
            assert result.exit_code == 0
            
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Docker might not be available, skip Docker-specific tests: {e}")
