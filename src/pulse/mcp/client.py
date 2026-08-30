from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pulse.subprocesses import isolated_process_kwargs, terminate_process
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult

MCP_MAX_CONFIG_BYTES = 1_048_576
MCP_MAX_TOOL_NAME_CHARS = 128


def _split_stdio_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return []
    return [part for part in parts if part]


def _is_loopback_http_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "::1", "localhost"}


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
        cmd = _split_stdio_command(self._endpoint)
        if not cmd:
            return ToolResult("MCP stdio command is invalid.", metadata={"error": "invalid_command"})
        payload = json.dumps({"method": self.name, "params": params}) + "\n"
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **isolated_process_kwargs(),
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload.encode()),
                timeout=30,
            )
            data = json.loads(stdout.decode(errors="replace"))
            content = data.get("result") or data.get("content") or str(data)
            return ToolResult(str(content), metadata={"server": self._server_name, "transport": "stdio"})
        except TimeoutError:
            await terminate_process(process)
            return ToolResult(f"MCP tool '{self.name}' timed out.", metadata={"error": "timeout"})
        except asyncio.CancelledError:
            await terminate_process(process)
            raise
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return ToolResult("MCP tool failed.", metadata={"error": "execution_failed"})

    async def _call_http(self, params: dict[str, Any]) -> ToolResult:
        try:
            import httpx
        except ImportError:
            return ToolResult("httpx is required for HTTP MCP transport.", metadata={"error": "missing_dependency"})
        if not _is_loopback_http_endpoint(self._endpoint):
            return ToolResult("MCP HTTP endpoint must be loopback.", metadata={"error": "endpoint_not_allowed"})

        payload = {"jsonrpc": "2.0", "id": 1, "method": self.name, "params": params}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self._endpoint, json=payload)
                data = response.json()
                content = data.get("result") or data.get("content") or str(data)
                return ToolResult(str(content), metadata={"server": self._server_name, "transport": "http"})
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return ToolResult("MCP tool failed.", metadata={"error": "execution_failed"})


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
        if self._config_path.is_symlink() or self._config_path.stat().st_size > MCP_MAX_CONFIG_BYTES:
            return []
        raw: dict[str, Any] = json.loads(self._config_path.read_text(encoding="utf-8"))
        servers = raw.get("mcp_servers", [])
        return servers if isinstance(servers, list) else []

    async def discover_and_register_tools(self) -> int:
        """Probe each configured MCP server for its tool manifest and register all tools.

        Returns the number of tools successfully registered.
        """
        servers = self._load_mcp_servers()
        registered = 0
        for server in servers:
            if not isinstance(server, dict):
                continue
            server_name = str(server.get("name", "unnamed"))[:MCP_MAX_TOOL_NAME_CHARS]
            transport = str(server.get("transport", "stdio")).lower()
            endpoint = str(server.get("endpoint", ""))
            raw_tools_manifest = server.get("tools", [])
            tools_manifest = raw_tools_manifest if isinstance(raw_tools_manifest, list) else []

            if not tools_manifest and transport == "http" and endpoint:
                tools_manifest = await self._probe_http_manifest(endpoint)
            elif not tools_manifest and transport == "stdio" and endpoint:
                tools_manifest = await self._probe_stdio_manifest(endpoint)

            for tool_def in tools_manifest:
                if not isinstance(tool_def, dict):
                    continue
                tool_leaf = str(tool_def.get("name", "unknown"))[:MCP_MAX_TOOL_NAME_CHARS]
                tool_name = f"{server_name}.{tool_leaf}"
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
        if not _is_loopback_http_endpoint(endpoint):
            return []
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(endpoint.rstrip("/") + "/tools")
                if response.is_success:
                    data = response.json()
                    return data if isinstance(data, list) else data.get("tools", [])
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001, S110
            pass
        return []

    async def _probe_stdio_manifest(self, command: str) -> list[dict[str, Any]]:
        cmd = _split_stdio_command(command)
        if not cmd:
            return []
        payload = json.dumps({"method": "tools/list", "params": {}}) + "\n"
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **isolated_process_kwargs(),
            )
            stdout, _ = await asyncio.wait_for(process.communicate(payload.encode()), timeout=10)
            data = json.loads(stdout.decode(errors="replace"))
            tools = data.get("result", data.get("tools", []))
            return tools if isinstance(tools, list) else []
        except TimeoutError:
            await terminate_process(process)
            return []
        except asyncio.CancelledError:
            await terminate_process(process)
            raise
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            await terminate_process(process)
            return []
