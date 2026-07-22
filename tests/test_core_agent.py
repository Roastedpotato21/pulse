import asyncio

from pulse.core.agent import Agent, AgentRequest
from pulse.core.protocols import StreamChunk
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class FakeProvider:
    config = object()
    is_configured = True

    def __init__(self) -> None:
        self.calls = []

    async def generate_stream(self, messages):
        self.calls.append(messages)
        yield StreamChunk("Hello")
        yield StreamChunk(" world")


class DirectTool:
    name = "direct-answer"
    description = "Return a local answer."
    requires_permission = False

    def matches(self, invocation: ToolInvocation):
        return invocation.message == "local"

    async def execute(self, invocation):
        return ToolResult("Handled without a model", terminal=True)


def test_agent_streams_response_and_retains_conversation_history() -> None:
    provider = FakeProvider()
    agent = Agent(provider, system_prompt="system")

    first = asyncio.run(agent.respond(AgentRequest("first", conversation_id="chat", context=["approved file"])))
    second = asyncio.run(agent.respond(AgentRequest("second", conversation_id="chat")))

    assert first.content == "Hello world"
    assert first.request_id
    assert second.content == "Hello world"
    assert provider.calls[0][0] == {"role": "system", "content": "system"}
    assert any("approved file" in message["content"] for message in provider.calls[0])
    assert any(message == {"role": "assistant", "content": "Hello world"} for message in provider.calls[1])


def test_agent_selects_terminal_tool_without_calling_model() -> None:
    provider = FakeProvider()
    agent = Agent(provider, system_prompt="system", tool_registry=ToolRegistry([DirectTool()]))

    response = asyncio.run(agent.respond(AgentRequest("local")))

    assert response.content == "Handled without a model"
    assert response.tool_name == "direct-answer"
    assert provider.calls == []
