"""Process manager for tracking, running, and terminating child process trees.

Handles timeout enforcement, graceful SIGTERM -> SIGKILL escalation, zombie cleanup,
and stdout/stderr output buffer truncation.

Security hardening:
    - Explicit close_fds=True for file descriptor isolation.
    - start_new_session=True for process group isolation (prevents signal leakage).
    - Environment sanitization via ResourceLimiter.sanitize_env().
    - Incremental stdout/stderr collection with byte-count limit kill.
    - Kills process immediately if cumulative output exceeds max_output_bytes.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.sandbox.resources import ResourceLimiter, ResourceLimits


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of a managed process execution."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False
    pid: int | None = None
    overlay_path: Path | None = None


class ProcessManager:
    """Async child process manager with lifecycle and timeout enforcement."""

    def __init__(self) -> None:
        self._active_processes: set[asyncio.subprocess.Process] = set()

    @property
    def active_count(self) -> int:
        return len(self._active_processes)

    async def execute(
        self,
        command: str | list[str],
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | None = None,
    ) -> ProcessResult:
        """Run a process asynchronously with timeout, output limits, and isolation.

        Security guarantees:
            - close_fds=True: child does not inherit parent file descriptors.
            - start_new_session=True: child runs in its own process group.
            - Environment sanitized: LD_PRELOAD, PYTHONSTARTUP, etc. stripped.
            - Output incrementally collected: process killed if output exceeds limit.
        """
        limiter = ResourceLimiter(limits)
        cmd_str = command if isinstance(command, str) else " ".join(command)
        work_dir = str(cwd) if cwd else None

        # Sanitize environment — strips dangerous variables
        merged_env = limiter.sanitize_env(env)

        preexec = limiter.make_preexec_fn()

        start_time = time.monotonic()
        timed_out = False
        exit_code = -1
        stdout_bytes = b""
        stderr_bytes = b""
        proc: asyncio.subprocess.Process | None = None

        # Platform-specific subprocess flags
        extra_kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            extra_kwargs["close_fds"] = True
            extra_kwargs["start_new_session"] = True
            extra_kwargs["preexec_fn"] = preexec
        else:
            extra_kwargs["close_fds"] = True

        try:
            if isinstance(command, str):
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                    env=merged_env,
                    **extra_kwargs,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    command[0],
                    *command[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir,
                    env=merged_env,
                    **extra_kwargs,
                )

            self._active_processes.add(proc)
            pid = proc.pid

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    self._collect_output(proc, limiter.limits.max_output_bytes),
                    timeout=limiter.limits.timeout_seconds,
                )
                exit_code = proc.returncode if proc.returncode is not None else 0
            except TimeoutError:
                timed_out = True
                await self._kill_process_gracefully(proc)
                stdout_bytes, stderr_bytes = b"", b"Process execution timed out."
                exit_code = -9

        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception as err:  # noqa: BLE001
            exit_code = -1
            stderr_bytes = str(err).encode("utf-8")
        finally:
            if proc in self._active_processes:
                self._active_processes.remove(proc)

        duration_ms = (time.monotonic() - start_time) * 1000.0

        clean_stdout, stdout_trunc = limiter.truncate_output(stdout_bytes)
        clean_stderr, stderr_trunc = limiter.truncate_output(stderr_bytes)

        return ProcessResult(
            command=cmd_str,
            exit_code=exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=stdout_trunc or stderr_trunc,
            pid=pid if 'pid' in locals() else None,
        )

    async def _collect_output(
        self,
        proc: asyncio.subprocess.Process,
        max_bytes: int,
    ) -> tuple[bytes, bytes]:
        """Collect stdout/stderr incrementally, killing process if output exceeds limit.

        Security rationale:
            Without incremental collection, a malicious process could generate
            infinite stdout, consuming all host memory. This method kills the
            process as soon as cumulative output exceeds the configured limit.
        """
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        chunk_size = 65536  # 64 KB reads

        async def read_stream(stream: asyncio.StreamReader | None, chunks: list[bytes], label: str) -> int:
            total = 0
            if stream is None:
                return 0
            while True:
                try:
                    data = await stream.read(chunk_size)
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception:  # noqa: BLE001
                    break
                if not data:
                    break
                total += len(data)
                chunks.append(data)
                if total > max_bytes:
                    # Output limit exceeded — kill the process
                    await self._kill_process_gracefully(proc)
                    chunks.append(f"\n[{label} TRUNCATED: exceeded {max_bytes} byte limit]".encode())
                    break
            return total

        # Collect stdout and stderr concurrently
        await asyncio.gather(
            read_stream(proc.stdout, stdout_chunks, "STDOUT"),
            read_stream(proc.stderr, stderr_chunks, "STDERR"),
        )

        await proc.wait()

        return b"".join(stdout_chunks), b"".join(stderr_chunks)

    async def _kill_process_gracefully(self, proc: asyncio.subprocess.Process) -> None:
        """Send SIGTERM, wait briefly, then force kill with SIGKILL."""
        if proc.returncode is not None:
            return

        try:
            # 1. Attempt graceful termination
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
                return
            except TimeoutError:
                pass

            # 2. Escalate to force kill
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass  # Already terminated

    async def terminate_all(self) -> None:
        """Kill all tracked active processes."""
        for proc in list(self._active_processes):
            await self._kill_process_gracefully(proc)
        self._active_processes.clear()
