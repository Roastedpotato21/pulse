"""Provider- and tool-independent request planning primitives for Pulse."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class PlanAction(StrEnum):
    """Work the agent runtime can perform for a plan step."""

    CONTEXT = "context"
    TOOL = "tool"
    LLM = "llm"


class PlanCondition(StrEnum):
    """Runtime conditions used to select a branch without coupling to tools."""

    ALWAYS = "always"
    TOOL_AVAILABLE = "tool_available"
    NO_TERMINAL_TOOL_RESULT = "no_terminal_tool_result"


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    """The small request shape planners need; adapters keep richer request data."""

    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One declarative unit of work in an execution plan."""

    id: str
    action: PlanAction
    description: str
    condition: PlanCondition = PlanCondition.ALWAYS
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """An immutable, validated plan suitable for audit and future persistence."""

    goal: str
    steps: tuple[PlanStep, ...]

    def __post_init__(self) -> None:
        if not self.goal.strip() or not self.steps:
            raise ValueError("Plans require a goal and at least one step.")
        completed: set[str] = set()
        for step in self.steps:
            if not step.id or step.id in completed:
                raise ValueError("Plan step IDs must be unique and non-empty.")
            if not step.description.strip() or not set(step.depends_on).issubset(completed):
                raise ValueError("Plan steps may only depend on earlier steps.")
            completed.add(step.id)

    def render(self) -> tuple[str, ...]:
        """Human-readable steps for model context and audit records."""
        return tuple(f"{number}. {step.description}" for number, step in enumerate(self.steps, start=1))


class PlanGenerator(Protocol):
    """Extension point for policy, model, or workflow-specific planners."""

    async def plan(self, request: PlanningRequest) -> ExecutionPlan: ...


class RequestPlanner:
    """Default deterministic planner for safe, inspectable request execution.

    It intentionally knows nothing about providers or registered tools. The
    runtime evaluates the two conditional branches when it executes the plan.
    Custom planners can replace this class through dependency injection.
    """

    async def plan(self, request: PlanningRequest) -> ExecutionPlan:
        goal = request.message.strip()
        if not goal:
            raise ValueError("Planning requests must include a message.")
        requested_tool = request.metadata.get("tool_name")
        tool_description = (
            f"Run requested tool '{requested_tool}' if it is available."
            if requested_tool
            else "Run a matching local tool if one can handle this request."
        )
        return ExecutionPlan(
            goal=goal,
            steps=(
                PlanStep("collect-context", PlanAction.CONTEXT, "Collect approved context required for the request."),
                PlanStep("route-tool", PlanAction.TOOL, tool_description, PlanCondition.TOOL_AVAILABLE, ("collect-context",)),
                PlanStep(
                    "generate-response",
                    PlanAction.LLM,
                    "Generate the final response from the gathered context and tool results.",
                    PlanCondition.NO_TERMINAL_TOOL_RESULT,
                    ("route-tool",),
                ),
            ),
        )
