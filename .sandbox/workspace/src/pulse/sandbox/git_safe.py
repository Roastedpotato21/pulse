"""Policy-checked safe Git operations wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pulse.sandbox.policy import ActionType, PolicyDecision
from pulse.sandbox.process import ProcessResult

if TYPE_CHECKING:
    from pulse.sandbox.api import Sandbox


class SafeGit:
    """Provides policy-gated access to repository Git operations."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    async def _run_git(self, subcmd: list[str]) -> ProcessResult:
        decision = self.sandbox.policy.evaluate(ActionType.GIT, " ".join(subcmd))
        if decision == PolicyDecision.DENY:
            return ProcessResult(
                command=f"git {' '.join(subcmd)}",
                exit_code=-1,
                stdout="",
                stderr="Policy denied Git operation.",
                duration_ms=0.0,
            )

        cmd = ["git"] + subcmd
        return await self.sandbox.execute_command(cmd)

    async def status(self) -> ProcessResult:
        return await self._run_git(["status", "--porcelain"])

    async def diff(self) -> ProcessResult:
        return await self._run_git(["diff"])

    async def add(self, target: str = ".") -> ProcessResult:
        return await self._run_git(["add", target])

    async def commit(self, message: str) -> ProcessResult:
        return await self._run_git(["commit", "-m", message])

    async def checkout(self, branch: str) -> ProcessResult:
        return await self._run_git(["checkout", branch])

    async def restore(self, path: str) -> ProcessResult:
        return await self._run_git(["restore", path])
