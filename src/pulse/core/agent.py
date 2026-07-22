"""CLI- and provider-independent orchestration for Pulse requests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from pulse.core.planner import ExecutionPlan, PlanAction, PlanCondition, PlanGenerator, PlanningRequest, RequestPlanner
from pulse.core.protocols import LLMProvider, StreamChunk
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


@dataclass(frozen=True, slots=True)
class AgentRequest:
    message: str
    conversation_id: str = "default"
    context: Sequence[str] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    content: str
    conversation_id: str
    request_id: str
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    role: str
    content: str


class ConversationStore(Protocol):
    async def read(self, conversation_id: str) -> list[ConversationMessage]: ...

    async def append(self, conversation_id: str, message: ConversationMessage) -> None: ...


class ContextSource(Protocol):
    async def context_for(self, request: AgentRequest) -> Sequence[str]: ...


class InMemoryConversationStore:
    """Safe default history store; replace it with persistent memory via DI."""

    def __init__(self) -> None:
        self._messages: defaultdict[str, list[ConversationMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def read(self, conversation_id: str) -> list[ConversationMessage]:
        async with self._lock:
            return list(self._messages[conversation_id])

    async def append(self, conversation_id: str, message: ConversationMessage) -> None:
        async with self._lock:
            self._messages[conversation_id].append(message)


class Agent:
    """Coordinates request analysis, optional tools, memory, and model streaming.

    The Agent depends only on protocols. CLI adapters supply requests, providers
    implement ``LLMProvider``, and future memory/planning/tool systems can be
    injected without changing this public API.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        system_prompt: str,
        conversation_store: ConversationStore | None = None,
        context_source: ContextSource | None = None,
        planner: PlanGenerator | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._system_prompt = system_prompt
        self._store = conversation_store or InMemoryConversationStore()
        self._context_source = context_source
        self._planner = planner or RequestPlanner()
        self._tool_registry = tool_registry or ToolRegistry()

    async def respond(self, request: AgentRequest) -> AgentResponse:
        request_id = str(request.metadata.get("request_id") or uuid4())
        request = AgentRequest(
            message=request.message,
            conversation_id=request.conversation_id,
            context=request.context,
            metadata={**request.metadata, "request_id": request_id},
        )
        chunks: list[str] = []
        response_metadata: dict[str, Any] = {}
        tool_name: str | None = None
        async for chunk in self.stream(request):
            chunks.append(chunk.content)
            response_metadata.update(chunk.metadata)
            tool_name = str(chunk.metadata.get("tool_name")) if chunk.metadata.get("tool_name") else tool_name
        return AgentResponse(
            content="".join(chunks),
            conversation_id=request.conversation_id,
            request_id=request_id,
            tool_name=tool_name,
            metadata=response_metadata,
        )

    async def stream(self, request: AgentRequest) -> AsyncGenerator[StreamChunk, None]:
        """Analyze and execute one request, yielding the final answer incrementally."""
        if not request.message.strip():
            raise ValueError("Agent requests must include a message.")

        request_id = str(request.metadata.get("request_id") or uuid4())
        normalized_request = AgentRequest(
            message=request.message,
            conversation_id=request.conversation_id,
            context=request.context,
            metadata={**request.metadata, "request_id": request_id},
        )
        await self._store.append(normalized_request.conversation_id, ConversationMessage("user", normalized_request.message))
        plan = await self._planner.plan(PlanningRequest(normalized_request.message, dict(normalized_request.metadata)))
        context = list(normalized_request.context)
        tool_result: ToolResult | None = None
        tool = None

        for step in plan.steps:
            if step.action is PlanAction.CONTEXT:
                if self._context_source:
                    context.extend(await self._context_source.context_for(normalized_request))
            elif step.action is PlanAction.TOOL:
                invocation = ToolInvocation(
                    name=normalized_request.metadata.get("tool_name"),
                    message=normalized_request.message,
                    metadata=normalized_request.metadata,
                )
                tool = self._tool_registry.match(invocation)
                if tool and self._condition_allows(step.condition, tool_result, tool_available=True):
                    tool_result = await self._tool_registry.execute(invocation)
                    if tool_result and tool_result.terminal:
                        await self._store.append(normalized_request.conversation_id, ConversationMessage("assistant", tool_result.content))
                        yield StreamChunk(tool_result.content, {"request_id": request_id, "tool_name": tool.name, "plan": plan, **tool_result.metadata})
                        return
            elif step.action is PlanAction.LLM and self._condition_allows(step.condition, tool_result):
                messages = await self._messages_for(normalized_request, context, tool_result, plan)
                output: list[str] = []
                async for chunk in self._provider.generate_stream(messages):
                    output.append(chunk.content)
                    yield StreamChunk(chunk.content, {"request_id": request_id, "tool_name": tool.name if tool else None, "plan": plan, **chunk.metadata})
                await self._store.append(normalized_request.conversation_id, ConversationMessage("assistant", "".join(output)))
                return

        raise RuntimeError("Execution plan did not contain an executable response step.")

    @staticmethod
    def _condition_allows(
        condition: PlanCondition, tool_result: ToolResult | None, *, tool_available: bool = False
    ) -> bool:
        if condition is PlanCondition.TOOL_AVAILABLE:
            return tool_available
        if condition is PlanCondition.NO_TERMINAL_TOOL_RESULT:
            return not (tool_result and tool_result.terminal)
        return True

    async def _messages_for(self, request: AgentRequest, context: list[str], tool_result: ToolResult | None, plan: ExecutionPlan) -> list[dict[str, str]]:
        history = await self._store.read(request.conversation_id)

        messages = [{"role": "system", "content": self._system_prompt}]
        messages.extend({"role": item.role, "content": item.content} for item in history)
        if context:
            messages.append({"role": "system", "content": "Approved context:\n" + "\n\n".join(context)})
        if plan:
            messages.append({"role": "system", "content": "Execution plan:\n" + "\n".join(plan.render())})
        if tool_result:
            messages.append({"role": "tool", "content": tool_result.content})
        return messages
