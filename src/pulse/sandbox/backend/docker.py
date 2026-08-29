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

Security hardening (secret leakage fix):
    - Environment variables are injected via --env-file instead of --env CLI args.
    - This prevents secrets from leaking through the host process table (ps aux).
    - The env file is created with restrictive permissions (0o600 on POSIX).
    - The env file is deleted in a finally block after docker run starts.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import typing
import uuid
from pathlib import Path

from pulse.sandbox.network import NetworkEnforcementLevel, NetworkMode, NetworkPolicy
from pulse.sandbox.process import ProcessEnforcementLevel, ProcessManager, ProcessResult
from pulse.sandbox.resources import ResourceLimits, ResourcePolicy
from pulse.sandbox.secrets import (
    SecretEnforcementLevel,
    SecretMode,
    SecretPolicy,
    build_isolated_environment,
)


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

    async def reconcile(self) -> None:
        """Startup reconciliation to aggressively reap orphaned Pulse containers.

        Queries the engine for containers labeled with `pulse.sandbox.managed=true`
        and force-removes them. This guarantees no leftover processes consume resources.
        """
        if not await self.is_available():
            return
        engine = self._engine
        if not engine:
            return

        proc = await asyncio.create_subprocess_exec(
            engine,
            "ps",
            "-a",
            "-q",
            "--filter",
            "label=pulse.sandbox.managed=true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        cids = stdout.decode().strip().split()
        
        if cids:
            rm_proc = await asyncio.create_subprocess_exec(
                engine,
                "rm",
                "-f",
                *cids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()

    @property
    def name(self) -> str:
        return self._engine or "docker"

    async def is_available(self) -> bool:
        """Check that a Docker or Podman CLI and its daemon are operational."""
        if self._engine:
            return await self._engine_operational(self._engine)
        for candidate in ("docker", "podman"):
            if await self._engine_operational(candidate):
                self._engine = candidate
                return True
        return False

    @staticmethod
    async def _engine_operational(engine: str) -> bool:
        if not shutil.which(engine):
            return False
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                engine,
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
            return proc.returncode == 0
        except (OSError, TimeoutError):
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return False

    def get_network_enforcement_capability(self, policy: NetworkPolicy) -> NetworkEnforcementLevel:
        """Determine what level of security this backend can enforce for the policy.
        
        Docker without root/iptables capabilities can only strictly enforce
        DENY_ALL and LOCALHOST_ONLY (container loopback) via --network none.
        ALLOWLIST and PROXY cannot be strictly enforced against raw sockets.
        """
        if not policy or policy.mode == NetworkMode.ALLOW_ALL:
            return NetworkEnforcementLevel.STRONGLY_ENFORCED
            
        if policy.mode in (NetworkMode.DENY_ALL, NetworkMode.LOCALHOST_ONLY):
            return NetworkEnforcementLevel.STRONGLY_ENFORCED
            
        return NetworkEnforcementLevel.UNSUPPORTED

    def get_secret_enforcement_capability(self, policy: SecretPolicy) -> SecretEnforcementLevel:
        """Determine if this backend can strongly enforce the requested secret isolation policy.
        
        Docker without root/mounts naturally isolates the host filesystem and environment,
        making it trivial to enforce DENY_ALL and ALLOW_EXPLICIT cleanly.
        """
        if not policy or policy.mode == SecretMode.ALLOW_ALL:
            return SecretEnforcementLevel.STRONGLY_ENFORCED
            
        if policy.mode in (SecretMode.DENY_ALL, SecretMode.ALLOW_EXPLICIT):
            return SecretEnforcementLevel.STRONGLY_ENFORCED
            
        return SecretEnforcementLevel.UNSUPPORTED

    def get_process_containment_capability(self) -> ProcessEnforcementLevel:
        """Determine if this backend provides strong process containment.
        
        Docker leverages Linux namespaces and cgroups to strongly contain
        processes, regardless of daemonization/setsid behavior.
        """
        return ProcessEnforcementLevel.STRONGLY_ENFORCED

    @staticmethod
    def _write_env_file(env: dict[str, str], path: Path) -> None:
        """Write environment variables to a file for --env-file injection.

        Security architecture:
            - File is created with restrictive permissions (0o600 on POSIX)
              to prevent other host users from reading secrets.
            - Values are written as KEY=VALUE, one per line.
            - The caller is responsible for deleting the file after use.
        """
        # Open with restrictive permissions on POSIX; on Windows os.open
        # ignores the mode but the file is user-owned by default.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(path), flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            lines = [f"{k}={v}\n" for k, v in env.items()]
            os.write(fd, "".join(lines).encode("utf-8"))
        finally:
            os.close(fd)

    def build_docker_cmd(
        self,
        command: str | list[str],
        workspace_root: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | ResourcePolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        secret_policy: SecretPolicy | None = None,
        cidfile: Path | None = None,
        env_file_path: Path | None = None,
        execution_id: str | None = None,
        overlay_export_path: Path | None = None,
        export_wrapper_path: Path | None = None,
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
            8. Environment injected via --env-file — prevents secret leakage
               through the host process table (ps aux).
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

        if execution_id:
            cmd_args.extend(["--label", f"pulse.sandbox.execution_id={execution_id}"])
        cmd_args.extend(["--label", "pulse.sandbox.managed=true"])

        if overlay_export_path and export_wrapper_path:
            cmd_args.extend(
                [
                    "-v",
                    f"{overlay_export_path.resolve()}:/workspace-export:rw",
                    "-v",
                    f"{export_wrapper_path.resolve()}:/pulse-export-wrapper.sh:ro",
                ]
            )

        # Network isolation flag
        if not network_policy or network_policy.mode in (NetworkMode.DENY_ALL, NetworkMode.LOCALHOST_ONLY):
            cmd_args.extend(["--network", "none"])

        # Resource limits flags
        if limits:
            policy = limits if isinstance(limits, ResourcePolicy) else limits.to_policy()
            if policy.memory_bytes:
                cmd_args.extend(["--memory", f"{policy.memory_bytes}b", "--memory-swap", f"{policy.memory_bytes}b"])
            if policy.max_processes:
                cmd_args.extend(["--pids-limit", f"{policy.max_processes}"])
            if policy.cpu_quota_percent:
                cpus = max(0.01, policy.cpu_quota_percent / 100.0)
                cmd_args.extend(["--cpus", f"{cpus}"])
            if policy.disk_bytes:
                # --storage-opt size= relies on overlayfs backing (xfs/btrfs).
                # Podman supports it natively in most overlay setups. Docker supports it with xfs pquota.
                # If unsupported by the daemon, execution will gracefully fail closed during startup.
                cmd_args.extend(["--storage-opt", f"size={policy.disk_bytes}"])
            # File descriptor limit (Finding #4)
            if policy.max_open_files:
                cmd_args.extend(["--ulimit", f"nofile={policy.max_open_files}:{policy.max_open_files}"])
            # CPU time hard-kill limit
            if policy.cpu_time_seconds is not None:
                cpu_secs = max(1, int(policy.cpu_time_seconds))
                cmd_args.extend(["--ulimit", f"cpu={cpu_secs}:{cpu_secs}"])

        # Environment variables isolation — injected via --env-file to prevent
        # secrets from leaking through the host process table (ps aux).
        safe_env = build_isolated_environment(secret_policy, extra_env=env)
        if env_file_path:
            self._write_env_file(safe_env, env_file_path)
            cmd_args.extend(["--env-file", str(env_file_path)])
        else:
            # Fallback for unit tests calling build_docker_cmd() directly
            # without an env_file_path — uses non-secret minimal env only.
            for k, v in safe_env.items():
                cmd_args.extend(["--env", f"{k}={v}"])

        # Image and target command
        cmd_args.append(self.image)
        if overlay_export_path and export_wrapper_path:
            cmd_args.extend(["sh", "/pulse-export-wrapper.sh"])
            if isinstance(command, str):
                cmd_args.extend(["sh", "-c", command])
            else:
                cmd_args.extend(command)
        elif isinstance(command, str):
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
        limits: ResourceLimits | ResourcePolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        secret_policy: SecretPolicy | None = None,
        execution_id: str | None = None,
        output_callback: typing.Callable[[str, bytes], typing.Awaitable[None]] | None = None,
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
        
        tx_id = execution_id or str(uuid.uuid4())
        cidfile = tmp_base / f"{tx_id}.cid"
        overlay_extract_dir = tmp_base / f"{tx_id}_overlay"
        env_file_path = tmp_base / f"{tx_id}.env"
        export_wrapper_path = tmp_base / f"{tx_id}_export.sh"
        overlay_extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            overlay_extract_dir.chmod(0o777)
        except OSError:
            pass
        self._write_export_wrapper(export_wrapper_path)
        
        docker_cmd = self.build_docker_cmd(
            command=command,
            workspace_root=workspace_root,
            cwd=cwd,
            env=env,
            limits=limits,
            network_policy=network_policy,
            secret_policy=secret_policy,
            cidfile=cidfile,
            env_file_path=env_file_path,
            execution_id=tx_id,
            overlay_export_path=overlay_extract_dir,
            export_wrapper_path=export_wrapper_path,
        )

        completed = False
        try:
            # The container engine enforces resource limits inside the container.
            # Applying POSIX rlimits to the Docker/Podman client itself can prevent
            # the client from starting and does not strengthen containment.
            result = await self.process_manager.execute(
                docker_cmd,
                cwd=workspace_root,
                limits=limits,
                output_callback=output_callback,
                apply_native_limits=False,
            )

            # The wrapper exports the tmpfs overlay before the container exits.
            # Keep the cidfile only for deterministic container cleanup.
            if cidfile.exists():
                cid = cidfile.read_text(encoding="utf-8").strip()
                if cid:
                    rm_cmd = [engine, "rm", "-f", cid]
                    rm_proc = await asyncio.create_subprocess_exec(
                        *rm_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await rm_proc.wait()

            self._validate_exported_overlay(overlay_extract_dir)
            export_marker = overlay_extract_dir / ".pulse-export-complete"
            export_complete = export_marker.is_file()
            export_marker.unlink(missing_ok=True)
            if not export_complete and result.exit_code == 0:
                raise RuntimeError(
                    "Container command succeeded but its tmpfs overlay was not exported."
                )
            if not export_complete:
                shutil.rmtree(overlay_extract_dir, ignore_errors=True)

            # Create a new result with the overlay path
            completed = True
            return ProcessResult(
                command=result.command,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                truncated=result.truncated,
                pid=result.pid,
                overlay_path=overlay_extract_dir if export_complete else None,
                metrics=result.metrics,
                termination_reason=result.termination_reason,
            )
        finally:
            # Always clean temporary files (Finding #3 — cleanup hardening)
            if cidfile.exists():
                try:
                    cidfile.unlink(missing_ok=True)
                except OSError:
                    pass
            if env_file_path.exists():
                try:
                    env_file_path.unlink(missing_ok=True)
                except OSError:
                    pass
            export_wrapper_path.unlink(missing_ok=True)
            if not completed:
                shutil.rmtree(overlay_extract_dir, ignore_errors=True)

    @staticmethod
    def _write_export_wrapper(path: Path) -> None:
        script = (
            b'#!/bin/sh\n'
            b'"$@"\n'
            b'status=$?\n'
            # Do not use ``cp -a`` here. GNU cp tries to preserve metadata on
            # the bind-mount root itself, which a non-root container user
            # cannot chmod/chown, and turns every successful command into 125.
            b'cp -R /workspace-overlay/. /workspace-export/ || exit 125\n'
            b"find /workspace-export -mindepth 1 -exec chmod a+rwX {} \\; || exit 125\n"
            b': > /workspace-export/.pulse-export-complete || exit 125\n'
            b'exit "$status"\n'
        )
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
        try:
            os.write(fd, script)
        finally:
            os.close(fd)
        try:
            path.chmod(0o755)
        except OSError:
            pass

    @staticmethod
    def _validate_exported_overlay(dest: Path) -> None:
        resolved_dest = dest.resolve()
        for item in dest.rglob("*"):
            if item.is_symlink():
                raise RuntimeError("Container overlay export contains a symbolic link.")
            try:
                item.resolve().relative_to(resolved_dest)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    "Container overlay export escaped its destination."
                ) from exc

    async def cleanup(self) -> None:
        await self.process_manager.terminate_all()
        await self.reconcile()
