import asyncio
from pathlib import Path

from pulse.memory import LongTermMemory, MemoryContextSource


def test_memory_persists_project_context_and_user_preferences(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path)
    entry = asyncio.run(memory.store_project_context("The invoice service uses decimal arithmetic.", tags=("invoice", "billing")))
    asyncio.run(memory.set_preference("response_style", "concise"))

    reloaded = LongTermMemory(tmp_path)
    matches = asyncio.run(reloaded.retrieve("billing invoice"))

    assert entry.category == "project"
    assert [match.content for match in matches] == ["The invoice service uses decimal arithmetic."]
    assert asyncio.run(reloaded.preferences()) == {"response_style": "concise"}


def test_memory_context_source_returns_relevant_context_before_agent_work(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path)
    asyncio.run(memory.remember_agent_information("Use the repository index before inspecting service code.", tags=("repository",)))
    asyncio.run(memory.set_preference("testing", "run focused tests first"))

    class Request:
        message = "How does repository service code work?"

    context = asyncio.run(MemoryContextSource(memory).context_for(Request()))

    assert any("User preference — testing" in item for item in context)
    assert any("repository index" in item for item in context)
