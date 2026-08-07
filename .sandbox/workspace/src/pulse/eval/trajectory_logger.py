from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# expose ``asdict`` to the global namespace for test convenience
builtins.asdict = asdict

@dataclass(slots=True)
class TrajectoryStep:
    """A single recorded step of an agent run."""

    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    prompt: str | None = None
    tool_name: str | None = None
    tool_args: Mapping[str, Any] | None = None
    reasoning: str | None = None
    token_cost: int | None = None
    test_log: str | None = None
    result: str | None = None

class TrajectoryLogger:
    """Collects and persists a sequence of :class:`TrajectoryStep` objects.
    Stored under ``.agent/trajectories/<task_id>.json``.
    """

    def __init__(self, workspace: Path, task_id: str | None = None) -> None:
        self.workspace = workspace.resolve()
        self.task_id = task_id or uuid.uuid4().hex
        self._steps: list[TrajectoryStep] = []
        self._base = self.workspace / ".agent" / "trajectories"
        self._base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API
    def add_step(
        self,
        *,
        prompt: str | None = None,
        tool_name: str | None = None,
        tool_args: Mapping[str, Any] | None = None,
        reasoning: str | None = None,
        token_cost: int | None = None,
        test_log: str | None = None,
        result: str | None = None,
    ) -> None:
        """Append a new step to the internal buffer."""
        step = TrajectoryStep(
            prompt=prompt,
            tool_name=tool_name,
            tool_args=tool_args,
            reasoning=reasoning,
            token_cost=token_cost,
            test_log=test_log,
            result=result,
        )
        self._steps.append(step)

    def dump(self) -> Path:
        """Write the buffered steps to ``.agent/trajectories/<task_id>.json``.
        Returns the path to the written file.
        """
        out_path = self._base / f"{self.task_id}.json"
        out_path.write_text(json.dumps([asdict(s) for s in self._steps], indent=2), encoding="utf-8")
        return out_path

    def load(self) -> Sequence[TrajectoryStep]:
        """Load a previously persisted trajectory (if it exists) and replace the buffer.
        Returns the loaded list of steps.
        """
        path = self._base / f"{self.task_id}.json"
        if not path.is_file():
            self._steps = []
            return []
        raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        self._steps = [TrajectoryStep(**item) for item in raw]
        return self._steps

    # ------------------------------------------------------------------ Helpers
    @property
    def steps(self) -> Sequence[TrajectoryStep]:
        """Read‑only view of the current steps buffer."""
        return tuple(self._steps)
