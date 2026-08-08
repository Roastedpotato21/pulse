"""Unit tests for Phase 3 of Pulse Sandbox: Rootless Container Execution Backends."""

from pathlib import Path

import pytest

from pulse.sandbox.backend import ContainerBackend, DockerBackend, HostBackend
from pulse.sandbox.network import NetworkMode, NetworkPolicy
from pulse.sandbox.resources import ResourceLimits

# ---------------------------------------------------------------------------
# 1. Protocol Conformance Tests
# ---------------------------------------------------------------------------


def test_container_backend_protocol_conformance():
    docker_be = DockerBackend()
    host_be = HostBackend()

    assert isinstance(docker_be, ContainerBackend)
    assert isinstance(host_be, ContainerBackend)


# ---------------------------------------------------------------------------
# 2. Docker CLI Flag Generation Tests
# ---------------------------------------------------------------------------


def test_docker_backend_flag_generation(tmp_path: Path):
    backend = DockerBackend(image="python:3.11-slim", container_engine="docker")
    limits = ResourceLimits(max_memory_bytes=512 * 1024 * 1024, max_pids=32)

    cmd_args = backend.build_docker_cmd(
        command="python --version",
        workspace_root=tmp_path,
        cwd=tmp_path / "src",
        env={"MY_ENV": "TEST"},
        limits=limits,
        network_policy=NetworkPolicy(mode=NetworkMode.DENY_ALL),
    )

    cmd_str = " ".join(cmd_args)
    assert "docker run --rm -i --read-only --cap-drop=ALL" in cmd_str
    assert "--network none" in cmd_str
    assert "--memory 536870912b" in cmd_str
    assert "--pids-limit 32" in cmd_str
    assert "--env MY_ENV=TEST" in cmd_str
    assert "python:3.11-slim sh -c python --version" in cmd_str


def test_docker_backend_network_enabled(tmp_path: Path):
    backend = DockerBackend(container_engine="podman")
    cmd_args = backend.build_docker_cmd(
        command=["pytest"],
        workspace_root=tmp_path,
        network_policy=NetworkPolicy(mode=NetworkMode.ALLOWLIST),
    )

    cmd_str = " ".join(cmd_args)
    assert "podman run" in cmd_str
    assert "--network none" not in cmd_str


# ---------------------------------------------------------------------------
# 3. Host Backend Fallback Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_host_backend_execution(tmp_path: Path):
    backend = HostBackend()
    assert await backend.is_available() is True

    result = await backend.execute(
        command="echo HOST_BACKEND_TEST",
        workspace_root=tmp_path,
    )

    assert result.exit_code == 0
    assert "HOST_BACKEND_TEST" in result.stdout
