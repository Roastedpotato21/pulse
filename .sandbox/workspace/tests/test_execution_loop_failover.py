from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from pulse.core.protocols import LLMProvider, StreamChunk
from pulse.orchestration import AgentOrchestrator
from pulse.planner import AutonomousLoop
from pulse.providers import FailoverProvider


class DummyPrimaryProvider(LLMProvider):
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail

    async def generate_stream(
        self, messages: list[dict[str, Any]], temperature: float = 0.2
    ) -> AsyncGenerator[StreamChunk, None]:
        if self.should_fail:
            raise RuntimeError("Primary provider timeout/failure")
        yield StreamChunk("Primary response", {})


class DummySecondaryProvider(LLMProvider):
    async def generate_stream(
        self, messages: list[dict[str, Any]], temperature: float = 0.2
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk("Secondary response", {})


@pytest.mark.anyio
async def test_failover_provider_success_primary():
    primary = DummyPrimaryProvider(should_fail=False)
    secondary = DummySecondaryProvider()
    provider = FailoverProvider(primary, secondary)

    chunks = [chunk.content async for chunk in provider.generate_stream([])]
    assert chunks == ["Primary response"]


@pytest.mark.anyio
async def test_failover_provider_fallback_to_secondary():
    primary = DummyPrimaryProvider(should_fail=True)
    secondary = DummySecondaryProvider()
    provider = FailoverProvider(primary, secondary)

    chunks = [chunk.content async for chunk in provider.generate_stream([])]
    assert chunks == ["Secondary response"]


@pytest.mark.anyio
async def test_autonomous_loop_execution(tmp_path: Path):
    orchestrator = AgentOrchestrator()
    loop = AutonomousLoop(
        orchestrator=orchestrator,
        checkpoint_dir=tmp_path / "checkpoints",
        max_steps=3,
    )

    result = await loop.run("test prompt")
    assert result.turns <= 3
    assert (tmp_path / "checkpoints" / "checkpoint_step_1.json").exists()
