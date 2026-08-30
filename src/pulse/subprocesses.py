"""Cross-platform subprocess isolation and cleanup helpers."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

_PORTABLE_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


def isolated_subprocess_environment(
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a minimal cross-platform environment with no host credentials."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _PORTABLE_ENV_ALLOWLIST
    }
    environment["PYTHONNOUSERSITE"] = "1"
    if extra:
        environment.update(extra)
    return environment


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
