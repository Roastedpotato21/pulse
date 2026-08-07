"""JSON-RPC 2.0 adapter for Pulse, with an optional local WebSocket server."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

from pulse.config import load_agent_config
from pulse.runtime import build_runtime
from pulse.tool_registry import ToolInvocation


class PulseRpcEngine(Protocol):
    async def respond_remote(self, prompt: str, context: list[str]) -> str: ...


class JsonRpcDispatcher:
    """Transport-neutral JSON-RPC dispatcher; easy to test without sockets."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self._error(request_id, -32600, "Invalid JSON-RPC request.")
        method, params = message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Parameters must be an object.")
        try:
            if method == "pulse.health":
                result: Any = {"status": "ok"}
            elif method in {"pulse.ask", "pulse.codeAction"}:
                prompt = str(params.get("prompt", "")).strip()
                if not prompt:
                    return self._error(request_id, -32602, "A prompt is required.")
                context = [str(item) for item in params.get("context", []) if isinstance(item, str)]
                result = {"content": await self.runtime.agent.respond_remote(prompt, context)}
            elif method in {"pulse.askStream", "pulse.stream"}:
                prompt = str(params.get("prompt", "")).strip()
                if not prompt:
                    return self._error(request_id, -32602, "A prompt is required.")
                from pulse.streaming import StreamingExecutionEngine
                engine = StreamingExecutionEngine(
                    provider=getattr(self.runtime, "provider", None),
                    task_manager=getattr(self.runtime, "task_manager", None),
                    tool_registry=getattr(self.runtime, "tools", None),
                )
                events = []
                async for event in engine.execute_stream(prompt):
                    events.append(event.to_dict())
                result = {"events": events, "completed": True}
            elif method == "pulse.command":
                name = str(params.get("name", ""))
                if name in {"edit", "rollback"}:
                    return self._error(request_id, -32602, "Interactive edit approval is unavailable through RPC.")
                tool_result = await self.runtime.tools.execute(ToolInvocation(name=name, arguments=params.get("arguments", {})))
                if tool_result is None:
                    return self._error(request_id, -32601, f"Unknown Pulse command: {name}")
                result = {"content": tool_result.content, "metadata": self._json_metadata(tool_result.metadata)}
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
        except Exception as error:  # Boundary adapter: return protocol errors, never tracebacks.  # noqa: BLE001
            return self._error(request_id, -32000, str(error))
        return None if request_id is None else {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _json_metadata(metadata: dict[str, Any]) -> dict[str, str]:
        return {key: str(value) for key, value in metadata.items()}


async def serve(workspace: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve Pulse over loopback WebSocket transport using JSON-RPC messages."""
    try:
        from websockets.asyncio.server import serve as websocket_serve
    except ImportError as error:
        raise RuntimeError("WebSocket support requires the 'websockets' package. Run `uv sync`.") from error

    workspace_path = Path(workspace).resolve()
    runtime = build_runtime(workspace_path, load_agent_config(workspace_path))
    dispatcher = JsonRpcDispatcher(runtime)

    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            try:
                payload = json.loads(raw)
                response = await dispatcher.dispatch(payload)
            except json.JSONDecodeError:
                response = JsonRpcDispatcher._error(None, -32700, "Parse error.")
            if response is not None:
                await websocket.send(json.dumps(response))

    async with websocket_serve(handler, host, port, max_size=1_048_576):
        print(f"Pulse JSON-RPC server listening on ws://{host}:{port}")
        await asyncio.get_running_loop().create_future()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pulse-rpc", description="Run Pulse's local JSON-RPC WebSocket server.")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(serve(args.workspace, args.host, args.port))
