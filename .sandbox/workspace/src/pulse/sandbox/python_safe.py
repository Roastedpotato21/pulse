"""Policy-checked safe Python and package environment execution wrapper."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from pulse.sandbox.policy import ActionType, PolicyDecision
from pulse.sandbox.process import ProcessResult

if TYPE_CHECKING:
    from pulse.sandbox.api import Sandbox


class SafePython:
    """Provides policy-gated access to Python interpreter and virtual environments."""

    def __init__(self, sandbox: Sandbox, python_executable: str | None = None) -> None:
        self.sandbox = sandbox
        self.python_executable = python_executable or sys.executable

    async def run_script(self, script_path: str, args: list[str] | None = None) -> ProcessResult:
        decision = self.sandbox.policy.evaluate(ActionType.PYTHON, script_path)
        if decision == PolicyDecision.DENY:
            return ProcessResult(
                command=f"python {script_path}",
                exit_code=-1,
                stdout="",
                stderr="Policy denied Python script execution.",
                duration_ms=0.0,
            )

        cmd = [self.python_executable, script_path] + (args or [])
        return await self.sandbox.execute_command(cmd)

    async def run_module(self, module: str, args: list[str] | None = None) -> ProcessResult:
        decision = self.sandbox.policy.evaluate(ActionType.PYTHON, f"-m {module}")
        if decision == PolicyDecision.DENY:
            return ProcessResult(
                command=f"python -m {module}",
                exit_code=-1,
                stdout="",
                stderr="Policy denied Python module execution.",
                duration_ms=0.0,
            )

        cmd = [self.python_executable, "-m", module] + (args or [])
        return await self.sandbox.execute_command(cmd)

    async def run_uv(self, uv_args: list[str]) -> ProcessResult:
        decision = self.sandbox.policy.evaluate(ActionType.PYTHON, f"uv {' '.join(uv_args)}")
        if decision == PolicyDecision.DENY:
            return ProcessResult(
                command=f"uv {' '.join(uv_args)}",
                exit_code=-1,
                stdout="",
                stderr="Policy denied uv command execution.",
                duration_ms=0.0,
            )

        cmd = ["uv"] + uv_args
        return await self.sandbox.execute_command(cmd)
