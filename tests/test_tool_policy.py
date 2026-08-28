"""Adversarial contracts for the model-to-tool authorization boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pulse.audit import AuditLog
from pulse.tool_policy import (
    ArgumentKind,
    ToolArgument,
    ToolPolicyEngine,
    ToolRisk,
    ToolSchema,
)
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class WriteTool:
    name = "write_file"
    description = "Write an authorized workspace file."
    requires_permission = False
    capability = "workspace.write"
    risk = ToolRisk.HIGH
    schema = ToolSchema(
        (ToolArgument("file", ArgumentKind.STRING, required=True),)
    )

    def __init__(self) -> None:
        self.executed = False

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.executed = True
        return ToolResult("written")


def _registry(tmp_path: Path, *, approve: bool) -> tuple[ToolRegistry, WriteTool, AuditLog]:
    audit = AuditLog(tmp_path / "audit.jsonl")

    async def permission_checker(_invocation: ToolInvocation, _tool: WriteTool) -> bool:
        return approve

    tool = WriteTool()
    registry = ToolRegistry(
        [tool],
        permission_checker=permission_checker,
        policy_engine=ToolPolicyEngine(
            workspace=tmp_path,
            subject_id="test-user",
            allowed_capabilities=frozenset({"workspace.write"}),
            audit_log=audit,
        ),
    )
    return registry, tool, audit


def test_schema_rejection_happens_before_policy_or_execution(tmp_path: Path) -> None:
    registry, tool, audit = _registry(tmp_path, approve=True)

    result = asyncio.run(registry.execute(ToolInvocation(name="write_file", arguments={})))

    assert result and result.metadata["error_code"] == "invalid_tool_arguments"
    assert tool.executed is False
    assert audit.entries == []


def test_workspace_escape_is_denied_and_audited(tmp_path: Path) -> None:
    registry, tool, audit = _registry(tmp_path, approve=True)

    result = asyncio.run(
        registry.execute(ToolInvocation(name="write_file", arguments={"file": "../outside.txt"}))
    )

    assert result and result.metadata["error_code"] == "tool_policy_denied"
    assert tool.executed is False
    assert audit.entries[-1].action == "tool-policy-deny"
    assert "escapes the authorized workspace" in audit.entries[-1].detail
    assert "outside.txt" not in audit.entries[-1].detail


def test_unknown_capability_fails_closed_before_execution(tmp_path: Path) -> None:
    registry, tool, audit = _registry(tmp_path, approve=True)
    registry._policy_engine.allowed_capabilities = frozenset()

    result = asyncio.run(
        registry.execute(ToolInvocation(name="write_file", arguments={"file": "safe.txt"}))
    )

    assert result and result.metadata["error_code"] == "tool_policy_denied"
    assert tool.executed is False
    assert audit.entries[-1].action == "tool-policy-deny"


def test_high_risk_tool_requires_explicit_approval_and_is_audited(tmp_path: Path) -> None:
    denied_registry, denied_tool, denied_audit = _registry(tmp_path, approve=False)
    denied = asyncio.run(
        denied_registry.execute(ToolInvocation(name="write_file", arguments={"file": "safe.txt"}))
    )
    assert denied and denied.metadata["error_code"] == "tool_approval_denied"
    assert denied_tool.executed is False
    assert denied_audit.entries[-1].action == "tool-policy-ask"

    allowed_registry, allowed_tool, allowed_audit = _registry(tmp_path, approve=True)
    allowed = asyncio.run(
        allowed_registry.execute(ToolInvocation(name="write_file", arguments={"file": "safe.txt"}))
    )
    assert allowed and allowed.content == "written"
    assert allowed_tool.executed is True
    assert allowed_audit.entries[-1].action == "tool-policy-ask"


def test_central_policy_fails_closed_when_approval_handler_is_unavailable(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    tool = WriteTool()
    registry = ToolRegistry(
        [tool],
        policy_engine=ToolPolicyEngine(
            workspace=tmp_path,
            allowed_capabilities=frozenset({"workspace.write"}),
            audit_log=audit,
        ),
    )

    result = asyncio.run(
        registry.execute(ToolInvocation(name="write_file", arguments={"file": "safe.txt"}))
    )

    assert result and result.metadata["error_code"] == "tool_approval_denied"
    assert tool.executed is False
