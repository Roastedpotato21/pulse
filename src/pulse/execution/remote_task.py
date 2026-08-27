"""Fenced bridge between durable tasks and the remote sandbox."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.sandbox.api import Sandbox
from pulse.sandbox.process import ProcessResult
from pulse.task_manager import Task, TaskManager


@dataclass(slots=True)
class RemoteTaskExecutor:
    """Submit one task attempt to a remote sandbox using its durable ID.

    This adapter is deliberately outside both ``TaskManager`` and the sandbox:
    the task layer owns fencing and recovery while the sandbox owns execution
    policy.  It is the sole bridge responsible for carrying the fenced
    ``remote_execution_id`` across that boundary.
    """

    task_manager: TaskManager
    sandbox: Sandbox

    async def execute(
        self,
        task_id: str,
        command: str | list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> Task:
        await self.sandbox.initialize()
        if getattr(self.sandbox.backend, "name", None) != "remote":
            raise RuntimeError(
                "RemoteTaskExecutor requires a configured remote sandbox backend."
            )

        task = await self.task_manager.start_task(task_id)
        execution_id = await self.task_manager.remote_execution_id_for_task(task.id)
        try:
            result = await self.sandbox.execute_command(
                command,
                cwd=cwd,
                env=env,
                execution_id=execution_id,
            )
        except Exception as err:  # noqa: BLE001 - persists a recoverable task failure
            return await self.task_manager.fail_task(task.id, str(err))

        if self._succeeded(result):
            return await self.task_manager.complete_task(
                task.id, json.dumps(self._result_payload(result), sort_keys=True)
            )
        return await self.task_manager.fail_task(
            task.id,
            result.stderr or f"Remote command exited with code {result.exit_code}.",
        )

    @staticmethod
    def _succeeded(result: ProcessResult) -> bool:
        return result.exit_code == 0 and not result.timed_out

    @staticmethod
    def _result_payload(result: ProcessResult) -> dict[str, Any]:
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "termination_reason": result.termination_reason,
        }
