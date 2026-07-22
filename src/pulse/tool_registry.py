"""Async, UI-independent registration and execution of Pulse capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    name: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    terminal: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    """Implement this small contract, then register an instance."""

    name: str
    description: str
    requires_permission: bool

    def matches(self, invocation: ToolInvocation) -> bool: ...

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...


PermissionChecker = Callable[[ToolInvocation, Tool], Awaitable[bool]]


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = (), *, permission_checker: PermissionChecker | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._permission_checker = permission_checker
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def discover(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def match(self, invocation: ToolInvocation) -> Tool | None:
        if invocation.name:
            return self.get(invocation.name)
        return next((tool for tool in self._tools.values() if tool.matches(invocation)), None)

    async def execute(self, invocation: ToolInvocation) -> ToolResult | None:
        tool = self.match(invocation)
        if tool is None:
            return None
        if tool.requires_permission and self._permission_checker and not await self._permission_checker(invocation, tool):
            return ToolResult(f"Permission denied for {tool.name}.", metadata={"permission_denied": True})
        return await tool.execute(invocation)

    async def execute_many(self, invocations: Iterable[ToolInvocation]) -> list[ToolResult | None]:
        """Independent tools may run concurrently; callers retain input ordering."""
        return list(await asyncio.gather(*(self.execute(invocation) for invocation in invocations)))
