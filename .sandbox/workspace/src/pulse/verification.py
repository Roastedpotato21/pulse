"""Async project test discovery and verification, independent of Pulse interfaces."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VerificationTarget:
    framework: str
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The outcome of a verification run, including diagnostics and retries."""

    success: bool
    framework: str | None
    command: tuple[str, ...] = ()
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    analysis: str = ""
    attempts: int = 0
    repairs_attempted: int = 0


CommandRunner = Callable[[tuple[str, ...], Path], Awaitable[tuple[int, str, str]]]
RepairHandler = Callable[[VerificationResult], Awaitable[bool]]


class VerificationEngine:
    """Detects and runs a project's native test command.

    A repair handler is deliberately optional. An approved autonomous workflow
    can use it to edit code after a failure and retry, while this service stays
    independent of agents, providers, tools, and the CLI.
    """

    def __init__(self, workspace: Path, *, runner: CommandRunner | None = None, max_retries: int = 3) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.workspace = workspace.resolve()
        self.max_retries = max_retries
        self._runner = runner or self._run_command

    def detect(self) -> VerificationTarget | None:
        """Select a deterministic test runner for the current workspace."""
        if any((self.workspace / file).exists() for file in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")):
            return VerificationTarget("pytest", (sys.executable, "-m", "pytest"))
        if (self.workspace / "package.json").exists():
            return VerificationTarget("npm", ("npm", "test"))
        if (self.workspace / "pom.xml").exists():
            return VerificationTarget("maven", ("mvn", "test"))
        if any((self.workspace / file).exists() for file in ("build.gradle", "build.gradle.kts", "gradlew", "gradlew.bat")):
            return VerificationTarget("gradle", ("gradle", "test"))
        return None

    async def verify(self, *, repair: RepairHandler | None = None) -> VerificationResult:
        """Run tests, analyze failures, and retry successful repairs up to three times."""
        target = self.detect()
        if target is None:
            return VerificationResult(False, None, analysis="No supported test runner was detected.")

        repairs = 0
        while True:
            return_code, stdout, stderr = await self._runner(target.command, self.workspace)
            result = VerificationResult(
                success=return_code == 0,
                framework=target.framework,
                command=target.command,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                analysis=self.analyze_errors(stdout, stderr, return_code),
                attempts=repairs + 1,
                repairs_attempted=repairs,
            )
            if result.success or repair is None or repairs >= self.max_retries:
                return result
            if not await repair(result):
                return result
            repairs += 1

    @staticmethod
    def analyze_errors(stdout: str, stderr: str, return_code: int) -> str:
        """Produce a stable short diagnostic suitable for a repair step."""
        output = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
        if return_code == 0:
            return "Tests passed."
        if not output:
            return f"Test command failed with exit code {return_code} and produced no output."
        markers = ("AssertionError", "ModuleNotFoundError", "ImportError", "SyntaxError", "TypeError", "FAIL", "ERROR")
        relevant = [line.strip() for line in output.splitlines() if any(marker in line for marker in markers)]
        excerpt = relevant or [line.strip() for line in output.splitlines() if line.strip()]
        return "\n".join(excerpt[:8])

    @staticmethod
    async def _run_command(command: tuple[str, ...], workspace: Path) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return 127, "", str(error)
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
