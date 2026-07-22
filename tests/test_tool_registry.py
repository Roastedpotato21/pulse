import asyncio

import pytest

from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class EchoTool:
    name = "echo"
    description = "Echo the supplied value."
    requires_permission = False

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(str(invocation.arguments["value"]))


class ProtectedTool(EchoTool):
    name = "protected"
    requires_permission = True


def test_registry_discovers_and_executes_registered_tool() -> None:
    registry = ToolRegistry([EchoTool()])

    result = asyncio.run(registry.execute(ToolInvocation(name="echo", arguments={"value": "hello"})))

    assert [tool.name for tool in registry.discover()] == ["echo"]
    assert result and result.content == "hello"


def test_registry_checks_permission_before_execution() -> None:
    async def deny(invocation, tool):
        return False

    result = asyncio.run(ToolRegistry([ProtectedTool()], permission_checker=deny).execute(ToolInvocation(name="protected", arguments={"value": "no"})))

    assert result and result.metadata["permission_denied"] is True


def test_registry_rejects_duplicate_names_and_supports_parallel_execution() -> None:
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([EchoTool(), EchoTool()])

    registry = ToolRegistry([EchoTool()])
    results = asyncio.run(registry.execute_many([
        ToolInvocation(name="echo", arguments={"value": "one"}),
        ToolInvocation(name="echo", arguments={"value": "two"}),
    ]))
    assert [result.content for result in results if result] == ["one", "two"]
