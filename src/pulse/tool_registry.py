"""Async, UI-independent registration and execution of Pulse capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pulse.tool_policy import (
    AuthorizationDecision,
    ToolPolicyEngine,
    ToolRisk,
    ToolSchema,
)


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
    capability: str
    risk: ToolRisk
    schema: ToolSchema | None

    def matches(self, invocation: ToolInvocation) -> bool: ...

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...


PermissionChecker = Callable[[ToolInvocation, Tool], Awaitable[bool]]


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        permission_checker: PermissionChecker | None = None,
        policy_engine: ToolPolicyEngine | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._permission_checker = permission_checker
        self._policy_engine = policy_engine
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
        schema = getattr(tool, "schema", None)
        if schema:
            validation_error = schema.validate(invocation.arguments)
            if validation_error:
                return ToolResult(
                    f"Invalid arguments for {tool.name}: {validation_error}",
                    metadata={"error_code": "invalid_tool_arguments", "validation_error": validation_error},
                )

        requires_approval = bool(getattr(tool, "requires_permission", False))
        policy_requires_approval = False
        if self._policy_engine:
            risk = getattr(tool, "risk", ToolRisk.LOW)
            capability = getattr(tool, "capability", None) or tool.name
            authorization = self._policy_engine.authorize(
                tool_name=tool.name,
                capability=capability,
                risk=risk,
                arguments=invocation.arguments,
            )
            self._policy_engine.record(authorization)
            if authorization.decision == AuthorizationDecision.DENY:
                return ToolResult(
                    f"Tool denied for {tool.name}: {authorization.reason}",
                    metadata={"error_code": "tool_policy_denied", "policy_reason": authorization.reason},
                )
            policy_requires_approval = authorization.decision == AuthorizationDecision.ASK
            requires_approval = requires_approval or policy_requires_approval

        approval_denied = (
            policy_requires_approval and self._permission_checker is None
        ) or (
            requires_approval
            and self._permission_checker is not None
            and not await self._permission_checker(invocation, tool)
        )
        if approval_denied:
            return ToolResult(
                f"Permission denied for {tool.name}.",
                metadata={"permission_denied": True, "error_code": "tool_approval_denied"},
            )
        return await tool.execute(invocation)

    async def execute_many(self, invocations: Iterable[ToolInvocation]) -> list[ToolResult | None]:
        """Independent tools may run concurrently; callers retain input ordering."""
        return list(await asyncio.gather(*(self.execute(invocation) for invocation in invocations)))
