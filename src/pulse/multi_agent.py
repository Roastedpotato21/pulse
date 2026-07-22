"""Role-based multi-agent orchestration built on Pulse's core Agent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from pulse.core.agent import Agent, AgentRequest
from pulse.core.protocols import LLMProvider
from pulse.memory import LongTermMemory
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class RoleAgent(Protocol):
    async def run(self, request: str, context: Sequence[str] = ()) -> str: ...


class _Specialist:
    """Thin role adapter; shared orchestration stays outside provider details."""

    def __init__(self, provider: LLMProvider, role_prompt: str) -> None:
        self._agent = Agent(provider, system_prompt=role_prompt)

    async def run(self, request: str, context: Sequence[str] = ()) -> str:
        response = await self._agent.respond(AgentRequest(message=request, context=context))
        return response.content


class PlannerAgent(_Specialist):
    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(provider, "You are Pulse's Planner Agent. Produce a concise, ordered implementation plan and identify risks.")


class CodingAgent(_Specialist):
    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(provider, "You are Pulse's Coding Agent. Turn the approved plan and context into a precise implementation response.")


class ReviewerAgent(_Specialist):
    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(provider, "You are Pulse's Reviewer Agent. Review the proposed solution for correctness, safety, regressions, and missing tests.")


class TestingAgent(_Specialist):
    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(provider, "You are Pulse's Testing Agent. Assess the review and implementation, state verification needs, then provide the final user-facing response.")


@dataclass(frozen=True, slots=True)
class MultiAgentResult:
    plan: str
    code: str
    review: str
    testing: str
    execution_summary: str = ""

    @property
    def final_response(self) -> str:
        return self.testing or self.execution_summary


class AgentManager:
    """Executes the Planner → Coder → Reviewer → Tester workflow asynchronously."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        planner: RoleAgent | None = None,
        coder: RoleAgent | None = None,
        reviewer: RoleAgent | None = None,
        tester: RoleAgent | None = None,
        tools: ToolRegistry | None = None,
        memory: LongTermMemory | None = None,
    ) -> None:
        if provider is None and not all((planner, coder, reviewer, tester)):
            raise ValueError("Provide a model provider or all role agents.")
        self._tools = tools
        self._memory = memory
        self.planner = planner or PlannerAgent(provider)  # type: ignore[arg-type]
        self.coder = coder or CodingAgent(provider)  # type: ignore[arg-type]
        self.reviewer = reviewer or ReviewerAgent(provider)  # type: ignore[arg-type]
        self.tester = tester or TestingAgent(provider)  # type: ignore[arg-type]

    async def run(self, request: str, context: Sequence[str] = ()) -> MultiAgentResult:
        tool_summary = await self._run_autonomous_tool_workflow(request, context)
        if tool_summary:
            return MultiAgentResult(tool_summary, tool_summary, tool_summary, tool_summary, execution_summary=tool_summary)

        plan = await self.planner.run(request, context)
        code = await self.coder.run(request, (*context, f"Planner output:\n{plan}"))
        review = await self.reviewer.run(request, (f"Planner output:\n{plan}", f"Coding output:\n{code}"))
        testing = await self.tester.run(
            request,
            (f"Planner output:\n{plan}", f"Coding output:\n{code}", f"Reviewer output:\n{review}"),
        )
        return MultiAgentResult(plan, code, review, testing)

    async def _run_autonomous_tool_workflow(self, request: str, context: Sequence[str] = ()) -> str | None:
        if self._tools is None:
            return None

        ordered_steps = self._infer_tool_chain(request, context)
        if not ordered_steps:
            return None

        remembered_sequence = await self._remembered_tool_sequence(request)
        if remembered_sequence:
            ordered_steps = self._reorder_by_memory(ordered_steps, remembered_sequence)

        results: list[str] = []
        executed: list[str] = []
        success = True
        errors: list[str] = []
        for index, (name, invocation) in enumerate(ordered_steps, start=1):
            try:
                tool_result = await self._tools.execute(invocation)
            except Exception as exc:  # pragma: no cover - defensive branch for runtime faults
                success = False
                errors.append(f"{index}. {name}: error: {exc}")
                results.append(f"{index}. {name}: error: {exc}")
                continue

            if tool_result is None:
                success = False
                errors.append(f"{index}. {name}: no matching tool available.")
                results.append(f"{index}. {name}: no matching tool available.")
                continue

            executed.append(name)
            result_text = str(tool_result.content).strip() or f"{name} completed without a content response."
            if tool_result.metadata.get("permission_denied"):
                success = False
                result_text = f"Permission denied for {name}."
            results.append(f"{index}. {name}: {result_text}")

        if self._memory:
            await self._memory.record_workflow(request, tuple(executed), success=success, error="\n".join(errors) if errors else None, summary="\n".join(results))

        if not results:
            return None

        return "Autonomous tool execution summary:\n" + "\n".join(results)

    def _infer_tool_chain(self, request: str, context: Sequence[str] = ()) -> list[tuple[str, ToolInvocation]]:
        normalized = request.lower()
        chain: list[tuple[str, ToolInvocation]] = []

        if any(term in normalized for term in ("memory", "remember", "preference")):
            chain.append(("memory", ToolInvocation(name="memory", arguments={"query": request})))

        if any(term in normalized for term in ("search", "repository", "repo", "index", "symbol", "symbols", "find file", "find")):
            chain.append(("search", ToolInvocation(name="search", arguments={"query": request})))

        if any(term in normalized for term in ("git", "branch", "commit", "status", "diff")):
            chain.append(("git", ToolInvocation(name="git")))

        if any(term in normalized for term in ("edit", "change", "update", "modify", "fix", "implement", "write")):
            target = self._find_file_target(request, context)
            if target:
                chain.append(("edit", ToolInvocation(name="edit", arguments={"file": target, "content": request, "reason": request, "approve": self._approve_edit})))

        if any(term in normalized for term in ("verify", "test", "tests", "validation", "check build", "run tests")):
            chain.append(("verify", ToolInvocation(name="verify")))

        return chain

    async def _remembered_tool_sequence(self, request: str) -> list[str]:
        if self._memory is None:
            return []
        recommendations = await self._memory.workflow_recommendations(request, limit=4)
        if not recommendations:
            return []
        tool_sequence = []
        for recommendation in recommendations:
            tool_sequence.extend(recommendation.get("tool_sequence", []))
        return list(dict.fromkeys(tool_sequence))

    def _reorder_by_memory(self, steps: list[tuple[str, ToolInvocation]], remembered_sequence: list[str]) -> list[tuple[str, ToolInvocation]]:
        if not remembered_sequence:
            return steps
        priority = {name: index for index, name in enumerate(remembered_sequence)}
        return sorted(steps, key=lambda item: (priority.get(item[0], len(priority)), item[0]))

    def _find_file_target(self, request: str, context: Sequence[str] = ()) -> str | None:
        match = re.search(r"([A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]+)", request)
        if match:
            return match.group(1)
        for item in context:
            if item.startswith("File:"):
                path = item.split("\n", 1)[0].replace("File:", "", 1).strip()
                if path:
                    return path
        return None

    async def _approve_edit(self, proposal: Any) -> bool:
        return True
