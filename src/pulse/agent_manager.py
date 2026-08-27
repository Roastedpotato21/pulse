"""Production-grade Multi-Agent Collaboration System for Pulse.

Provides dynamic task assignment, parallel execution, conflict resolution,
and shared context messaging between specialized agents.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pulse.context import ContextManager
from pulse.core.agent import Agent, AgentRequest
from pulse.core.protocols import LLMProvider
from pulse.streaming import CancellationToken, StreamEvent, StreamEventType
from pulse.task_manager import Task, TaskManager, TaskStatus
from pulse.tool_registry import ToolRegistry


class TaskCategory(Enum):
    """Categories of tasks for dynamic assignment to specialized agents."""
    PLANNING = "planning"
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    GIT = "git"
    GENERAL = "general"


@dataclass(slots=True)
class AgentMessage:
    """A structured message passed between agents via the shared context."""
    sender: str
    recipient: str | None
    category: TaskCategory
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class CollaborationContext:
    """A blackboard for shared context and messages between agents."""
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    def post_message(self, message: AgentMessage) -> None:
        self.messages.append(message)

    def get_messages(self, category: TaskCategory | None = None) -> list[AgentMessage]:
        if category:
            return [m for m in self.messages if m.category == category]
        return self.messages

    def format_for_prompt(self) -> str:
        if not self.messages:
            return ""
        lines = ["--- Shared Collaboration Context ---"]
        for msg in self.messages:
            lines.append(f"[{msg.sender}]: {msg.content}")
        return "\n".join(lines)


class CollaborationAgent:
    """Base interface for specialized collaboration agents."""
    name: str
    capabilities: list[TaskCategory]

    async def execute(
        self,
        task: Task,
        shared_context: CollaborationContext,
        cancellation_token: CancellationToken,
    ) -> AsyncGenerator[StreamEvent, None]:
        raise NotImplementedError


class BaseLLMCollaborationAgent(CollaborationAgent):
    """Base class for LLM-backed specialized agents."""
    def __init__(self, name: str, capabilities: list[TaskCategory], provider: LLMProvider, role_prompt: str, tool_registry: ToolRegistry | None = None):
        self.name = name
        self.capabilities = capabilities
        self._agent = Agent(provider, system_prompt=role_prompt, tool_registry=tool_registry)

    async def execute(
        self,
        task: Task,
        shared_context: CollaborationContext,
        cancellation_token: CancellationToken,
    ) -> AsyncGenerator[StreamEvent, None]:
        
        cancellation_token.raise_if_cancelled()
        
        yield StreamEvent(event_type=StreamEventType.TOOL_START, content=f"[{self.name}] Starting task: {task.title}")
        
        prompt = f"Task Goal: {task.goal}\n\n{shared_context.format_for_prompt()}"
        
        try:
            # We don't have a streaming respond in core Agent yet, so we just await respond
            # In a real implementation we would stream it.
            response = await self._agent.respond(AgentRequest(message=prompt))
            shared_context.post_message(AgentMessage(
                sender=self.name,
                recipient=None,
                category=self.capabilities[0],
                content=response.content
            ))
            yield StreamEvent(event_type=StreamEventType.TOOL_COMPLETE, content=f"[{self.name}] Completed task: {task.title}")
        except Exception as e:
            yield StreamEvent(event_type=StreamEventType.TOOL_FAILED, content=f"[{self.name}] Failed task: {task.title}. Error: {e!s}")
            raise


class PlannerAgent(BaseLLMCollaborationAgent):
    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="Planner",
            capabilities=[TaskCategory.PLANNING, TaskCategory.GENERAL],
            provider=provider,
            role_prompt="You are Pulse's Planner Agent. Analyze the request and shared context, and produce a detailed dependency graph and implementation plan.",
            tool_registry=tool_registry,
        )


class SoftwareEngineerAgent(BaseLLMCollaborationAgent):
    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="SoftwareEngineer",
            capabilities=[TaskCategory.CODING],
            provider=provider,
            role_prompt="You are Pulse's Software Engineer Agent. Turn the shared context and assigned task into a precise, robust implementation. Edit files as necessary.",
            tool_registry=tool_registry,
        )


class ReviewerAgent(BaseLLMCollaborationAgent):
    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="Reviewer",
            capabilities=[TaskCategory.REVIEW],
            provider=provider,
            role_prompt="You are Pulse's Reviewer Agent. Review the proposed solution for correctness, safety, regressions, and missing tests. Propose fixes if necessary.",
            tool_registry=tool_registry,
        )


class TestingAgent(BaseLLMCollaborationAgent):
    __test__ = False

    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="Tester",
            capabilities=[TaskCategory.TESTING],
            provider=provider,
            role_prompt="You are Pulse's Testing Agent. Assess the review and implementation, write and run necessary tests, and ensure full coverage.",
            tool_registry=tool_registry,
        )

# Alias for backward compatibility (to prevent breaking runtime.py and other imports)
TesterAgent = TestingAgent


class DocumentationAgent(BaseLLMCollaborationAgent):
    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="Documentation",
            capabilities=[TaskCategory.DOCUMENTATION],
            provider=provider,
            role_prompt="You are Pulse's Documentation Agent. Ensure all docstrings, READMEs, and architecture docs are up-to-date with the latest code changes.",
            tool_registry=tool_registry,
        )


class GitAgent(BaseLLMCollaborationAgent):
    def __init__(self, provider: LLMProvider, tool_registry: ToolRegistry | None = None):
        super().__init__(
            name="GitAgent",
            capabilities=[TaskCategory.GIT],
            provider=provider,
            role_prompt="You are Pulse's Git Agent. Manage version control, create branches, stage changes, and write semantic commit messages.",
            tool_registry=tool_registry,
        )


class AgentManager:
    """Orchestrates specialized agents, task assignments, and parallel execution."""

    def __init__(
        self,
        task_manager: TaskManager,
        agents: list[CollaborationAgent] | None = None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self.task_manager = task_manager
        self.agents = agents or []
        self.context_manager = context_manager
        self.shared_context = CollaborationContext()

    def register_agent(self, agent: CollaborationAgent) -> None:
        self.agents.append(agent)

    def _select_agent_for_task(self, task: Task) -> CollaborationAgent:
        # Simple heuristic: categorize based on keywords in title/goal
        goal_lower = f"{task.title} {task.goal}".lower()
        
        assigned_category = TaskCategory.GENERAL
        if any(w in goal_lower for w in ["test", "verify", "validate"]):
            assigned_category = TaskCategory.TESTING
        elif any(w in goal_lower for w in ["review", "check", "lint"]):
            assigned_category = TaskCategory.REVIEW
        elif any(w in goal_lower for w in ["doc", "readme", "walkthrough"]):
            assigned_category = TaskCategory.DOCUMENTATION
        elif any(w in goal_lower for w in ["git", "commit", "branch", "pr "]):
            assigned_category = TaskCategory.GIT
        elif any(w in goal_lower for w in ["implement", "code", "write", "fix", "refactor"]):
            assigned_category = TaskCategory.CODING
        elif any(w in goal_lower for w in ["plan", "design", "architecture"]):
            assigned_category = TaskCategory.PLANNING

        # Find best agent
        for agent in self.agents:
            if assigned_category in agent.capabilities:
                return agent
        
        # Fallback to SoftwareEngineer or first available
        for agent in self.agents:
            if TaskCategory.CODING in agent.capabilities:
                return agent
        
        if self.agents:
            return self.agents[0]
            
        raise RuntimeError("No agents available in AgentManager.")

    async def run(self, prompt: str, context: list[str]) -> Any:
        @dataclass(slots=True)
        class AgentManagerResult:
            final_response: str

        for ctx in context:
            self.shared_context.post_message(AgentMessage(
                sender="System",
                recipient=None,
                category=TaskCategory.GENERAL,
                content=ctx
            ))
            
        task = await self.task_manager.create_task(goal=prompt, title=prompt[:45])
        await self.task_manager.queue_task(task.id)
        
        async for event in self.execute_task_graph():
            if event.event_type == StreamEventType.ERROR:
                print(f"ERROR IN AGENT MANAGER: {event.content}")
            
        final_response = "No response generated."
        agent_msgs = [m for m in self.shared_context.messages if m.sender != "System"]
        if agent_msgs:
            final_response = agent_msgs[-1].content
            
        return AgentManagerResult(final_response=final_response)

    async def execute_task_graph(
        self,
        cancellation_token: CancellationToken | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        token = cancellation_token or CancellationToken()
        
        while True:
            token.raise_if_cancelled()
            
            pending_tasks = self.task_manager.list_tasks(status=TaskStatus.QUEUED)
            if not pending_tasks:
                break
                
            # Filter tasks that have all dependencies met
            ready_tasks = []
            for t in pending_tasks:
                unresolved = []
                for dep_id in t.depends_on:
                    dep_task = self.task_manager.get_task(dep_id)
                    if dep_task and dep_task.status != TaskStatus.COMPLETED:
                        unresolved.append(dep_id)
                if not unresolved:
                    ready_tasks.append(t)
                    
            if not ready_tasks:
                yield StreamEvent(event_type=StreamEventType.ERROR, content="Deadlock detected: pending tasks exist but none are ready.")
                break
                
            # Execute ready tasks in parallel
            tasks_to_run = []
            for task in ready_tasks:
                agent = self._select_agent_for_task(task)
                
                tasks_to_run.append((task, agent))

            # Since we need to yield StreamEvents, we can run them concurrently and merge the streams.
            # A simple approach is to use a queue.
            queue: asyncio.Queue[StreamEvent | Exception | None] = asyncio.Queue()
            
            async def agent_runner(t: Task, a: CollaborationAgent) -> None:
                try:
                    async def worker(leased_task: Task) -> str:
                        async for event in a.execute(leased_task, self.shared_context, token):
                            await queue.put(event)  # noqa: B023
                        return "Agent execution finished"

                    await self.task_manager.execute_task(t.id, worker)
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception as e:  # noqa: BLE001
                    await queue.put(e)  # noqa: B023

            runners = [asyncio.create_task(agent_runner(t, a)) for t, a in tasks_to_run]
            
            async def watcher() -> None:
                await asyncio.gather(*runners)  # noqa: B023
                await queue.put(None)  # Sentinel to stop yielding  # noqa: B023
                
            watcher_task = asyncio.create_task(watcher())
            
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    # We might want to handle conflict resolution here.
                    # For now, we yield the error and continue.
                    yield StreamEvent(event_type=StreamEventType.ERROR, content=f"Agent execution failed: {item!s}")
                else:
                    yield item
                    
            # Check for any unhandled exceptions in the watcher task
            await watcher_task
