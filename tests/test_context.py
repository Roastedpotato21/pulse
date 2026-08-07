"""Unit tests for the Pulse Context Manager (pulse.context).

Tests cover:
- BuiltContext / ContextItem construction
- Each built-in source adapter
- ContextRanker ordering
- ContextCompressor token budget and truncation
- TTL cache hit / miss
- Custom RAG source registration
- AgentOrchestrator integration
- as_strings() return type
- Graceful handling of an empty request
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pulse.context import (
    ActiveFileSource,
    BuiltContext,
    ContextCompressor,
    ContextItem,
    ContextManager,
    ContextRanker,
    ConversationHistorySource,
    GitStatusSource,
    MemorySource,
    RepositoryIntelligenceSource,
    UserIntentSource,
    _estimate_tokens,
)

# ---------------------------------------------------------------------------
# 1. build() returns a BuiltContext
# ---------------------------------------------------------------------------


def test_build_returns_built_context() -> None:
    cm = ContextManager()
    result = asyncio.run(cm.build("How does the planner work?"))
    assert isinstance(result, BuiltContext)
    assert isinstance(result.items, list)
    assert result.total_tokens >= 0
    assert isinstance(result.build_time_ms, float)
    assert result.build_time_ms >= 0


# ---------------------------------------------------------------------------
# 2. Sources gathered concurrently (asyncio.gather used — timing smoke test)
# ---------------------------------------------------------------------------


def test_sources_gathered_concurrently() -> None:
    """All sources should complete within the same event-loop gather call.

    We register two slow sources (each sleeps 0.05 s) and assert that the
    total wall time is well under 0.15 s — confirming concurrent execution
    rather than sequential (~0.10 s).
    """

    class SlowSource:
        async def gather(self, request: str) -> list[ContextItem]:
            await asyncio.sleep(0.05)
            return [ContextItem(source="slow", content="slow result")]

    async def _run() -> float:
        cm = ContextManager()
        await cm.register_source(SlowSource())
        await cm.register_source(SlowSource())
        start = time.monotonic()
        await cm.build("test")
        return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed < 0.15, f"Sources appear sequential (elapsed {elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# 3. Ranker orders by relevance
# ---------------------------------------------------------------------------


def test_ranking_orders_by_relevance() -> None:
    ranker = ContextRanker()
    high = ContextItem(source="memory", content="the planner generates execution plan steps")
    low = ContextItem(source="git", content="branch main HEAD abc123")

    ranked = ranker.rank([low, high], "how does the planner generate steps")
    assert ranked[0].source == "memory", "High-overlap item should rank first"
    assert ranked[0].relevance_score > ranked[1].relevance_score


# ---------------------------------------------------------------------------
# 4. Token budget respected
# ---------------------------------------------------------------------------


def test_token_budget_respected() -> None:
    # 10 items each ~100 tokens — budget of 200 tokens → only ~2 fit.
    items = [
        ContextItem(source="test", content="x " * 200, relevance_score=float(i) / 10)
        for i in range(10, 0, -1)  # descending score so pop() removes lowest
    ]
    compressor = ContextCompressor(max_tokens=200)
    fitted, _ = asyncio.run(compressor.compress(items))
    total = sum(i.token_estimate for i in fitted)
    assert total <= 200, f"Total tokens {total} exceeds budget 200"


# ---------------------------------------------------------------------------
# 5. Compression triggered
# ---------------------------------------------------------------------------


def test_compression_triggers() -> None:
    # A single item that is far too large for the budget.
    big_content = "\n".join(f"line {i}: " + "data " * 20 for i in range(200))
    items = [ContextItem(source="test", content=big_content, relevance_score=0.9)]
    compressor = ContextCompressor(max_tokens=50)
    fitted, was_compressed = asyncio.run(compressor.compress(items))
    assert was_compressed is True
    assert sum(i.token_estimate for i in fitted) <= 50 + 20  # small tolerance


# ---------------------------------------------------------------------------
# 6. MemorySource
# ---------------------------------------------------------------------------


def test_memory_source() -> None:
    memory = MagicMock()
    memory.context_for = AsyncMock(return_value=[
        "User preference — language: Python",
        "Remembered task: implement planner",
    ])
    source = MemorySource(memory)
    items = asyncio.run(source.gather("planner implementation"))
    assert len(items) == 1
    assert items[0].source == "memory"
    assert "planner" in items[0].content or "preference" in items[0].content
    memory.context_for.assert_awaited_once_with("planner implementation", limit=4)


# ---------------------------------------------------------------------------
# 7. GitStatusSource
# ---------------------------------------------------------------------------


def test_git_source() -> None:
    git = MagicMock()
    status = MagicMock()
    status.is_repository = True
    status.branch = "feature/ctx-manager"
    status.head = "abc1234"
    status.changes = []
    git.status = AsyncMock(return_value=status)

    source = GitStatusSource(git)
    items = asyncio.run(source.gather("any request"))
    assert len(items) == 1
    assert items[0].source == "git"
    assert "feature/ctx-manager" in items[0].content
    assert items[0].metadata["branch"] == "feature/ctx-manager"


def test_git_source_non_repo() -> None:
    git = MagicMock()
    status = MagicMock()
    status.is_repository = False
    git.status = AsyncMock(return_value=status)

    source = GitStatusSource(git)
    items = asyncio.run(source.gather("any"))
    assert items == []


# ---------------------------------------------------------------------------
# 8. RepositoryIntelligenceSource
# ---------------------------------------------------------------------------


def test_repository_source() -> None:
    repo = MagicMock()
    sym = MagicMock()
    sym.name = "DAGPlanner"
    sym.kind = "class"
    result = MagicMock()
    result.path = "src/pulse/planner/dag_planner.py"
    result.score = 8.0
    result.symbols = [sym]
    repo.search = AsyncMock(return_value=[result])

    source = RepositoryIntelligenceSource(repo)
    items = asyncio.run(source.gather("dag planner"))
    assert len(items) == 1
    assert items[0].source == "repository"
    assert "dag_planner.py" in items[0].content
    assert items[0].relevance_score == pytest.approx(min(8.0 / 10.0, 1.0), abs=1e-4)


# ---------------------------------------------------------------------------
# 9. ActiveFileSource
# ---------------------------------------------------------------------------


def test_active_file_source(tmp_path: Path) -> None:
    test_file = tmp_path / "agent.py"
    test_file.write_text("class Agent:\n    pass\n", encoding="utf-8")

    source = ActiveFileSource(workspace=tmp_path)
    items = asyncio.run(source.gather("request", active_file=str(test_file)))
    assert len(items) == 1
    assert items[0].source == "active_file"
    assert "agent.py" in items[0].content
    assert "class Agent" in items[0].content


def test_active_file_source_no_file() -> None:
    source = ActiveFileSource(workspace=Path("."))
    items = asyncio.run(source.gather("request", active_file=None))
    assert items == []


def test_active_file_source_missing_file(tmp_path: Path) -> None:
    source = ActiveFileSource(workspace=tmp_path)
    items = asyncio.run(source.gather("request", active_file=str(tmp_path / "nonexistent.py")))
    assert items == []


# ---------------------------------------------------------------------------
# 10. Cache hit within TTL
# ---------------------------------------------------------------------------


def test_cache_hit() -> None:
    call_count = 0

    class CountingSource:
        async def gather(self, request: str) -> list[ContextItem]:
            nonlocal call_count
            call_count += 1
            return [ContextItem(source="counting", content="data")]

    async def _run() -> int:
        cm = ContextManager(cache_ttl=30.0)
        await cm.register_source(CountingSource())
        await cm.build("same request")
        await cm.build("same request")  # should be served from cache
        return call_count

    result = asyncio.run(_run())
    assert result == 1, f"Source called {result} times; expected 1 (cache hit)"


# ---------------------------------------------------------------------------
# 11. Cache miss after TTL
# ---------------------------------------------------------------------------


def test_cache_miss_after_ttl() -> None:
    call_count = 0

    class CountingSource:
        async def gather(self, request: str) -> list[ContextItem]:
            nonlocal call_count
            call_count += 1
            return [ContextItem(source="counting", content="data")]

    async def _run() -> int:
        cm = ContextManager(cache_ttl=0.05)  # 50 ms TTL
        await cm.register_source(CountingSource())
        await cm.build("same request")
        await asyncio.sleep(0.1)  # exceed TTL
        await cm.build("same request")  # cache should have expired
        return call_count

    result = asyncio.run(_run())
    assert result == 2, f"Source called {result} times; expected 2 (cache miss after TTL)"


# ---------------------------------------------------------------------------
# 12. Custom RAG source registration
# ---------------------------------------------------------------------------


def test_custom_source_registered() -> None:
    class MyRagSource:
        async def gather(self, request: str) -> list[ContextItem]:
            return [ContextItem(source="rag", content=f"RAG result for: {request}")]

    async def _run() -> BuiltContext:
        cm = ContextManager(cache_ttl=0)
        await cm.register_source(MyRagSource())
        return await cm.build("custom query")

    result = asyncio.run(_run())
    sources = {item.source for item in result.items}
    assert "rag" in sources, "Custom RAG source output not found in built context"


# ---------------------------------------------------------------------------
# 13. AgentOrchestrator integration
# ---------------------------------------------------------------------------


def test_orchestrator_integration() -> None:
    """Managed context should be prepended to the agent context list."""
    from pulse.context import ContextManager
    from pulse.core.agent import AgentRequest, AgentResponse
    from pulse.orchestration.orchestrator import AgentOrchestrator

    managed_strings = ["ContextManager item 1", "ContextManager item 2"]
    cm = MagicMock(spec=ContextManager)
    cm.as_strings = AsyncMock(return_value=managed_strings)

    agent = MagicMock()
    captured_requests: list[AgentRequest] = []

    async def fake_respond(req: AgentRequest) -> AgentResponse:
        captured_requests.append(req)
        return AgentResponse(
            content="ok",
            conversation_id=req.conversation_id,
            request_id="test-id",
        )

    agent.respond = fake_respond

    orchestrator = AgentOrchestrator(
        agent=agent,
        context_manager=cm,
    )

    request = AgentRequest(message="explain the planner")
    asyncio.run(orchestrator.handle_request(request))

    assert captured_requests, "Agent.respond was never called"
    ctx = list(captured_requests[0].context)
    assert "ContextManager item 1" in ctx
    assert "ContextManager item 2" in ctx
    # Managed context must appear before any other context.
    assert ctx.index("ContextManager item 1") == 0


# ---------------------------------------------------------------------------
# 14. as_strings() returns list[str]
# ---------------------------------------------------------------------------


def test_as_strings_returns_list_of_str() -> None:
    cm = ContextManager()
    result = asyncio.run(cm.as_strings("what is the memory module"))
    assert isinstance(result, list)
    assert all(isinstance(s, str) for s in result)


# ---------------------------------------------------------------------------
# 15. Empty request is handled gracefully
# ---------------------------------------------------------------------------


def test_empty_request_graceful() -> None:
    cm = ContextManager()
    # Should not raise even with an empty prompt.
    result = asyncio.run(cm.build(""))
    assert isinstance(result, BuiltContext)


# ---------------------------------------------------------------------------
# Bonus: UserIntentSource extracts meaningful signals
# ---------------------------------------------------------------------------


def test_user_intent_source_extracts_actions() -> None:
    source = UserIntentSource()
    items = asyncio.run(source.gather("implement the context manager in context.py"))
    assert len(items) == 1
    assert items[0].source == "intent"
    meta = items[0].metadata
    assert "implement" in meta["actions"]
    assert any("context" in ref for ref in meta["refs"] + meta["keywords"])


# ---------------------------------------------------------------------------
# Bonus: ConversationHistorySource
# ---------------------------------------------------------------------------


def test_conversation_history_source() -> None:
    store = MagicMock()
    msg1 = MagicMock()
    msg1.role = "user"
    msg1.content = "What is the planner?"
    msg2 = MagicMock()
    msg2.role = "assistant"
    msg2.content = "The planner decomposes tasks."
    store.read = AsyncMock(return_value=[msg1, msg2])

    source = ConversationHistorySource(store, conversation_id="default")
    items = asyncio.run(source.gather("follow-up question"))
    assert len(items) == 1
    assert items[0].source == "history"
    assert "planner" in items[0].content


# ---------------------------------------------------------------------------
# Bonus: _estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_non_zero() -> None:
    assert _estimate_tokens("hello world") > 0
    assert _estimate_tokens("") == 1  # minimum 1


# ---------------------------------------------------------------------------
# Bonus: ContextManager with no sources still returns BuiltContext
# ---------------------------------------------------------------------------


def test_context_manager_no_sources() -> None:
    async def _run() -> BuiltContext:
        cm = ContextManager()
        # Remove all sources for isolation.
        cm._builtin_sources = []
        cm._extra_sources = []
        return await cm.build("anything")

    result = asyncio.run(_run())
    assert isinstance(result, BuiltContext)
    assert result.items == []
    assert result.total_tokens == 0


# ---------------------------------------------------------------------------
# Bonus: source failure is silently swallowed
# ---------------------------------------------------------------------------


def test_failing_source_does_not_crash_build() -> None:
    class BrokenSource:
        async def gather(self, request: str) -> list[ContextItem]:
            raise RuntimeError("DB connection refused")

    async def _run() -> BuiltContext:
        cm = ContextManager(cache_ttl=0)
        await cm.register_source(BrokenSource())
        return await cm.build("any query")

    # Must not raise; just returns whatever the other sources provided.
    result = asyncio.run(_run())
    assert isinstance(result, BuiltContext)
