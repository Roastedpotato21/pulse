"""Unit tests for Phase 3 of Pulse Sandbox: Rootless Container Execution Backends."""

from pathlib import Path

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend import ContainerBackend, DockerBackend, HostBackend
from pulse.sandbox.network import NetworkMode, NetworkPolicy
from pulse.sandbox.process import ProcessResult
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
    assert "--label pulse.sandbox.managed=true" in cmd_str
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


@pytest.mark.anyio
async def test_backend_selection_prefers_operational_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DockerBackend()
    probes: list[str] = []

    async def operational(engine: str) -> bool:
        probes.append(engine)
        return True

    monkeypatch.setattr(backend, "_engine_operational", operational)
    assert await backend.is_available() is True
    assert backend.name == "docker"
    assert probes == ["docker"]


@pytest.mark.anyio
async def test_explicit_backend_is_reconciled_on_initialize(tmp_path: Path) -> None:
    class RecordingBackend(HostBackend):
        reconciled = False

        async def reconcile(self) -> None:
            self.reconciled = True

    backend = RecordingBackend()
    sandbox = Sandbox(tmp_path, backend=backend, unsafe_host_execution=True)
    await sandbox.initialize()
    assert backend.reconciled is True


@pytest.mark.anyio
async def test_docker_client_does_not_receive_container_native_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingProcessManager:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def execute(self, *_args: object, **kwargs: object) -> ProcessResult:
            self.kwargs = kwargs
            command = _args[0]
            assert isinstance(command, list)
            export_mount = next(
                part
                for part in command
                if isinstance(part, str) and part.endswith(":/workspace-export:rw")
            )
            export_path = Path(export_mount.removesuffix(":/workspace-export:rw"))
            (export_path / ".pulse-export-complete").touch()
            return ProcessResult("docker run", 0, "", "", 1.0)

    manager = RecordingProcessManager()
    backend = DockerBackend(container_engine="docker", process_manager=manager)  # type: ignore[arg-type]

    async def available() -> bool:
        return True

    monkeypatch.setattr(backend, "is_available", available)
    await backend.execute(
        "echo ok",
        workspace_root=tmp_path,
        limits=ResourceLimits(max_memory_bytes=64 * 1024 * 1024),
    )

    assert manager.kwargs["apply_native_limits"] is False


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
