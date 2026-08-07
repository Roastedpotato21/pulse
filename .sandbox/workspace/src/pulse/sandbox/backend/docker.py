"""Rootless Docker and Podman container execution backend.

Enforces capability dropping (--cap-drop=ALL), read-only root filesystems (--read-only),
tmpfs /tmp mounts, non-root user execution, and network isolation (--network none).

Security hardening (CoW bypass fix):
    - Workspace mounted as READ-ONLY (:ro) inside the container.
    - Writable overlay at /workspace-overlay for container writes.
    - After execution, overlay changes are extracted and returned for CoW staging.
    - --memory-swap equal to --memory prevents swap exhaustion.
    - --user 65534:65534 (nobody) prevents container root execution.
    - --cpu-quota/--cpu-period for CPU limiting.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path

from pulse.sandbox.process import ProcessManager, ProcessResult
from pulse.sandbox.resources import ResourceLimits


class DockerBackend:
    """Production-grade rootless container execution backend.

    Security architecture:
        The workspace is mounted read-only (:ro) to prevent any container
        process from modifying workspace files directly, closing the CoW
        bypass vulnerability. A writable tmpfs overlay at /workspace-overlay
        is provided for container processes that need to write files. After
        execution, the caller can extract changes from the overlay and route
        them through the CoW transaction layer.
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        container_engine: str | None = None,
        process_manager: ProcessManager | None = None,
    ) -> None:
        self.image = image
        self._engine = container_engine
        self.process_manager = process_manager or ProcessManager()

    @property
    def name(self) -> str:
        return self._engine or "docker"

    async def is_available(self) -> bool:
        """Check if Docker or Podman CLI is installed on the host."""
        if self._engine:
            return shutil.which(self._engine) is not None
        if shutil.which("podman"):
            self._engine = "podman"
            return True
        if shutil.which("docker"):
            self._engine = "docker"
            return True
        return False

    def build_docker_cmd(
        self,
        command: str | list[str],
        workspace_root: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | None = None,
        network_enabled: bool = False,
        cidfile: Path | None = None,
    ) -> list[str]:
        """Construct the exact `docker run` or `podman run` CLI arguments.

        Security architecture:
            1. Workspace is mounted READ-ONLY (:ro) — prevents CoW bypass.
            2. /workspace-overlay is a writable tmpfs for container writes.
            3. Capabilities are dropped (--cap-drop=ALL).
            4. Root filesystem is read-only (--read-only).
            5. No new privileges (--security-opt=no-new-privileges:true).
            6. User is nobody (65534:65534) — no root in container.
            7. Memory-swap equals memory — prevents swap exhaustion.
        """
        engine = self._engine or "docker"
        workspace_abs = str(workspace_root.resolve())

        # Determine relative container working directory
        rel_workdir = "/workspace"
        if cwd:
            resolved_cwd = cwd.resolve()
            try:
                rel_parts = resolved_cwd.relative_to(workspace_root.resolve())
                rel_workdir = f"/workspace/{rel_parts.as_posix()}".rstrip("/")
            except ValueError:
                rel_workdir = "/workspace"

        cmd_args: list[str] = [
            engine,
            "run",
        ]
        if not cidfile:
            cmd_args.append("--rm")
            
        cmd_args.extend([
            "-i",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            # Non-root user inside container (nobody)
            "--user", "65534:65534",
            # /tmp as writable tmpfs (noexec prevents execution from /tmp)
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            # CRITICAL FIX: Workspace mounted READ-ONLY to prevent CoW bypass.
            # Container processes CANNOT modify workspace files directly.
            "-v", f"{workspace_abs}:/workspace:ro",
            # Writable overlay for any container writes — extracted after execution
            "--tmpfs", "/workspace-overlay:rw,size=256m",
            # Working directory
            "-w", rel_workdir,
        ])

        if cidfile:
            cmd_args.insert(2, f"--cidfile={cidfile.as_posix()}")

        # Network isolation flag
        if not network_enabled:
            cmd_args.extend(["--network", "none"])

        # Resource limits flags
        if limits:
            if limits.max_memory_bytes > 0:
                cmd_args.extend(["--memory", f"{limits.max_memory_bytes}b"])
                # Prevent swap exhaustion: set swap limit equal to memory limit
                cmd_args.extend(["--memory-swap", f"{limits.max_memory_bytes}b"])
            if limits.max_pids > 0:
                cmd_args.extend(["--pids-limit", str(limits.max_pids)])
            # CPU limiting
            if limits.max_cpu_percent > 0 and limits.max_cpu_percent < 100:
                cpu_period = 100000  # 100ms default
                cpu_quota = int(cpu_period * limits.max_cpu_percent / 100.0)
                cmd_args.extend(["--cpu-period", str(cpu_period)])
                cmd_args.extend(["--cpu-quota", str(cpu_quota)])

        # Environment variables
        if env:
            for k, v in env.items():
                cmd_args.extend(["-e", f"{k}={v}"])

        # Image and target command
        cmd_args.append(self.image)
        if isinstance(command, str):
            cmd_args.extend(["sh", "-c", command])
        else:
            cmd_args.extend(command)

        return cmd_args

    async def execute(
        self,
        command: str | list[str],
        workspace_root: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | None = None,
        network_enabled: bool = False,
    ) -> ProcessResult:
        if not await self.is_available():
            return ProcessResult(
                command=str(command),
                exit_code=-1,
                stdout="",
                stderr=f"Container engine '{self.name}' is not installed or available on this system.",
                duration_ms=0.0,
            )

        engine = self._engine or "docker"
        # We need a temporary place for the cidfile and the extracted overlay
        tmp_base = Path(tempfile.gettempdir()) / "pulse_sandbox"
        tmp_base.mkdir(parents=True, exist_ok=True)
        
        tx_id = str(uuid.uuid4())
        cidfile = tmp_base / f"{tx_id}.cid"
        overlay_extract_dir = tmp_base / f"{tx_id}_overlay"
        
        docker_cmd = self.build_docker_cmd(
            command=command,
            workspace_root=workspace_root,
            cwd=cwd,
            env=env,
            limits=limits,
            network_enabled=network_enabled,
            cidfile=cidfile,
        )

        try:
            result = await self.process_manager.execute(docker_cmd, cwd=workspace_root, limits=limits)
            
            # Extract overlay if container ID was captured
            if cidfile.exists():
                cid = cidfile.read_text(encoding="utf-8").strip()
                if cid:
                    overlay_extract_dir.mkdir(parents=True, exist_ok=True)
                    # docker cp <cid>:/workspace-overlay/. <extract_dir>
                    # Note: /workspace-overlay might just be empty if untouched, but docker cp needs careful handling
                    # We copy the directory contents
                    cp_cmd = [engine, "cp", f"{cid}:/workspace-overlay", str(overlay_extract_dir)]
                    proc = await asyncio.create_subprocess_exec(
                        *cp_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    
                    # Remove the container now that we've extracted
                    rm_cmd = [engine, "rm", "-f", cid]
                    rm_proc = await asyncio.create_subprocess_exec(
                        *rm_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await rm_proc.wait()
            
            # Create a new result with the overlay path
            return ProcessResult(
                command=result.command,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                truncated=result.truncated,
                pid=result.pid,
                overlay_path=overlay_extract_dir if overlay_extract_dir.exists() else None
            )
        finally:
            if cidfile.exists():
                try:
                    cidfile.unlink()
                except OSError:
                    pass

    async def cleanup(self) -> None:
        await self.process_manager.terminate_all()
