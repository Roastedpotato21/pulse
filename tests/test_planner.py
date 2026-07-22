import asyncio

from pulse.core.agent import Agent, AgentRequest
from pulse.core.planner import (
    ExecutionPlan,
    PlanAction,
    PlanCondition,
    PlanStep,
    PlanningRequest,
    RequestPlanner,
)
from pulse.core.protocols import StreamChunk
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


def test_request_planner_creates_an_ordered_conditional_plan() -> None:
    plan = asyncio.run(RequestPlanner().plan(PlanningRequest("Inspect the repository and explain the result.")))

    assert [step.action for step in plan.steps] == [PlanAction.CONTEXT, PlanAction.TOOL, PlanAction.LLM]
    assert plan.steps[1].condition is PlanCondition.TOOL_AVAILABLE
    assert plan.steps[2].condition is PlanCondition.NO_TERMINAL_TOOL_RESULT
    assert plan.steps[2].depends_on == ("route-tool",)


def test_execution_plan_rejects_invalid_dependencies() -> None:
    try:
        ExecutionPlan("goal", (PlanStep("answer", PlanAction.LLM, "Answer", depends_on=("missing",)),))
    except ValueError as error:
        assert "earlier steps" in str(error)
    else:
        raise AssertionError("Expected invalid plan dependencies to be rejected.")


class RecordingPlanner:
    def __init__(self) -> None:
        self.was_called = False

    async def plan(self, request: PlanningRequest) -> ExecutionPlan:
        self.was_called = True
        return ExecutionPlan(
            request.message,
            (
                PlanStep("tool", PlanAction.TOOL, "Use the local answer tool."),
                PlanStep("answer", PlanAction.LLM, "Answer if the tool did not finish.", PlanCondition.NO_TERMINAL_TOOL_RESULT, ("tool",)),
            ),
        )


class PlanCheckingTool:
    name = "checked"
    description = "Verifies planning precedes tool execution."
    requires_permission = False

    def __init__(self, planner: RecordingPlanner) -> None:
        self.planner = planner

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.message == "check plan"

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        assert self.planner.was_called
        return ToolResult("Plan executed first")


class UnusedProvider:
    config = object()
    is_configured = True

    async def generate_stream(self, messages):
        raise AssertionError("terminal tool should bypass the provider")
        yield StreamChunk("")


def test_agent_executes_plan_before_tool_dispatch() -> None:
    planner = RecordingPlanner()
    agent = Agent(
        UnusedProvider(),
        system_prompt="system",
        planner=planner,
        tool_registry=ToolRegistry([PlanCheckingTool(planner)]),
    )

    response = asyncio.run(agent.respond(AgentRequest("check plan")))

    assert response.content == "Plan executed first"
    assert planner.was_called
