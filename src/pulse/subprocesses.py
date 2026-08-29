"""Cross-platform subprocess isolation and cleanup helpers."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any


def isolated_process_kwargs() -> dict[str, Any]:
    """Return platform options that isolate child console control events.

    Windows children receive a distinct process-group identifier and therefore
    cannot deliver a group-scoped Ctrl+C event to the Pulse or pytest parent.
    POSIX callers use their existing session/process-group policy.
    """

    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


async def terminate_process(
    process: asyncio.subprocess.Process | None,
    *,
    grace_seconds: float = 1.0,
) -> None:
    """Terminate one child without generating console control events."""

    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.1, grace_seconds))
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                return
        await process.wait()
