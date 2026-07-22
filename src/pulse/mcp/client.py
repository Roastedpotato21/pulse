from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class _MCPTool:
    """An async tool_registry-compatible wrapper around an MCP server tool definition."""

    requires_permission = True

    def __init__(
        self,
        name: str,
        description: str,
        server_name: str,
        transport: str,
        endpoint: str,
    ) -> None:
        self.name = name
        self.description = description
        self._server_name = server_name
        self._transport = transport
        self._endpoint = endpoint

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = dict(invocation.arguments)
        if self._transport == "stdio":
            return await self._call_stdio(params)
        return await self._call_http(params)

    async def _call_stdio(self, params: dict[str, Any]) -> ToolResult:
        cmd = self._endpoint.split()
        payload = json.dumps({"method": self.name, "params": params}) + "\n"
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload.encode()),
                timeout=30,
            )
            data = json.loads(stdout.decode(errors="replace"))
            content = data.get("result") or data.get("content") or str(data)
            return ToolResult(str(content), metadata={"server": self._server_name, "transport": "stdio"})
        except asyncio.TimeoutError:
            return ToolResult(f"MCP tool '{self.name}' timed out.", metadata={"error": "timeout"})
        except Exception as exc:
            return ToolResult(f"MCP tool '{self.name}' error: {exc}", metadata={"error": str(exc)})

    async def _call_http(self, params: dict[str, Any]) -> ToolResult:
        try:
            import httpx
        except ImportError:
            return ToolResult("httpx is required for HTTP MCP transport.", metadata={"error": "missing_dependency"})

        payload = {"jsonrpc": "2.0", "id": 1, "method": self.name, "params": params}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self._endpoint, json=payload)
                data = response.json()
                content = data.get("result") or data.get("content") or str(data)
                return ToolResult(str(content), metadata={"server": self._server_name, "transport": "http"})
        except Exception as exc:
            return ToolResult(f"MCP tool '{self.name}' error: {exc}", metadata={"error": str(exc)})


class MCPClientManager:
    """Discovers and connects to standard stdio/HTTP MCP servers defined in agent.config.json under `mcp_servers`.

    Converts external MCP tool definitions into async pulse.tool_registry tool instances.
    """

    def __init__(self, config_path: Path, registry: ToolRegistry) -> None:
        self._config_path = config_path
        self._registry = registry

    def _load_mcp_servers(self) -> list[dict[str, Any]]:
        if not self._config_path.exists():
            return []
        raw: dict[str, Any] = json.loads(self._config_path.read_text(encoding="utf-8"))
        return list(raw.get("mcp_servers", []))

    async def discover_and_register_tools(self) -> int:
        """Probe each configured MCP server for its tool manifest and register all tools.

        Returns the number of tools successfully registered.
        """
        servers = self._load_mcp_servers()
        registered = 0
        for server in servers:
            server_name: str = server.get("name", "unnamed")
            transport: str = server.get("transport", "stdio").lower()
            endpoint: str = server.get("endpoint", "")
            tools_manifest: list[dict[str, Any]] = server.get("tools", [])

            if not tools_manifest and transport == "http" and endpoint:
                tools_manifest = await self._probe_http_manifest(endpoint)
            elif not tools_manifest and transport == "stdio" and endpoint:
                tools_manifest = await self._probe_stdio_manifest(endpoint)

            for tool_def in tools_manifest:
                tool_name = f"{server_name}.{tool_def.get('name', 'unknown')}"
                description = tool_def.get("description", f"MCP tool from {server_name}")
                tool = _MCPTool(
                    name=tool_name,
                    description=description,
                    server_name=server_name,
                    transport=transport,
                    endpoint=endpoint,
                )
                try:
                    self._registry.register(tool)
                    registered += 1
                except ValueError:
                    pass  # Already registered; skip

        return registered

    async def _probe_http_manifest(self, endpoint: str) -> list[dict[str, Any]]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(endpoint.rstrip("/") + "/tools")
                if response.is_success:
                    data = response.json()
                    return data if isinstance(data, list) else data.get("tools", [])
        except Exception:
            pass
        return []

    async def _probe_stdio_manifest(self, command: str) -> list[dict[str, Any]]:
        cmd = command.split()
        payload = json.dumps({"method": "tools/list", "params": {}}) + "\n"
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(payload.encode()), timeout=10)
            data = json.loads(stdout.decode(errors="replace"))
            tools = data.get("result", data.get("tools", []))
            return tools if isinstance(tools, list) else []
        except Exception:
            return []
