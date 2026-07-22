import asyncio
from pathlib import Path

from pulse.memory import LongTermMemory
from pulse.multi_agent import AgentManager
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class RecordingRole:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def run(self, request: str, context=()) -> str:
        self.calls.append((request, tuple(context)))
        return self.output


def test_agent_manager_runs_roles_in_planner_coder_reviewer_tester_order() -> None:
    planner, coder, reviewer, tester = (RecordingRole(value) for value in ("plan", "code", "review", "final"))
    manager = AgentManager(planner=planner, coder=coder, reviewer=reviewer, tester=tester)

    result = asyncio.run(manager.run("Add a feature", ("File: app.py",)))

    assert result.plan == "plan" and result.code == "code" and result.review == "review"
    assert result.final_response == "final"
    assert "Planner output:\nplan" in coder.calls[0][1]
    assert "Coding output:\ncode" in reviewer.calls[0][1]
    assert "Reviewer output:\nreview" in tester.calls[0][1]


class ChainTool:
    name = "chain"
    description = "Test stub tool."
    requires_permission = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.calls.append(invocation.name or "")
        return ToolResult(f"ran {self.name}")


def test_agent_manager_can_chain_autonomous_tools() -> None:
    memory = ChainTool("memory")
    repository = ChainTool("search")
    git = ChainTool("git")
    verify = ChainTool("verify")
    registry = ToolRegistry([memory, repository, git, verify])
    planner, coder, reviewer, tester = (RecordingRole(value) for value in ("plan", "code", "review", "final"))
    manager = AgentManager(planner=planner, coder=coder, reviewer=reviewer, tester=tester, tools=registry)

    result = asyncio.run(manager.run("Inspect memory, repository, git status, and then verify the project."))

    assert result.final_response.startswith("Autonomous tool execution summary:")
    assert result.execution_summary.startswith("Autonomous tool execution summary:")
    assert memory.calls == ["memory"]
    assert repository.calls == ["search"]
    assert git.calls == ["git"]
    assert verify.calls == ["verify"]


def test_agent_manager_remembers_successful_workflows_for_future_selection(tmp_path: Path) -> None:
    memory_tool = ChainTool("memory")
    verify_tool = ChainTool("verify")
    registry = ToolRegistry([memory_tool, verify_tool])
    workflow_memory = LongTermMemory(tmp_path)
    planner, coder, reviewer, tester = (RecordingRole(value) for value in ("plan", "code", "review", "final"))
    manager = AgentManager(planner=planner, coder=coder, reviewer=reviewer, tester=tester, tools=registry, memory=workflow_memory)

    asyncio.run(manager.run("Remember the preference and verify the project."))

    learned = asyncio.run(workflow_memory.workflow_recommendations("verify the project"))

    assert learned
    assert learned[0]["success"] is True
    assert learned[0]["tool_sequence"] == ["memory", "verify"]
