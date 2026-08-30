"""JSON-RPC 2.0 adapter for Pulse, with an optional local WebSocket server."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import Any, Protocol

from pulse import __version__
from pulse.config import load_agent_config
from pulse.runtime import build_runtime
from pulse.telemetry import get_correlation_id, set_correlation_id
from pulse.tool_registry import ToolInvocation

RPC_PROTOCOL_VERSION = "1.0"
RPC_COMPATIBLE_MAJOR = 1
RPC_MAX_PROMPT_CHARS = 100_000
RPC_MAX_CONTEXT_ITEMS = 64
RPC_MAX_CONTEXT_CHARS = 500_000
RPC_COMMAND_ALLOWLIST = frozenset({"doctor", "git", "index", "search", "status", "symbols"})
RPC_METHODS = (
    "pulse.ask",
    "pulse.askStream",
    "pulse.codeAction",
    "pulse.command",
    "pulse.health",
    "pulse.protocolVersion",
    "pulse.stream",
)


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
        requested_protocol = params.get("protocol_version")
        if requested_protocol is not None and not self._is_compatible_protocol(
            requested_protocol
        ):
            return self._error(
                request_id,
                -32001,
                "Unsupported Pulse RPC protocol version.",
            )
        correlation_id = set_correlation_id(params.get("correlation_id"))
        try:
            if method == "pulse.health":
                result: Any = {
                    "status": "ok",
                    "service_version": __version__,
                    "protocol_version": RPC_PROTOCOL_VERSION,
                }
            elif method == "pulse.protocolVersion":
                result = {
                    "protocol_version": RPC_PROTOCOL_VERSION,
                    "compatible_major": RPC_COMPATIBLE_MAJOR,
                    "methods": list(RPC_METHODS),
                }
            elif method in {"pulse.ask", "pulse.codeAction"}:
                prompt = str(params.get("prompt", "")).strip()
                if not prompt:
                    return self._error(request_id, -32602, "A prompt is required.")
                if len(prompt) > RPC_MAX_PROMPT_CHARS:
                    return self._error(request_id, -32602, "The prompt exceeds the size limit.")
                raw_context = params.get("context", [])
                if not isinstance(raw_context, list) or any(
                    not isinstance(item, str) for item in raw_context
                ):
                    return self._error(request_id, -32602, "Context must be an array of strings.")
                if len(raw_context) > RPC_MAX_CONTEXT_ITEMS or sum(
                    len(item) for item in raw_context
                ) > RPC_MAX_CONTEXT_CHARS:
                    return self._error(request_id, -32602, "Context exceeds the size limit.")
                context = list(raw_context)
                result = {"content": await self.runtime.agent.respond_remote(prompt, context)}
            elif method in {"pulse.askStream", "pulse.stream"}:
                prompt = str(params.get("prompt", "")).strip()
                if not prompt:
                    return self._error(request_id, -32602, "A prompt is required.")
                if len(prompt) > RPC_MAX_PROMPT_CHARS:
                    return self._error(request_id, -32602, "The prompt exceeds the size limit.")
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
                if name not in RPC_COMMAND_ALLOWLIST:
                    return self._error(
                        request_id,
                        -32602,
                        "This command is unavailable through RPC because interactive approval is required.",
                    )
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "Command arguments must be an object.")
                tool_result = await self.runtime.tools.execute(
                    ToolInvocation(name=name, arguments=arguments)
                )
                if tool_result is None:
                    return self._error(request_id, -32601, "Unknown Pulse command.")
                result = {"content": tool_result.content, "metadata": self._json_metadata(tool_result.metadata)}
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
        except Exception:  # Boundary adapter: return protocol errors, never tracebacks.  # noqa: BLE001
            return self._error(request_id, -32000, "Internal Pulse error.")
        return None if request_id is None else {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
            "correlation_id": correlation_id,
            "pulse_protocol_version": RPC_PROTOCOL_VERSION,
        }

    @staticmethod
    def _is_compatible_protocol(value: object) -> bool:
        if not isinstance(value, str):
            return False
        major, separator, minor = value.partition(".")
        return separator == "." and major.isdigit() and minor.isdigit() and int(major) == RPC_COMPATIBLE_MAJOR

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
            "correlation_id": get_correlation_id(),
            "pulse_protocol_version": RPC_PROTOCOL_VERSION,
        }

    @staticmethod
    def _json_metadata(metadata: dict[str, Any]) -> dict[str, str]:
        allowed = {"correlation_id", "error_code", "permission_denied"}
        return {key: str(value) for key, value in metadata.items() if key in allowed}


def _valid_rpc_token(token: str | None) -> bool:
    if token is None or len(token) < 32 or len(token) > 512:
        return False
    lowered = token.lower()
    return lowered not in {"changeme", "replace_me", "placeholder"} and len(set(token)) >= 8


def _authorized_rpc_header(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    candidate = header.removeprefix("Bearer ")
    return bool(candidate) and hmac.compare_digest(candidate, token)


async def serve(
    workspace: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auth_token: str | None = None,
) -> None:
    """Serve Pulse over loopback WebSocket transport using JSON-RPC messages."""
    if host.strip().lower() not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("pulse-rpc is a local-only service and must bind to loopback.")
    token = auth_token or os.environ.get("PULSE_RPC_TOKEN")
    if not _valid_rpc_token(token):
        raise ValueError(
            "PULSE_RPC_TOKEN must be a non-placeholder secret of 32-512 characters."
        )
    assert token is not None
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
                if not isinstance(payload, dict):
                    response = JsonRpcDispatcher._error(None, -32600, "Invalid JSON-RPC request.")
                else:
                    response = await dispatcher.dispatch(payload)
            except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
                response = JsonRpcDispatcher._error(None, -32700, "Parse error.")
            if response is not None:
                await websocket.send(json.dumps(response))

    async def authenticate(_connection: Any, request: Any) -> Any | None:
        if _authorized_rpc_header(request.headers.get("Authorization"), token):
            return None
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        return Response(401, "Unauthorized", Headers(), b"Unauthorized")

    async with websocket_serve(
        handler,
        host,
        port,
        max_size=1_048_576,
        process_request=authenticate,
    ):
        print(f"Pulse JSON-RPC server listening on ws://{host}:{port}")
        await asyncio.get_running_loop().create_future()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pulse-rpc", description="Run Pulse's local JSON-RPC WebSocket server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.workspace, args.host, args.port))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError):
        print("pulse-rpc failed to start. Check host, port, workspace, and token settings.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
