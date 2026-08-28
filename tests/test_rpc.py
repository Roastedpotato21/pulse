import asyncio
from types import SimpleNamespace

from pulse.rpc import JsonRpcDispatcher


class FakeAgent:
    async def respond_remote(self, prompt, context):
        return f"Reply to {prompt} with {len(context)} context item(s)"


class FakeTools:
    async def execute(self, invocation):
        return SimpleNamespace(content=f"Ran {invocation.name}", metadata={})


def test_json_rpc_dispatches_prompts_and_tool_commands() -> None:
    dispatcher = JsonRpcDispatcher(SimpleNamespace(agent=FakeAgent(), tools=FakeTools()))

    ask = asyncio.run(dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "pulse.ask", "params": {"prompt": "Hello", "context": ["file"]}}))
    command = asyncio.run(dispatcher.dispatch({"jsonrpc": "2.0", "id": 2, "method": "pulse.command", "params": {"name": "verify"}}))

    assert ask["result"] == {"content": "Reply to Hello with 1 context item(s)"}
    assert command["result"] == {"content": "Ran verify", "metadata": {}}
    assert len(ask["correlation_id"]) == 32
    assert len(command["correlation_id"]) == 32


def test_json_rpc_rejects_invalid_requests_and_remote_edits() -> None:
    dispatcher = JsonRpcDispatcher(SimpleNamespace(agent=FakeAgent(), tools=FakeTools()))

    invalid = asyncio.run(dispatcher.dispatch({"id": 1, "method": "pulse.ask"}))
    edit = asyncio.run(dispatcher.dispatch({"jsonrpc": "2.0", "id": 2, "method": "pulse.command", "params": {"name": "edit"}}))

    assert invalid["error"]["code"] == -32600
    assert "approval" in edit["error"]["message"]
