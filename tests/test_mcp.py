from __future__ import annotations

import pytest
from pathlib import Path
from pulse.tool_registry import ToolInvocation, ToolRegistry
from pulse.mcp import MCPClientManager, LocalToolLoader


def test_mcp_client_empty_config(tmp_path: Path):
    config = tmp_path / "agent.config.json"
    config.write_text("{}", encoding="utf-8")
    registry = ToolRegistry()
    manager = MCPClientManager(config_path=config, registry=registry)
    servers = manager._load_mcp_servers()
    assert servers == []


def test_mcp_client_missing_config(tmp_path: Path):
    registry = ToolRegistry()
    manager = MCPClientManager(config_path=tmp_path / "nonexistent.json", registry=registry)
    assert manager._load_mcp_servers() == []


@pytest.mark.anyio
async def test_mcp_client_registers_tools_from_manifest(tmp_path: Path):
    config = tmp_path / "agent.config.json"
    config.write_text(
        '{"mcp_servers": [{"name": "testsrv", "transport": "stdio", "endpoint": "echo", "tools": [{"name": "ping", "description": "Ping tool"}]}]}',
        encoding="utf-8",
    )
    registry = ToolRegistry()
    manager = MCPClientManager(config_path=config, registry=registry)
    count = await manager.discover_and_register_tools()
    assert count == 1
    assert registry.get("testsrv.ping") is not None


def test_local_tool_loader_empty_dir(tmp_path: Path):
    registry = ToolRegistry()
    loader = LocalToolLoader(workspace=tmp_path, registry=registry)
    count = loader.load()
    assert count == 0


def test_local_tool_loader_loads_valid_tool(tmp_path: Path):
    tools_dir = tmp_path / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    tool_script = tools_dir / "greet.py"
    tool_script.write_text(
        'from pulse.tool_registry import ToolInvocation, ToolResult\n'
        'NAME = "greet"\n'
        'DESCRIPTION = "Say hello"\n'
        'def execute(invocation: ToolInvocation) -> ToolResult:\n'
        '    return ToolResult("hello from local tool")\n',
        encoding="utf-8",
    )
    registry = ToolRegistry()
    loader = LocalToolLoader(workspace=tmp_path, registry=registry)
    count = loader.load()
    assert count == 1
    assert registry.get("greet") is not None


@pytest.mark.anyio
async def test_local_tool_loader_executes_tool(tmp_path: Path):
    tools_dir = tmp_path / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "echo_tool.py").write_text(
        'from pulse.tool_registry import ToolInvocation, ToolResult\n'
        'NAME = "echo_tool"\n'
        'DESCRIPTION = "Echo args"\n'
        'def execute(invocation: ToolInvocation) -> ToolResult:\n'
        '    return ToolResult(f"echo: {invocation.message}")\n',
        encoding="utf-8",
    )
    registry = ToolRegistry()
    loader = LocalToolLoader(workspace=tmp_path, registry=registry)
    loader.load()
    result = await registry.execute(ToolInvocation(name="echo_tool", message="test-msg"))
    assert result is not None
    assert "echo: test-msg" in result.content


def test_local_tool_loader_skips_no_execute(tmp_path: Path):
    tools_dir = tmp_path / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "broken.py").write_text('NAME = "broken"\nDESCRIPTION = "No execute"\n', encoding="utf-8")
    registry = ToolRegistry()
    loader = LocalToolLoader(workspace=tmp_path, registry=registry)
    count = loader.load()
    assert count == 0


def test_local_tool_loader_skips_private_files(tmp_path: Path):
    tools_dir = tmp_path / ".agent" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "_helper.py").write_text('NAME = "helper"\nDESCRIPTION = "private"\ndef execute(i): pass\n', encoding="utf-8")
    registry = ToolRegistry()
    count = LocalToolLoader(workspace=tmp_path, registry=registry).load()
    assert count == 0
