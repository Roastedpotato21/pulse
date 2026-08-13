"""Portable subprocess lifecycle management for sandbox backends."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.sandbox.resources import (
    ExecutionMetrics,
    ResourceController,
    ResourceLimits,
    ResourcePolicy,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome and resource observations for a managed process execution."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False
    pid: int | None = None
    overlay_path: Path | None = None
    metrics: ExecutionMetrics | None = None
    termination_reason: str | None = None


class ProcessManager:
    """Runs isolated process groups with timeout, cancellation and output caps."""
    def __init__(self) -> None:
        self._active_processes: set[asyncio.subprocess.Process] = set()

    @property
    def active_count(self) -> int:
        return len(self._active_processes)

    async def execute(self, command: str | list[str], cwd: Path | str | None = None, env: dict[str, str] | None = None, limits: ResourceLimits | ResourcePolicy | None = None, output_callback: typing.Callable[[str, bytes], typing.Awaitable[None]] | None = None) -> ProcessResult:
        controller = ResourceController(limits if isinstance(limits, ResourcePolicy) else (limits or ResourceLimits()).to_policy())
        policy = controller.policy
        cmd_str = command if isinstance(command, str) else " ".join(command)
        extra_kwargs: dict[str, Any] = {"close_fds": True}
        if sys.platform != "win32":
            extra_kwargs.update(start_new_session=True, preexec_fn=controller.make_preexec_fn())
        else:
            extra_kwargs["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)

        proc: asyncio.subprocess.Process | None = None
        stdout = b""
        stderr = b""
        reason: str | None = None
        controller.monitor.start()
        try:
            if isinstance(command, str):
                proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd) if cwd else None, env=controller.sanitize_env(env), **extra_kwargs)
            else:
                proc = await asyncio.create_subprocess_exec(command[0], *command[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd) if cwd else None, env=controller.sanitize_env(env), **extra_kwargs)
            self._active_processes.add(proc)
            try:
                stdout, stderr, output_limited = await asyncio.wait_for(self._collect_output(proc, policy.max_output_bytes, output_callback), timeout=policy.wall_time_seconds)
                if output_limited:
                    reason = "output_limit"
            except TimeoutError:
                reason = "timeout"
                await self._terminate_tree(proc, policy.termination_grace_seconds)
                stderr += b"\nProcess execution timed out."
            except asyncio.CancelledError:
                reason = "cancelled"
                await self._terminate_tree(proc, policy.termination_grace_seconds)
                raise
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001, execution boundaries return structured result
            reason = reason or "launch_error"
            stderr += str(error).encode("utf-8", errors="replace")
        finally:
            if proc is not None:
                if proc.returncode is None:
                    await self._terminate_tree(proc, policy.termination_grace_seconds)
                self._active_processes.discard(proc)

        exit_code = -9 if reason == "timeout" else (proc.returncode if proc and proc.returncode is not None else -1)
        clean_stdout, stdout_truncated = self._truncate(stdout, policy.max_output_bytes)
        clean_stderr, stderr_truncated = self._truncate(stderr, policy.max_output_bytes)
        metrics = controller.monitor.finish(output_bytes=len(stdout) + len(stderr), exit_status=exit_code, termination_reason=reason)
        if reason == "output_limit":
            marker = "\n... [OUTPUT TRUNCATED BY SANDBOX RESOURCE LIMITER]"
            if len(clean_stdout) >= len(clean_stderr):
                clean_stdout += marker
            else:
                clean_stderr += marker
        return ProcessResult(cmd_str, exit_code, clean_stdout, clean_stderr, metrics.elapsed_ms, reason == "timeout", stdout_truncated or stderr_truncated or reason == "output_limit", proc.pid if proc else None, metrics=metrics, termination_reason=reason)

    async def _collect_output(self, proc: asyncio.subprocess.Process, max_bytes: int, output_callback: typing.Callable[[str, bytes], typing.Awaitable[None]] | None = None) -> tuple[bytes, bytes, bool]:
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        total = 0
        exceeded = False
        lock = asyncio.Lock()

        async def read_stream(stream: asyncio.StreamReader | None, name: str) -> None:
            nonlocal total, exceeded
            if stream is None:
                return
            while data := await stream.read(65_536):
                async with lock:
                    if output_callback:
                        try:
                            await output_callback(name, data)
                        except OSError:
                            logger.debug("output_callback failed for stream %s", name)
                    remaining = max_bytes - total
                    if remaining <= 0:
                        exceeded = True
                    else:
                        chunks[name].append(data[:remaining])
                        total += min(len(data), remaining)
                        exceeded = len(data) > remaining
                if exceeded:
                    await self._terminate_tree(proc, 0.1)
                    return

        await asyncio.gather(read_stream(proc.stdout, "stdout"), read_stream(proc.stderr, "stderr"))
        await proc.wait()
        return b"".join(chunks["stdout"]), b"".join(chunks["stderr"]), exceeded

    @staticmethod
    def _truncate(content: bytes, max_bytes: int) -> tuple[str, bool]:
        if len(content) <= max_bytes:
            return content.decode("utf-8", errors="replace"), False
        return content[:max_bytes].decode("utf-8", errors="ignore"), True

    async def _terminate_tree(self, proc: asyncio.subprocess.Process, grace_seconds: float) -> None:
        """Terminate the complete process tree, then force reap it if needed.

        Security architecture:
            On POSIX, ``start_new_session=True`` places the child in a new
            process group whose pgid equals the child PID.  ``killpg()``
            sends signals to every process in that group, covering all
            descendants that have NOT called ``setsid()`` / ``setpgid()``.

        Limitations:
            A descendant that calls ``setsid()`` or ``setpgid(0, 0)``
            creates a new process group and **escapes** ``killpg()``.
            This is a fundamental POSIX limitation.  Container backends
            (Docker/Podman) provide PID-namespace isolation which is the
            definitive containment boundary for untrusted code.
        """
        if proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
                await proc.wait()
                return

            # Cache pgid once to avoid PID-recycling race on repeated lookups.
            try:
                pgid = os.getpgid(proc.pid)
            except (ProcessLookupError, PermissionError):
                # Process already exited — nothing to kill.
                return

            # 1. Graceful SIGTERM to entire process group.
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return

            # 2. Wait for the grace period.
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            except TimeoutError:
                # 3. Escalate to SIGKILL.
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

            # 4. Reap the direct child.
            await proc.wait()

            # 5. Best-effort reap of any remaining group members.
            self._reap_remaining_group(pgid)

        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _reap_remaining_group(pgid: int) -> None:
        """Best-effort reap of orphaned members still in *pgid*.

        Uses ``os.waitpid(-pgid, WNOHANG)`` which reaps any child of the
        current process whose process-group equals *pgid*.  This covers
        grandchildren that were re-parented to PID 1 / subreaper but are
        still in the original process group.

        Silently ignored on platforms where this is unsupported.
        """
        if sys.platform == "win32":
            return
        for _ in range(64):  # bounded loop to avoid infinite reaping
            try:
                pid, _ = os.waitpid(-pgid, os.WNOHANG)
                if pid == 0:
                    break  # no more waitable children in this group
            except ChildProcessError:
                break  # no children to wait for
            except OSError:
                break

    async def terminate_all(self) -> None:
        """Kill all tracked active processes and reap them."""
        await asyncio.gather(*(self._terminate_tree(proc, 0.1) for proc in list(self._active_processes)), return_exceptions=True)
        self._active_processes.clear()
