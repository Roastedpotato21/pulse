from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pulse.config import AgentConfig, LoggingConfig, ModelConfig, SandboxConfig
from pulse.mutations import MutationTracker
from pulse.task_manager import TaskManager
from pulse.telemetry import TelemetryLogger, correlation_scope
from pulse.tool_policy import ToolRisk
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class CorrelatedTool:
    name = "status"
    description = "fixture"
    requires_permission = False
    capability = "status"
    risk = ToolRisk.LOW
    schema = None

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult("ok")


def test_correlation_id_flows_through_telemetry_tool_task_and_mutation(
    tmp_path: Path,
) -> None:
    telemetry_path = tmp_path / ".agent" / "logs" / "telemetry.jsonl"
    telemetry = TelemetryLogger(telemetry_path)
    registry = ToolRegistry([CorrelatedTool()], telemetry=telemetry)
    manager = TaskManager(tmp_path, telemetry=telemetry)
    mutations = MutationTracker(tmp_path)

    async def exercise() -> tuple[ToolResult, str]:
        result = await registry.execute(ToolInvocation(name="status"))
        task = await manager.create_task("correlated task")
        assert result is not None
        return result, task.metadata["correlation_id"]

    with correlation_scope("request-123"):
        with mutations.transaction():
            (tmp_path / "changed.txt").write_text("changed", encoding="utf-8")
        tool_result, task_correlation = asyncio.run(exercise())

    mutation = next(iter(mutations.history()))
    telemetry_events = [
        json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert mutation["correlation_id"] == "request-123"
    assert tool_result.metadata["correlation_id"] == "request-123"
    assert task_correlation == "request-123"
    assert {event["correlation_id"] for event in telemetry_events} == {"request-123"}


def test_logging_config_remains_backward_compatible(tmp_path: Path) -> None:
    config = AgentConfig(
        agent_name="Pulse",
        mode="single-model",
        model=ModelConfig("provider", "model", 0.2),
        sandbox=SandboxConfig(tmp_path, True, True, False),
        logging=LoggingConfig(tmp_path / "actions.jsonl"),
    )

    assert config.logging.telemetry_log is None
