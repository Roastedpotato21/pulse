import asyncio
from types import SimpleNamespace

import pytest

from pulse.rpc import RPC_PROTOCOL_VERSION, JsonRpcDispatcher, serve


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
    assert ask["pulse_protocol_version"] == RPC_PROTOCOL_VERSION


def test_json_rpc_rejects_invalid_requests_and_remote_edits() -> None:
    dispatcher = JsonRpcDispatcher(SimpleNamespace(agent=FakeAgent(), tools=FakeTools()))

    invalid = asyncio.run(dispatcher.dispatch({"id": 1, "method": "pulse.ask"}))
    edit = asyncio.run(dispatcher.dispatch({"jsonrpc": "2.0", "id": 2, "method": "pulse.command", "params": {"name": "edit"}}))

    assert invalid["error"]["code"] == -32600
    assert "approval" in edit["error"]["message"]


def test_json_rpc_negotiates_a_versioned_compatibility_contract() -> None:
    dispatcher = JsonRpcDispatcher(SimpleNamespace(agent=FakeAgent(), tools=FakeTools()))
    supported = asyncio.run(
        dispatcher.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "pulse.protocolVersion",
                "params": {"protocol_version": "1.9"},
            }
        )
    )
    rejected = asyncio.run(
        dispatcher.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "pulse.health",
                "params": {"protocol_version": "2.0"},
            }
        )
    )

    assert supported["result"]["compatible_major"] == 1
    assert "pulse.health" in supported["result"]["methods"]
    assert rejected["error"]["code"] == -32001
    assert rejected["pulse_protocol_version"] == RPC_PROTOCOL_VERSION


def test_json_rpc_refuses_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="local-only"):
        asyncio.run(serve(".", host="0.0.0.0"))
