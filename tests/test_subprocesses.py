from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from pulse.subprocesses import isolated_process_kwargs, terminate_process
from pulse.verification import VerificationEngine


def test_windows_children_receive_an_isolated_process_group() -> None:
    kwargs = isolated_process_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kwargs == {}


@pytest.mark.anyio
async def test_termination_uses_terminate_then_bounded_kill() -> None:
    class HangingProcess:
        returncode: int | None = None
        terminated = False
        killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            if self.killed:
                return -9
            await asyncio.Event().wait()
            return 0

    process = HangingProcess()
    await terminate_process(process, grace_seconds=0.01)  # type: ignore[arg-type]

    assert process.terminated
    assert process.killed


@pytest.mark.anyio
async def test_verification_launches_an_isolated_child(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CompletedStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def read(self, _limit: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    class CompletedProcess:
        returncode = 0
        pid = 1234
        stdout = CompletedStream([b"passed"])
        stderr = CompletedStream([])

        async def wait(self) -> int:
            return self.returncode

    async def fake_create(*_command: str, **kwargs: object) -> CompletedProcess:
        captured.update(kwargs)
        return CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = await VerificationEngine._run_command(("pytest",), Path.cwd())

    assert result == (0, "passed", "")
    assert captured.get("creationflags", 0) == isolated_process_kwargs().get(
        "creationflags", 0
    )
