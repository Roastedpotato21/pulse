"""Unit tests for the Pulse Streaming Execution Engine (pulse.streaming).

Tests cover:
- End-to-end streaming event sequence generation
- LLM token-by-token streaming via AsyncGenerator
- Tool execution progress event emissions
- CancellationToken interruption and graceful halt
- TaskManager progress update integration
- JSON-RPC streaming method dispatch
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pulse.core.protocols import StreamChunk
from pulse.rpc import JsonRpcDispatcher
from pulse.streaming import (
    CancellationToken,
    StreamEvent,
    StreamEventType,
    StreamingExecutionEngine,
)
from pulse.task_manager import TaskManager

# ---------------------------------------------------------------------------
# Test Fixtures & Mocks
# ---------------------------------------------------------------------------


class MockStreamingProvider:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.is_configured = True

    async def generate_stream(
        self, messages: list[dict[str, Any]], temperature: float = 0.2
    ) -> AsyncGenerator[StreamChunk, None]:
        for token in self.tokens:
            yield StreamChunk(content=token)
            await asyncio.sleep(0.001)

    def chat(self, messages: list[Any], temperature: float = 0.2) -> str:
        return "".join(self.tokens)


# ---------------------------------------------------------------------------
# 1. End-to-End Streaming Event Sequence
# ---------------------------------------------------------------------------


def test_streaming_event_sequence() -> None:
    provider = MockStreamingProvider(["Hello", " ", "world", "!"])
    engine = StreamingExecutionEngine(provider=provider)

    events: list[StreamEvent] = []

    async def _run() -> None:
        async for evt in engine.execute_stream("Explain context manager"):
            events.append(evt)

    asyncio.run(_run())

    event_types = [e.event_type for e in events]

    assert StreamEventType.REASONING_START in event_types
    assert StreamEventType.REASONING_STEP in event_types
    assert StreamEventType.LLM_TOKEN in event_types
    assert StreamEventType.COMPLETION in event_types

    # Assert accumulated token stream
    token_deltas = [e.delta for e in events if e.event_type == StreamEventType.LLM_TOKEN]
    assert "".join(token_deltas) == "Hello world!"


# ---------------------------------------------------------------------------
# 2. Tool Execution Real-time Progress Streaming
# ---------------------------------------------------------------------------


def test_tool_execution_streaming() -> None:
    tool_registry = MagicMock()
    mock_tool = MagicMock()
    mock_tool.name = "search"
    tool_registry.get.return_value = mock_tool

    tool_result = MagicMock()
    tool_result.content = "Found 3 matching files."
    tool_registry.execute = AsyncMock(return_value=tool_result)

    reasoning_engine = MagicMock()
    reasoning_engine.safety_manager = None
    reasoning_res = MagicMock()
    reasoning_res.reasoning_steps = []
    reasoning_res.strategy.requires_planning = False
    reasoning_res.strategy.selected_tools = ["search"]
    reasoning_res.response_text = "Tool completed."
    reasoning_engine.reason = AsyncMock(return_value=reasoning_res)

    engine = StreamingExecutionEngine(
        reasoning_engine=reasoning_engine,
        tool_registry=tool_registry,
    )

    events: list[StreamEvent] = []

    async def _run() -> None:
        async for evt in engine.execute_stream("search query"):
            events.append(evt)

    asyncio.run(_run())

    event_types = [e.event_type for e in events]
    assert StreamEventType.TOOL_START in event_types
    assert StreamEventType.TOOL_PROGRESS in event_types
    assert StreamEventType.TOOL_COMPLETE in event_types


# ---------------------------------------------------------------------------
# 3. Cancellation Token Interruption
# ---------------------------------------------------------------------------


def test_cancellation_token_interruption() -> None:
    provider = MockStreamingProvider(["Token 1", "Token 2", "Token 3", "Token 4"])
    engine = StreamingExecutionEngine(provider=provider)
    token = CancellationToken()

    events: list[StreamEvent] = []

    async def _run() -> None:
        async for evt in engine.execute_stream("Test cancel", cancellation_token=token):
            events.append(evt)
            if evt.event_type == StreamEventType.LLM_TOKEN:
                token.cancel("User clicked stop")

    asyncio.run(_run())

    event_types = [e.event_type for e in events]
    assert StreamEventType.CANCELLED in event_types
    cancelled_event = next(e for e in events if e.event_type == StreamEventType.CANCELLED)
    assert "User clicked stop" in cancelled_event.content


# ---------------------------------------------------------------------------
# 4. TaskManager Progress Synchronization
# ---------------------------------------------------------------------------


def test_task_manager_progress_sync(tmp_path: Any) -> None:
    task_mgr = TaskManager(workspace=tmp_path)
    task = asyncio.run(task_mgr.create_task("Streaming task"))

    engine = StreamingExecutionEngine(
        provider=MockStreamingProvider(["Streaming", " ", "task"]),
        task_manager=task_mgr,
    )

    events: list[StreamEvent] = []

    async def _run() -> None:
        async for evt in engine.execute_stream("Stream task", task_id=task.id):
            events.append(evt)

    asyncio.run(_run())

    updated_task = task_mgr.get_task(task.id)
    assert updated_task is not None
    assert updated_task.status.value == "COMPLETED"
    assert updated_task.progress == 100.0


# ---------------------------------------------------------------------------
# 5. JSON-RPC Streaming Method Dispatch
# ---------------------------------------------------------------------------


def test_json_rpc_ask_stream(tmp_path: Any) -> None:
    runtime = MagicMock()
    runtime.provider = MockStreamingProvider(["RPC", " ", "response"])
    dispatcher = JsonRpcDispatcher(runtime)

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "pulse.askStream",
        "params": {"prompt": "Streaming over RPC"},
    }

    res = asyncio.run(dispatcher.dispatch(req))

    assert res is not None
    assert res["id"] == 1
    assert "events" in res["result"]
    assert res["result"]["completed"] is True
    assert len(res["result"]["events"]) >= 3
